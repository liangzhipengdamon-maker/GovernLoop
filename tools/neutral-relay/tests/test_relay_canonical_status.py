import os
import sys
import unittest

# Ensure neutral_relay can be imported (tests live in tools/neutral-relay/tests/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay as nr


class TestCanonicalStatusConstants(unittest.TestCase):
    """Requirement 6: the public canonical tokens must exist verbatim so a
    checkpoint session / CI can assert on them."""

    def test_tokens_defined(self):
        self.assertEqual(nr.STATUS_TARGET_NOT_CONFIRMED, "TARGET_NOT_CONFIRMED")
        self.assertEqual(nr.STATUS_INJECTION_NOT_CONFIRMED, "INJECTION_NOT_CONFIRMED")
        self.assertEqual(nr.STATUS_DELIVERY_CONFIRMED, "DELIVERY_CONFIRMED")
        self.assertEqual(nr.STATUS_NOT_SENT, "NOT_SENT")
        self.assertEqual(nr.STATUS_POSSIBLY_SENT_UNCONFIRMED, "POSSIBLY_SENT_UNCONFIRMED")


class TestRelayInjectionStatus(unittest.TestCase):
    """Requirement 2/4/6: the Phase-A decision maps to a canonical token, or
    None when the relay may proceed to Phase B (composer positively identified
    AND text written AND verified)."""

    def test_target_not_confirmed(self):
        # 0 or >1 trustworthy composers -> fail closed at preflight.
        self.assertEqual(
            nr.relay_injection_status(False, "no-trustworthy-chat-composer", False, False),
            nr.STATUS_TARGET_NOT_CONFIRMED)
        self.assertEqual(
            nr.relay_injection_status(False, "ambiguous-multiple-chat-composers", False, False),
            nr.STATUS_TARGET_NOT_CONFIRMED)

    def test_injection_not_confirmed_write(self):
        # Composer identified, but the write refused -> not verified.
        self.assertEqual(
            nr.relay_injection_status(True, None, False, False),
            nr.STATUS_INJECTION_NOT_CONFIRMED)

    def test_injection_not_confirmed_verify(self):
        # Composer identified and written, but verification failed -> rolled back.
        self.assertEqual(
            nr.relay_injection_status(True, None, True, False),
            nr.STATUS_INJECTION_NOT_CONFIRMED)

    def test_proceed_when_all_ok(self):
        self.assertIsNone(
            nr.relay_injection_status(True, None, True, True))


class TestDeliveryCanonicalStatus(unittest.TestCase):
    """Requirement 6/7: collapse the DELIVERY_CONFIRMED_* family to the canonical
    token; distinguish composer-cleared-but-thread-unconfirmed from a draft that
    never left the composer."""

    def test_delivery_confirmed_family(self):
        for s in ("DELIVERY_CONFIRMED_PRIMARY", "DELIVERY_CONFIRMED_AUXILIARY",
                  "DELIVERY_CONFIRMED_RECONCILED"):
            self.assertEqual(nr.delivery_canonical_status(s), nr.STATUS_DELIVERY_CONFIRMED)

    def test_possibly_sent_unconfirmed(self):
        # Composer cleared, thread not yet confirmed -> ambiguous evidence. NOT
        # a false COMPLETE, not a resend trigger.
        self.assertEqual(nr.delivery_canonical_status("SEND_PENDING_TIMEOUT"),
                         nr.STATUS_POSSIBLY_SENT_UNCONFIRMED)

    def test_not_sent_draft_present(self):
        # Draft still present (no click accepted) -> genuinely not sent.
        self.assertEqual(nr.delivery_canonical_status("SEND_NOT_CONFIRMED"),
                         nr.STATUS_NOT_SENT)
        self.assertEqual(nr.delivery_canonical_status("SEND_BUTTON_UNAVAILABLE"),
                         nr.STATUS_NOT_SENT)


if __name__ == "__main__":
    unittest.main()
