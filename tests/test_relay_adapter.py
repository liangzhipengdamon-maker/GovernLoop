import unittest
import json
import os
import sys
import tempfile

# Ensure scripts can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))
import relay_adapter

class TestRelayAdapter(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory to strictly isolate tests from real .agent-bridge
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["AGENT_BRIDGE_DIR"] = self.temp_dir.name
        
        self.status_file = relay_adapter.get_status_file()
        self.review_file = relay_adapter.get_review_file()
        
        # We don't touch the real bridge directory at all.
        self.initial_status = {
            "protocol_version": "1",
            "state": "REVIEW_REQUESTED",
            "repo": "liangzhipengdamon-maker/Agent-Ops",
            "pr": 5,
            "head": "abcdef123456",
            "request": "independent_review"
        }
        with open(self.status_file, "w") as f:
            json.dump(self.initial_status, f)

    def tearDown(self):
        # Clean up isolated temp directory
        self.temp_dir.cleanup()
        if "AGENT_BRIDGE_DIR" in os.environ:
            del os.environ["AGENT_BRIDGE_DIR"]

    def test_review_request_transitions_to_waiting(self):
        relay_adapter.handle_review_request()
        with open(self.status_file, "r") as f:
            status = json.load(f)
        self.assertEqual(status["state"], "WAITING_FOR_REVIEW")

    def test_no_default_bridge_dir_fails_fast(self):
        # The legacy '.agent-bridge' default was removed (chore/remove-agent-
        # bridge): without AGENT_BRIDGE_DIR the adapter must fail fast instead
        # of silently using a dangling default path.
        saved = os.environ.pop("AGENT_BRIDGE_DIR", None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                relay_adapter.get_bridge_dir()
            self.assertIn("AGENT_BRIDGE_DIR is required", str(ctx.exception))
            # The file getters propagate the same fail-fast.
            with self.assertRaises(RuntimeError):
                relay_adapter.get_status_file()
        finally:
            if saved is not None:
                os.environ["AGENT_BRIDGE_DIR"] = saved

    def test_gpt_review_triple_head_match_pass(self):
        relay_adapter.handle_review_request()
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        req_id = status["request_id"]
        
        with open(self.review_file, "w") as f:
            f.write(f"REVIEW_REQUEST_ID: {req_id}\nVERDICT: PASS\nPR: 5\nHEAD: abcdef123456\nSUMMARY:\nLooks good.\nACTIONS:\n")
            
        # Provide matching current_head
        relay_adapter.handle_gpt_review_return(current_head="abcdef123456")
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        self.assertEqual(status["state"], "WAITING_PO_AUTH")

    def test_gpt_review_missing_current_head_fails_closed(self):
        relay_adapter.handle_review_request()
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        req_id = status["request_id"]
        
        with open(self.review_file, "w") as f:
            f.write(f"REVIEW_REQUEST_ID: {req_id}\nVERDICT: PASS\nPR: 5\nHEAD: abcdef123456\n")
            
        # Provide NO current_head
        relay_adapter.handle_gpt_review_return(current_head=None)
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        # Rejects review, does not transition
        self.assertEqual(status["state"], "WAITING_FOR_REVIEW")

    def test_stale_review_rejected_review_head_mismatch(self):
        relay_adapter.handle_review_request()
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        req_id = status["request_id"]
        
        with open(self.review_file, "w") as f:
            f.write(f"REVIEW_REQUEST_ID: {req_id}\nVERDICT: PASS\nPR: 5\nHEAD: 000000000000\n")
        
        relay_adapter.handle_gpt_review_return(current_head="abcdef123456")
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        self.assertEqual(status["state"], "WAITING_FOR_REVIEW")

    def test_stale_review_rejected_current_head_mismatch(self):
        relay_adapter.handle_review_request()
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        req_id = status["request_id"]
        
        with open(self.review_file, "w") as f:
            f.write(f"REVIEW_REQUEST_ID: {req_id}\nVERDICT: PASS\nPR: 5\nHEAD: abcdef123456\n")
        
        # The remote GitHub PR HEAD drifted!
        relay_adapter.handle_gpt_review_return(current_head="drifted12345")
        
        with open(self.status_file, "r") as f:
            status = json.load(f)
        # Should stay WAITING_FOR_REVIEW, effectively rejecting the stale PASS verdict
        self.assertEqual(status["state"], "WAITING_FOR_REVIEW")

if __name__ == '__main__':
    unittest.main()
