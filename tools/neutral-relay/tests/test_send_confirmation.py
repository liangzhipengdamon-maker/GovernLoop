import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

# Ensure neutral_relay can be imported (tests live in tools/neutral-relay/tests/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay


class FakeCDP:
    """Base fake: click bookkeeping + static options. Sequence-aware probing is
    implemented by FakeSequenced (the tests all use that subclass)."""

    def __init__(self, *, click_ok=True, cleared=None, users=None, assistants=None,
                 streaming=False):
        self.click_ok = click_ok
        self.clicks = 0
        self.cleared = cleared if cleared is not None else [True]
        self.users = users if users is not None else [0]
        self.assistants = assistants if assistants is not None else [0]
        self.streaming = streaming

    async def click_send(self):
        self.clicks += 1
        return self.click_ok


class VirtualClock:
    """Injected clock for SendConfirmation timeouts. Each awaited sleep ticks
    the clock forward by 1s, so confirm_timeout/pending_timeout are driven by
    iteration count instead of wall-clock time (fast, deterministic tests)."""

    def __init__(self):
        self._t = 0.0

    def __call__(self):
        return self._t

    async def tick(self, _seconds=1.0):
        self._t += 1.0


def make_confirmation(fake, *, confirm_timeout=10, pending_timeout=10):
    """Real SendConfirmation wired to a FakeSequenced with a virtual clock."""
    clock = VirtualClock()
    conf = neutral_relay.SendConfirmation(
        click_send=fake.click_send,
        composer_cleared=fake.composer_cleared,
        turn_counts=fake.turn_counts,
        assistant_streaming=fake.assistant_streaming,
        confirm_timeout=confirm_timeout,
        pending_timeout=pending_timeout,
        ui_transition_seconds=0.0,
        sleep=clock.tick,
        now=clock,
    )
    return conf, fake, clock


