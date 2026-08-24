import unittest
import os
import json
import tempfile
import sys
import asyncio

# Ensure neutral_relay can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay


class TestResponseCompletionTracker(unittest.TestCase):
    def snapshot(self, text, *, user_count=2, last_user_text="RID-1", soft=False, has_assistant=True,
                 has_copy_rate=True, stop_present=False):
        return {
            "userCount": user_count,
            "lastUserText": last_user_text,
            "text": text,
            "hasAssistant": has_assistant,
            "softGenerating": soft,
            # B4 (F1): completion features are now the hard finalize gate.
            "hasCopyRate": has_copy_rate,
            "stopPresent": stop_present,
        }

    # --- NORMAL stage (no soft markers) ---

    def test_normal_short_response_uses_normal_settle_window(self):
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("Short answer", soft=False)
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=0), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=2), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=4), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=6), (False, ""))
        # 5 consecutive identical reads + 8s stable + features -> finalize (B4: 8s/4 reads).
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=8), (True, "Short answer"))

    def test_normal_does_not_finalize_before_4_reads_or_8s(self):
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("answer", soft=False)
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=0), (False, ""))
        # 4 reads but only 6s stable -> not complete (B4: 8s settle).
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=2), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=4), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=6), (False, ""))

    def test_normal_never_finalizes_without_completion_features(self):
        # B4 (F1): stable text alone must NOT finalize — copy/rate icons present
        # and the stop button gone gate completion. This fixes the A-class
        # false truncation (streaming pause mistaken for "done").
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("VERDICT: PASS", soft=False, has_copy_rate=False)
        for now in (0, 4, 8, 16, 32):
            self.assertEqual(tracker.observe(snap, 1, "RID-1", now=now), (False, ""))
        # stop button still present also blocks finalize.
        snap2 = self.snapshot("VERDICT: PASS", soft=False, has_copy_rate=True, stop_present=True)
        for now in (0, 8, 16):
            self.assertEqual(tracker.observe(snap2, 1, "RID-1", now=now), (False, ""))
        # once features are present, the settle gate applies (8s / 4 reads).
        snap3 = self.snapshot("VERDICT: PASS", soft=False, has_copy_rate=True, stop_present=False)
        for now in (0, 4, 8, 16):
            expected = (True, "VERDICT: PASS") if now >= 8 else (False, "")
            self.assertEqual(tracker.observe(snap3, 1, "RID-1", now=now), expected)

    # --- CONSERVATIVE stage (soft markers remain) ---

    def test_conservative_stale_marker_with_permanently_stable_response_eventually_finalizes(self):
        # Case 1: stale marker + permanently stable completed response.
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("VERDICT: PASS", soft=True)
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=0), (False, ""))
        # Through 25s: 6 consecutive reads but only 25s stable -> not final.
        for now in (5, 10, 15, 20, 25):
            self.assertEqual(tracker.observe(snap, 1, "RID-1", now=now), (False, ""))
        # At 30s: 7 consecutive reads AND 30s stable -> finalize (CONSERVATIVE).
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=30), (True, "VERDICT: PASS"))

    def test_conservative_pause_must_not_prematurely_finalize(self):
        # Case 2: stale marker + 10-20s generation pause + later text growth.
        tracker = neutral_relay.ResponseCompletionTracker()
        paused = self.snapshot("VERDICT: PASS", soft=True)
        # Growth (text change) only after a 20s pause.
        for now in (0, 5, 10, 15, 20):
            self.assertEqual(tracker.observe(paused, 1, "RID-1", now=now), (False, ""))
        # Even at the 20s pause boundary, CONSERVATIVE (30s) must NOT finalize.
        self.assertEqual(tracker.observe(paused, 1, "RID-1", now=20), (False, ""))
        # When the response grows again, the timer resets (no premature finalize).
        grown = self.snapshot("VERDICT: PASS\nREVIEW_REQUEST_ID: GLR-LME", soft=True)
        self.assertEqual(tracker.observe(grown, 1, "RID-1", now=25), (False, ""))

    def test_changing_streaming_response_never_finalizes(self):
        # Case 3: constantly changing streaming response.
        tracker = neutral_relay.ResponseCompletionTracker()
        for now in range(0, 120, 3):
            complete, text = tracker.observe(
                self.snapshot(f"streaming-{now}", soft=True),
                1,
                "RID-1",
                now=now,
            )
            self.assertFalse(complete)
            self.assertEqual(text, "")

    def test_markers_clear_then_normal_short_settle_applies(self):
        # Case 4: soft markers present, then clear -> NORMAL settle applies.
        tracker = neutral_relay.ResponseCompletionTracker()
        # First two reads carry a soft marker (CONSERVATIVE would need 30s).
        self.assertEqual(tracker.observe(self.snapshot("B", soft=True), 1, "RID-1", now=0), (False, ""))
        self.assertEqual(tracker.observe(self.snapshot("B", soft=True), 1, "RID-1", now=2), (False, ""))
        # Marker clears; NORMAL (8s + 4 reads, B4) now applies to the already-stable text.
        self.assertEqual(tracker.observe(self.snapshot("B", soft=False), 1, "RID-1", now=4), (False, ""))
        self.assertEqual(tracker.observe(self.snapshot("B", soft=False), 1, "RID-1", now=6), (False, ""))
        self.assertEqual(tracker.observe(self.snapshot("B", soft=False), 1, "RID-1", now=8), (True, "B"))

    def test_long_conversation_user_count_has_no_turn_ceiling(self):
        # Case 5: long >20-turn conversation still correlates and finalizes.
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("PASS", user_count=26, soft=False)
        self.assertEqual(tracker.observe(snap, 25, "RID-1", now=0), (False, ""))
        self.assertEqual(tracker.observe(snap, 25, "RID-1", now=2), (False, ""))
        self.assertEqual(tracker.observe(snap, 25, "RID-1", now=4), (False, ""))
        self.assertEqual(tracker.observe(snap, 25, "RID-1", now=6), (False, ""))
        self.assertEqual(tracker.observe(snap, 25, "RID-1", now=8), (True, "PASS"))

    def test_never_stable_response_never_completes_fail_closed(self):
        # Case 6: never-stable response -> observer never completes (caller
        # enforces the timeout and fails closed).
        tracker = neutral_relay.ResponseCompletionTracker()
        for now in range(0, 40, 2):
            complete, text = tracker.observe(
                self.snapshot(f"changing-{now}", soft=True),
                1,
                "RID-1",
                now=now,
            )
            self.assertFalse(complete)
            self.assertEqual(text, "")

    def test_correlation_still_requires_intended_user_turn(self):
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("PASS", user_count=1, last_user_text="OLD-RID", soft=False)
        for now in (0, 2, 4, 10):
            self.assertEqual(tracker.observe(snap, 1, "RID-1", now=now), (False, ""))

    def test_finalization_does_not_require_response_to_echo_request_id(self):
        # Generic transport: the assistant reply need not echo REVIEW_REQUEST_ID.
        tracker = neutral_relay.ResponseCompletionTracker()
        snap = self.snapshot("VERDICT: PASS", soft=False)
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=0), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=2), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=4), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=6), (False, ""))
        self.assertEqual(tracker.observe(snap, 1, "RID-1", now=8), (True, "VERDICT: PASS"))


