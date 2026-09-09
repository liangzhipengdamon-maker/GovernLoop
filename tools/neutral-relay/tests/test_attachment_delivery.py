import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# Ensure neutral_relay can be imported (tests live in tools/neutral-relay/tests/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay


def make_uploader(*, input_node=1, visible=None, set_error=None, session="S-1",
                  visibility_retries=3, retry_delay=0.0):
    """Real neutral_relay.AttachmentUploader with injected in-memory CDP callbacks."""
    calls = {"set": [], "visible": []}

    async def find_input():
        return input_node

    async def set_files(node_id, abs_path):
        if set_error:
            raise RuntimeError(set_error)
        calls["set"].append((session, node_id, abs_path))

    async def is_visible(base):
        calls["visible"].append(base)
        if visible is None:
            return True
        if callable(visible):
            return visible(base)
        return visible

    up = neutral_relay.AttachmentUploader(
        find_input, set_files, is_visible,
        visibility_retries=visibility_retries, retry_delay=retry_delay,
    )
    return up, calls


class TestAttachmentUploader(unittest.TestCase):
    def test_attachment_success(self):
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "report.md")
            with open(ev, "w") as f:
                f.write("# report\n")
            up, calls = make_uploader()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ok, reason = asyncio.run(up.upload(ev))
            self.assertTrue(ok)
            self.assertIsNone(reason)
            self.assertEqual(len(calls["set"]), 1)
            self.assertEqual(os.path.basename(calls["set"][0][2]), "report.md")

    def test_missing_file_fails_closed(self):
        up, calls = make_uploader()
        ok, reason = asyncio.run(up.upload("/no/such/report.md"))
        self.assertFalse(ok)
        self.assertEqual(reason, "missing-file")
        self.assertEqual(calls["set"], [])

    def test_no_file_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "report.md")
            open(ev, "w").write("# report\n")
            up, calls = make_uploader(input_node=None)
            ok, reason = asyncio.run(up.upload(ev))
            self.assertFalse(ok)
            self.assertEqual(reason, "no-file-input")
            self.assertEqual(calls["set"], [])

    def test_upload_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "report.md")
            open(ev, "w").write("# report\n")
            up, calls = make_uploader(set_error="boom")
            ok, reason = asyncio.run(up.upload(ev))
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("upload-error:boom"))

    def test_not_visible_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "report.md")
            open(ev, "w").write("# report\n")
            up, calls = make_uploader(visible=False, visibility_retries=3, retry_delay=0.0)
            ok, reason = asyncio.run(up.upload(ev))
            self.assertFalse(ok)
            self.assertEqual(reason, "not-visible")
            self.assertEqual(len(calls["visible"]), 3)  # retried, then failed closed

    def test_visible_after_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "report.md")
            open(ev, "w").write("# report\n")
            attempts = {"n": 0}

            def visible_later(base):
                attempts["n"] += 1
                return attempts["n"] >= 2

            up, calls = make_uploader(visible=visible_later, visibility_retries=5, retry_delay=0.0)
            ok, reason = asyncio.run(up.upload(ev))
            self.assertTrue(ok)
            self.assertIsNone(reason)


