"""Failure-path regressions for the live delivery scenario controller."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import run_delivery


class RunnerFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "delivery-run.json"
        self.client = Mock()
        self.runner = run_delivery.Runner(self.client, Mock(), "image", "1.0", "role",
                                         Path(directory.name) / "events.log", self.output)

    def test_failed_health_probe_restores_intake_and_retains_fault_evidence(self):
        mode = "normal"
        requests = []

        def app(path, data=None):
            nonlocal mode
            if path == "/delivery/fault":
                mode = data["mode"]
            elif path == "/delivery/state":
                at = time.time()
                requests.extend({"pipeline": pipeline, "mode": mode, "outcome": "injected-http503",
                                 "status": 503, "receivedAt": at, "completedAt": at}
                                for pipeline in ("logs", "metrics"))
            return {"requests": list(requests)}

        self.runner.app = app
        self.runner.emit = Mock(return_value={"marker": "offered"})
        self.runner.health = Mock(side_effect=TimeoutError("unresponsive app"))
        with self.assertRaisesRegex(TimeoutError, "unresponsive app"):
            self.runner.recovery("http503")
        self.assertEqual(mode, "normal")
        saved = json.loads(self.output.read_text())["recoveries"][0]
        self.assertEqual({request["pipeline"] for request in saved["requests"]}, {"logs", "metrics"})
        self.assertGreaterEqual(saved["restoredAt"], saved["startedAt"])
        self.assertEqual(self.runner.evidence["samples"], [])

    def test_delivery_failure_cleans_up_vm_and_keeps_failed_offline_verdict(self):
        self.client.run_microvm.return_value = {"microvmId": "microvm-test"}
        self.client.get_microvm.return_value = {"state": "RUNNING", "endpoint": "example.invalid"}
        self.client.create_microvm_auth_token.return_value = {"authToken": {"X-aws-proxy-auth": "secret"}}
        self.runner.wait_state = Mock(side_effect=[{"state": "RUNNING", "at": time.time()},
                                                  {"state": "TERMINATED", "at": time.time()}])
        self.runner.emit = Mock(side_effect=TimeoutError("automatic delivery failed"))
        with contextlib.redirect_stdout(io.StringIO()):
            status = self.runner.run()
        self.assertEqual(status, 1)
        self.client.terminate_microvm.assert_called_once_with(microvmIdentifier="microvm-test")
        saved = json.loads(self.output.read_text())
        self.assertTrue(saved["vm"]["terminated"])
        self.assertIn("automatic delivery failed", saved["error"])
        verdict = json.loads(self.output.with_name("delivery-run-evaluation.json").read_text())
        self.assertFalse(verdict["passed"])
        self.assertNotIn("secret", self.output.read_text())


if __name__ == "__main__":
    unittest.main()