class TestNeutralRelay(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "config.json")
        self.req_path = os.path.join(self.temp_dir.name, "request.txt")
        self.out_path = os.path.join(self.temp_dir.name, "out.md")

        # Setup valid config
        config = {
            "routes": {
                "test/repo": {
                    "conversation_url": "mock_url",
                    "cdp_port": 1234
                }
            }
        }
        with open(self.config_path, "w") as f:
            json.dump(config, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    class Args:
        def __init__(self, req, out, cfg, dry_run=False, conversation_url=None,
                     cdp_port=None, attachment=None):
            self.request_file = req
            self.output_file = out
            self.config_file = cfg
            self.dry_run = dry_run
            self.conversation_url = conversation_url
            self.cdp_port = cdp_port
            self.attachment = attachment or []
            self.wait_timeout = 60

    def test_default_config_path_uses_governloop_authority(self):
        self.assertEqual(
            neutral_relay.DEFAULT_CONFIG_PATH,
            os.path.expanduser("~/.governloop/relay/config.json"),
        )

    def test_repo_route_parsing_and_dry_run(self):
        # Setup valid request
        with open(self.req_path, "w") as f:
            f.write("REVIEW_REQUEST_ID: 12345\nREPO: test/repo\nPR: 1\nHEAD: abc\n")

        # An explicit --config-file equivalent must continue to override the default.
        args = self.Args(self.req_path, self.out_path, self.config_path, dry_run=True)
        ret = asyncio.run(neutral_relay.run_relay(args))
        
        self.assertEqual(ret, 0)
        self.assertTrue(os.path.exists(self.out_path))
        with open(self.out_path, "r") as f:
            content = f.read()
            self.assertIn("REVIEW_REQUEST_ID: 12345", content)

    def test_unknown_repo_fails_closed(self):
        with open(self.req_path, "w") as f:
            f.write("REVIEW_REQUEST_ID: 12345\nREPO: unknown/repo\nPR: 1\nHEAD: abc\n")

        args = self.Args(self.req_path, self.out_path, self.config_path, dry_run=True)
        ret = asyncio.run(neutral_relay.run_relay(args))
        
        self.assertEqual(ret, 1)
        self.assertFalse(os.path.exists(self.out_path))

    def test_missing_request_id_fails_closed(self):
        with open(self.req_path, "w") as f:
            f.write("REPO: test/repo\nPR: 1\nHEAD: abc\n")

        args = self.Args(self.req_path, self.out_path, self.config_path, dry_run=True)
        ret = asyncio.run(neutral_relay.run_relay(args))
        
        self.assertEqual(ret, 1)
        self.assertFalse(os.path.exists(self.out_path))


if __name__ == '__main__':
    unittest.main()