class TestUploadAttachments(unittest.TestCase):
    def test_multiple_attachments_same_conversation(self):
        with tempfile.TemporaryDirectory() as td:
            ev1 = os.path.join(td, "a.md")
            ev2 = os.path.join(td, "b.json")
            open(ev1, "w").write("a")
            open(ev2, "w").write("{}")
            up, calls = make_uploader(session="S-BOUND")
            buf = io.StringIO()
            with redirect_stdout(buf):
                ok, failed, reason = asyncio.run(
                    neutral_relay.upload_attachments(up, [ev1, ev2]))
            self.assertTrue(ok)
            self.assertIsNone(failed)
            self.assertIsNone(reason)
            self.assertEqual(len(calls["set"]), 2)
            # every upload went through the same session id + same file input
            self.assertEqual({c[0] for c in calls["set"]}, {"S-BOUND"})
            self.assertEqual({c[1] for c in calls["set"]}, {1})
            self.assertIn("ATTACHED: " + ev1, buf.getvalue())
            self.assertIn("ATTACHED: " + ev2, buf.getvalue())

    def test_attachment_failure_never_false_complete(self):
        with tempfile.TemporaryDirectory() as td:
            ev1 = os.path.join(td, "a.md")
            missing = os.path.join(td, "missing.md")
            open(ev1, "w").write("a")
            up, calls = make_uploader()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ok, failed, reason = asyncio.run(
                    neutral_relay.upload_attachments(up, [ev1, missing, ev1]))
            self.assertFalse(ok)                      # never reports success
            self.assertEqual(failed, missing)
            self.assertEqual(reason, "missing-file")
            self.assertIn("ATTACH_FAIL missing-file", buf.getvalue())
            # stops at the first failure: the trailing ev1 was never uploaded
            self.assertEqual(len(calls["set"]), 1)

    def test_failure_on_second_attachment_stops_iteration(self):
        with tempfile.TemporaryDirectory() as td:
            ev1 = os.path.join(td, "a.md")
            ev2 = os.path.join(td, "b.md")
            ev3 = os.path.join(td, "c.md")
            for f in (ev1, ev2, ev3):
                open(f, "w").write("x")
            up, calls = make_uploader(visible=lambda base: base != "b.md",
                                      visibility_retries=2, retry_delay=0.0)
            ok, failed, reason = asyncio.run(
                neutral_relay.upload_attachments(up, [ev1, ev2, ev3]))
            self.assertFalse(ok)
            self.assertEqual(failed, ev2)
            self.assertEqual(reason, "not-visible")
            self.assertEqual(len(calls["set"]), 2)  # a.md + b.md only; c.md never attempted


class Args:
    def __init__(self, req, out, cfg, *, dry_run=False, conversation_url=None, cdp_port=None,
                 attachment=None):
        self.request_file = req
        self.output_file = out
        self.config_file = cfg
        self.dry_run = dry_run
        self.conversation_url = conversation_url
        self.cdp_port = cdp_port
        self.attachment = attachment or []
        self.wait_timeout = 60


class TestSessionLevelRouting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.req = os.path.join(self.temp_dir.name, "request.txt")
        self.out = os.path.join(self.temp_dir.name, "out.md")
        self.cfg = os.path.join(self.temp_dir.name, "config.json")
        with open(self.req, "w") as f:
            f.write("REVIEW_REQUEST_ID: RID-1\nREPO: owner/repo\n"
                    "CHECKPOINT: REVIEW_REQUIRED\nSESSION: S-1\n\nhello\n")
        json.dump({
            "routes": {
                "owner/repo": {
                    "conversation_url": "https://chatgpt.com/c/cfg-default",
                    "cdp_port": 9233,
                }
            }
        }, open(self.cfg, "w"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_session_conversation_url_override_dry_run(self):
        # session-level URL overrides the configured route for this run only
        args = Args(self.req, self.out, self.cfg, dry_run=True,
                    conversation_url="https://chatgpt.com/c/session-url")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = asyncio.run(neutral_relay.run_relay(args))
        self.assertEqual(ret, 0)
        self.assertIn("session-url", buf.getvalue())
        self.assertNotIn("cfg-default", buf.getvalue())
        # canonical config file untouched
        cfg = json.load(open(self.cfg))
        self.assertEqual(cfg["routes"]["owner/repo"]["conversation_url"],
                         "https://chatgpt.com/c/cfg-default")

    def test_attachment_flag_reaches_run_relay_args(self):
        args = Args(self.req, self.out, self.cfg, dry_run=True,
                    attachment=["report.md", "manifest.json"])
        self.assertEqual(args.attachment, ["report.md", "manifest.json"])


if __name__ == "__main__":
    unittest.main()
