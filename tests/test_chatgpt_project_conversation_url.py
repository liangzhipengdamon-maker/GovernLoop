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


class TestChatGPTProjectConversationURL(unittest.TestCase):
    def _state(self, td):
        sid = "TEST-PROJECT-URL-2026-08-25"
        state = {
            "session_id": sid,
            "repo": "acme/widget",
            "project": "widget",
            "task": "PROJECT-URL",
            "task_source": "test",
            "conversation_url": None,
            "cdp_port": 9233,
            "created_at": "2026-08-25T00:00:00+00:00",
            "updated_at": "2026-08-25T00:00:00+00:00",
            "checkpoints": [],
            "status": "ACTIVE",
        }
        gl.save_state(td, state)
        return state

    def test_direct_conversation_url_still_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            url = "https://chatgpt.com/c/6a8b16ab-3644-83ec-9e38-4d32cbd7549c"
            bound, _ = gl.bind_url(td, state["session_id"], url)
            self.assertIsNotNone(bound)
            self.assertEqual(bound["conversation_url"], url)

    def test_project_conversation_url_is_accepted_and_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            url = (
                "https://chatgpt.com/g/g-p-6a8ad2785a288191a7d4fc6487fbd08e/"
                "c/6a8b16ab-3644-83ec-9e38-4d32cbd7549c"
            )
            bound, _ = gl.bind_url(td, state["session_id"], url)
            self.assertIsNotNone(bound)
            self.assertEqual(bound["conversation_url"], url)

    def test_project_home_without_conversation_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            bound, msg = gl.bind_url(
                td,
                state["session_id"],
                "https://chatgpt.com/g/g-p-6a8ad2785a288191a7d4fc6487fbd08e",
            )
            self.assertIsNone(bound)
            self.assertIn("not a valid ChatGPT conversation URL", msg)

    def test_extra_path_after_conversation_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            bound, _ = gl.bind_url(
                td,
                state["session_id"],
                "https://chatgpt.com/g/g-p-project/c/abc-123/extra",
            )
            self.assertIsNone(bound)


if __name__ == "__main__":
    unittest.main()