class FakeSequenced(FakeCDP):
    """Sequence-aware fake: cleared/users/assistants consumed per call."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._ci = 0
        self._ui = 0
        self._ai = 0

    @staticmethod
    def _at(seq, i):
        return seq[i] if i < len(seq) else seq[-1]

    async def composer_cleared(self):
        v = self._at(self.cleared, self._ci)
        self._ci += 1
        return v

    async def turn_counts(self):
        u = self._at(self.users, self._ui)
        a = self._at(self.assistants, self._ai)
        self._ui += 1
        self._ai += 1
        return u, a

    async def assistant_streaming(self):
        return self.streaming


def run(fake, user_before=9, **kw):
    conf, fake, _clock = make_confirmation(fake, **kw)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(conf.confirm(user_before))
    return result, buf.getvalue(), fake


class TestSendConfirmation(unittest.TestCase):

    def test_first_click_swallowed_composer_remains_then_safe_reclick(self):
        # 1) first click swallowed: composer non-empty for the whole first
        #    confirm window -> one safe re-click -> composer clears + user +1.
        fake = FakeSequenced(
            cleared=[False] * 10 + [True] * 10,
            users=[9] * 10 + [10] * 10,
            assistants=[3] * 20,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertTrue(delivered)
        self.assertTrue(primary)
        self.assertEqual(status, "DELIVERY_CONFIRMED_PRIMARY")
        self.assertEqual(f.clicks, 2)  # exactly one safe re-click
        self.assertIn("SEND_DRAFT_STILL_PRESENT", out)

    def test_second_attempt_still_nonempty_send_not_confirmed(self):
        # 2) after the safe re-click the composer is STILL non-empty ->
        #    SEND_NOT_CONFIRMED, fail closed.
        fake = FakeSequenced(
            cleared=[False] * 100,
            users=[9] * 100,
            assistants=[3] * 100,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertFalse(delivered)
        self.assertEqual(status, "SEND_NOT_CONFIRMED")
        self.assertEqual(f.clicks, 2)  # initial + one re-click, never more

    def test_composer_clears_user_delayed_send_pending(self):
        # 3) composer clears but the user turn is delayed -> SEND_PENDING
        #    (no re-click), later user +1 -> PRIMARY.
        fake = FakeSequenced(
            cleared=[True],
            users=[9] * 5 + [10] * 20,
            assistants=[3] * 20,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertTrue(delivered)
        self.assertTrue(primary)
        self.assertEqual(status, "DELIVERY_CONFIRMED_PRIMARY")
        self.assertIn("SEND_PENDING", out)
        self.assertEqual(f.clicks, 1)  # never re-clicked after composer cleared

    def test_pending_later_user_plus1_primary_pass(self):
        # 4) pending window: user turn eventually +1 -> PRIMARY PASS.
        fake = FakeSequenced(
            cleared=[True],
            users=[9, 9, 9, 10],
            assistants=[3] * 10,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertTrue(delivered)
        self.assertTrue(primary)
        self.assertEqual(status, "DELIVERY_CONFIRMED_PRIMARY")
        self.assertIn("DELIVERY_CONFIRMED_PRIMARY: PASS", out)

    def test_pending_new_assistant_turn_no_prior_streaming_auxiliary(self):
        # 5) pending window: no user +1, but a NEW assistant turn appears and
        #    nothing was streaming before the send -> AUXILIARY PASS.
        fake = FakeSequenced(
            cleared=[True],
            users=[9] * 10,
            assistants=[3, 3, 4],   # baseline 3, then a new turn -> 4
            streaming=False,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertTrue(delivered)
        self.assertFalse(primary)
        self.assertEqual(status, "DELIVERY_CONFIRMED_AUXILIARY")
        self.assertIn("DELIVERY_CONFIRMED_AUXILIARY: PASS", out)

    def test_prior_assistant_streaming_rejects_auxiliary_signal(self):
        # 6) an assistant turn WAS streaming before the send -> the auxiliary
        #    signal is rejected -> pending eventually times out.
        fake = FakeSequenced(
            cleared=[True],
            users=[9] * 10,
            assistants=[3, 3, 4],   # would trip the naive count check
            streaming=True,         # ...but streaming-before guard rejects it
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertFalse(delivered)
        self.assertEqual(status, "SEND_PENDING_TIMEOUT")
        self.assertIn("SEND_PENDING_TIMEOUT", out)

    def test_pending_timeout_no_resend(self):
        # 7) pending window exhausts with no signal -> SEND_PENDING_TIMEOUT,
        #    and the relay does NOT retry send (single click only).
        fake = FakeSequenced(
            cleared=[True],
            users=[9] * 50,
            assistants=[3] * 50,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertFalse(delivered)
        self.assertEqual(status, "SEND_PENDING_TIMEOUT")
        self.assertEqual(f.clicks, 1)  # no re-click after composer cleared
        self.assertIn("will NOT retry Send", out)

    def test_never_reclick_after_composer_cleared(self):
        # 8) explicit: once the composer is cleared the state machine must
        #    never click send again, no matter how long it waits.
        fake = FakeSequenced(
            cleared=[True],
            users=[9] * 50,
            assistants=[3] * 50,
            streaming=True,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertEqual(status, "SEND_PENDING_TIMEOUT")
        self.assertEqual(f.clicks, 1)

    def test_manual_recovery_guidance_no_rerun_same_request(self):
        # 9) SEND_NOT_CONFIRMED guidance must NOT tell the user to re-run the
        #    same request (duplicate-delivery risk); it must say DO NOT re-run.
        fake = FakeSequenced(
            cleared=[False] * 100,
            users=[9] * 100,
            assistants=[3] * 100,
        )
        (delivered, primary, status), out, f = run(fake)
        self.assertEqual(status, "SEND_NOT_CONFIRMED")
        self.assertNotIn("re-run with the same request", out)
        self.assertIn("DO NOT re-run the send path for the same request", out)
        self.assertIn("DELIVERY_MODE=MANUAL_SEND_RECOVERY", out)

    def test_send_button_unavailable(self):
        fake = FakeSequenced(click_ok=False, cleared=[False], users=[9], assistants=[3])
        (delivered, primary, status), out, f = run(fake)
        self.assertFalse(delivered)
        self.assertEqual(status, "SEND_BUTTON_UNAVAILABLE")
        self.assertEqual(f.clicks, 1)


if __name__ == "__main__":
    unittest.main()


class TestSendConfirmationReconciliation(TestSendConfirmation):
    """B1: request-correlated read-back reconciliation in SEND_PENDING."""

    def _conf(self, fake, snap, req_id, pending_timeout=10):
        async def _snap():
            return snap
        conf = neutral_relay.SendConfirmation(
            click_send=fake.click_send,
            composer_cleared=fake.composer_cleared,
            turn_counts=fake.turn_counts,
            assistant_streaming=fake.assistant_streaming,
            confirm_timeout=1,
            pending_timeout=pending_timeout,
            ui_transition_seconds=0.0,
            snapshot=_snap,
            req_id=req_id,
        )
        return conf

    def test_pending_request_correlated_readback_reconciles(self):
        # composer cleared, no user-turn/assistant-count signal, but the
        # REQUEST-CORRELATED read-back is observed (REVIEW_REQUEST_ID in the
        # thread's last user message + a settled assistant reply) ->
        # DELIVERY_CONFIRMED_RECONCILED. Never re-clicked.
        req_id = "WS-A65-PRODUCT-CLOSURE-E2E-2026-08-24-BEFORE_DESTRUCTIVE_ACTION-1"
        snap = {
            "userCount": 9,
            "lastUserText": f"evidence.txt 文档 REVIEW_REQUEST_ID: {req_id} REPO: ws CHECKPOINT: BEFORE_DESTRUCTIVE_ACTION",
            "text": '{ "verdict": "BLOCK", "confidence": "high", "rationale": "ok", "required_fixes": [] }',
            "hasAssistant": True,
            "softGenerating": False,
            "hasCopyRate": True,   # B4 F1: completion features gate finalize
            "stopPresent": False,
        }
        fake = FakeSequenced(cleared=[True], users=[9] * 30, assistants=[3] * 30, streaming=False)
        conf = self._conf(fake, snap, req_id, pending_timeout=12)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = asyncio.run(conf.confirm(9))
        delivered, primary, status = result
        self.assertTrue(delivered)
        self.assertFalse(primary)
        self.assertEqual(status, "DELIVERY_CONFIRMED_RECONCILED")
        self.assertIn("DELIVERY_CONFIRMED_RECONCILED: PASS", buf.getvalue())
        self.assertEqual(fake.clicks, 1)  # no re-click after composer cleared

    def test_pending_unrelated_assistant_message_does_not_reconcile(self):
        # safety boundary: an assistant reply WITHOUT our REVIEW_REQUEST_ID in
        # the thread's last user message must NOT count as delivery proof ->
        # SEND_PENDING_TIMEOUT (fail-closed, no resend, no false positive).
        req_id = "REQ-123"
        snap = {
            "userCount": 9,
            "lastUserText": "some unrelated conversation message",
            "text": "an unrelated assistant reply",
            "hasAssistant": True,
            "softGenerating": False,
            "hasCopyRate": True,
            "stopPresent": False,
        }
        fake = FakeSequenced(cleared=[True], users=[9] * 30, assistants=[3] * 30, streaming=False)
        conf = self._conf(fake, snap, req_id, pending_timeout=3)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = asyncio.run(conf.confirm(9))
        delivered, primary, status = result
        self.assertFalse(delivered)
        self.assertEqual(status, "SEND_PENDING_TIMEOUT")
        self.assertEqual(fake.clicks, 1)
