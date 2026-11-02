"""Controller failures must retain enough state to clean up the real VM."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cloud


class WorkflowFailureTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name)
        cloud.save(self.results / "deployment.json", {"imageArn": "image", "imageVersion": "1.0"})
        self.state = "RUNNING"
        self.suspensions = 0

    def aws(self, service, operation, *args):
        if operation == "run-microvm":
            return {"microvmId": "vm-one", "endpoint": "vm.invalid"}
        if operation == "create-microvm-auth-token":
            return {"authToken": {"X-aws-proxy-auth": "not-written-to-evidence"}}
        if operation == "get-microvm":
            return {"state": self.state}
        if operation == "suspend-microvm":
            self.suspensions += 1
            if self.suspensions == 2:
                raise subprocess.CalledProcessError(1, [operation], stderr="suspend failed")
            self.state = "SUSPENDED"
        elif operation == "resume-microvm":
            self.state = "RUNNING"
        elif operation == "terminate-microvm":
            self.state = "TERMINATED"
        else:
            raise AssertionError(operation)
        return {}

    def test_failed_second_cycle_keeps_vm_id_and_partial_progress_for_cleanup(self):
        def reply(request, timeout):
            return io.BytesIO(json.dumps({"passed": True, "checks": {"guest": True}}).encode())

        with patch.object(cloud, "aws", self.aws), patch.object(cloud.urllib.request, "urlopen", reply), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                cloud.run(self.results)
            partial = json.loads((self.results / "vm.json").read_text())
            self.assertEqual(partial["microvmId"], "vm-one")
            self.assertEqual(len(partial["cycles"]), 2)
            self.assertEqual(partial["cycles"][0]["resumed"]["state"], "RUNNING")
            self.assertEqual(partial["cycles"][1], {"cycle": 2})
            self.assertFalse((self.results / "termination-armed.json").exists())
            self.assertFalse((self.results / "verification-resumed-2.json").exists())
            cloud.terminate(self.results)
        cleaned = json.loads((self.results / "vm.json").read_text())
        self.assertTrue(cleaned["terminated"])
        self.assertGreaterEqual(cleaned["end"], cleaned["start"])
        self.assertEqual(cleaned["cycles"], partial["cycles"])
        self.assertNotIn("not-written-to-evidence", (self.results / "vm.json").read_text())

    def test_already_removed_vm_records_completed_cleanup(self):
        cloud.save(self.results / "vm.json", {"microvmId": "vm-one", "start": 1})
        missing = subprocess.CalledProcessError(1, ["terminate"], stderr="ResourceNotFoundException")
        with patch.object(cloud, "aws", side_effect=missing):
            cloud.terminate(self.results)
        cleaned = json.loads((self.results / "vm.json").read_text())
        self.assertTrue(cleaned["terminated"])
        self.assertGreater(cleaned["end"], 1)

    def test_transition_timeout_does_not_treat_transitional_state_as_success(self):
        with patch.object(cloud, "aws", return_value={"state": "SUSPENDING"}), patch.object(cloud.time, "monotonic", side_effect=[0, 2]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                cloud.wait("get-microvm", [], "SUSPENDED", 1)


if __name__ == "__main__":
    unittest.main()
