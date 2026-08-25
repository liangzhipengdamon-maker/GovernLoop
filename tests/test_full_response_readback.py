import importlib.util
import os
import tempfile
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "skills", "workbuddy", "governloop", "scripts", "governloop_session.py",
)

spec = importlib.util.spec_from_file_location("governloop_session", MODULE_PATH)
gl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl)


class FullResponseReadbackTests(unittest.TestCase):
    def _state(self, td):
        state = {
            "session_id": "TEST-FULL-READBACK-2026-08-25",
            "repo": "acme/widget",
            "project": "widget",
            "task": "FULL-READBACK",
            "task_source": "test",
            "conversation_url": "https://chatgpt.com/c/abc-123",
            "cdp_port": 9233,
            "created_at": "2026-08-25T00:00:00+00:00",
            "updated_at": "2026-08-25T00:00:00+00:00",
            "checkpoints": [],
            "status": "ACTIVE",
        }
        gl.save_state(td, state)
        return state

    def _stub_relay(self, td, response):
        stub = os.path.join(td, "stub_relay.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                "if '--help' in sys.argv:\n"
                "    print('usage: stub_relay.py --request-file R --output-file O --config-file C')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "out = args[args.index('--output-file') + 1]\n"
                f"open(out, 'w', encoding='utf-8').write({response!r})\n"
                "print('Success: Wrote response to ' + out)\n"
            )
        return stub

    def test_checkpoint_stdout_contains_complete_response(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            response = "BEGIN-REPLY\n" + ("0123456789" * 120) + "\nEND-REPLY"
            relay = self._stub_relay(td, response)

            ok, text, code = gl.run_checkpoint(
                td,
                cwd=td,
                ctype="REVIEW_REQUIRED",
                message="test full response readback",
                relay_path=relay,
                state=state,
            )

            self.assertTrue(ok, text)
            self.assertEqual(code, 0)
            self.assertIn("RESPONSE_BEGIN\n", text)
            self.assertIn("\nRESPONSE_END", text)
            self.assertIn(response, text)
            self.assertNotIn("RESPONSE (head)", text)

    def test_agent_does_not_need_temp_response_path_for_readback(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            response = "complete assistant reply"
            relay = self._stub_relay(td, response)

            ok, text, code = gl.run_checkpoint(
                td,
                cwd=td,
                ctype="FINAL_VERIFICATION",
                relay_path=relay,
                state=state,
            )

            self.assertTrue(ok, text)
            self.assertEqual(code, 0)
            self.assertIn(response, text)
            self.assertNotIn("governloop-response-", text)


if __name__ == "__main__":
    unittest.main()
