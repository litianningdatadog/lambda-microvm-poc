"""Credential-free cloud orchestration regressions; AWS and HTTPS stay mocked."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import check
import cloud


class FakeCloud:
    """Model platform hooks, read-only state, and verification at external boundaries."""

    def __init__(self, baselines, startup_failure=None, state_failure=None, state_change=None):
        self.baselines = baselines
        self.startup_failure = startup_failure
        self.state_failure = state_failure
        self.state_change = state_change or {}
        self.vms = {}
        self.calls = []
        self.requests = []
        self.termination_attempts = []
        self.termination_failures = set()

    def aws(self, service, operation, *args):
        if service != "lambda-microvms":
            raise AssertionError(f"Unexpected service: {service}")
        self.calls.append((operation, args))
        if operation == "run-microvm":
            number = len(self.vms) + 1
            microvm_id = f"microvm-{number}"
            self.vms[microvm_id] = {
                "status": "FAILED" if number == self.startup_failure else "RUNNING",
                "endpoint": f"{microvm_id}.example",
                "state": {
                    "prepared": True, "runCount": 1, "suspendCount": 0, "resumeCount": 0,
                    "terminateCount": 0, "verificationCount": 0, "bundledOpenSSL": False,
                    "before": {"runtimeId": self.baselines[number - 1], "rcClientId": None},
                    "run": {"runtimeId": f"runtime-{number}", "rcClientId": None},
                    "runtimeId": f"runtime-{number}", "rcClientId": None,
                    **copy.deepcopy(self.state_change),
                },
            }
            # No endpoint in this response: inventory must survive even a startup failure.
            return {"microvmId": microvm_id}
        microvm_id = args[args.index("--microvm-identifier") + 1]
        vm = self.vms[microvm_id]
        if operation == "get-microvm":
            return {"state": vm["status"], "endpoint": vm["endpoint"]}
        if operation == "create-microvm-auth-token":
            if args[args.index("--allowed-ports") + 1] != '[{"port":8080}]':
                raise AssertionError("Tokens must be scoped to the guest port")
            return {"authToken": {"X-aws-proxy-auth": f"secret-auth-token-{microvm_id}"}}
        if operation == "terminate-microvm":
            self.termination_attempts.append(microvm_id)
            if microvm_id in self.termination_failures:
                self.termination_failures.remove(microvm_id)
                raise subprocess.CalledProcessError(1, ["aws"], stderr="temporary termination failure")
            vm["status"] = "TERMINATED"
            vm["state"]["terminateCount"] += 1
            return {}
        if operation == "suspend-microvm":
            vm["status"] = "SUSPENDED"
            vm["state"]["suspendCount"] += 1
            return {}
        if operation == "resume-microvm":
            vm["status"] = "RUNNING"
            vm["state"]["resumeCount"] += 1
            return {}
        raise AssertionError(f"Unexpected operation: {operation}")

    def urlopen(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        microvm_id = url.hostname.removesuffix(".example")
        if (url.scheme != "https" or timeout != 90
                or request.get_header("X-aws-proxy-auth") != f"secret-auth-token-{microvm_id}"
                or request.get_header("X-aws-proxy-port") != "8080"):
            raise AssertionError("Guest requests must use HTTPS and in-memory scoped tokens")
        self.requests.append((microvm_id, request.get_method(), url.path))
        vm = self.vms[microvm_id]
        if vm["status"] != "RUNNING":
            raise AssertionError("Guest must be running")
        if url.path == "/state" and request.get_method() == "GET":
            if microvm_id == self.state_failure:
                raise OSError("state capture failed")
            result = vm["state"]
        elif url.path == "/verify" and request.get_method() == "POST" and request.data == b"{}":
            vm["state"]["verificationCount"] += 1
            result = self.verification(microvm_id)
        else:
            raise AssertionError("Only GET /state and selected POST /verify are permitted")
        return io.StringIO(json.dumps(result))

    def verification(self, microvm_id):
        state = copy.deepcopy(self.vms[microvm_id]["state"])
        resumed = state["resumeCount"] > 0
        seed = int(microvm_id.split("-")[-1]) * 10 + state["verificationCount"]
        ids = [{"traceId": str((seed << 64) + seed * 1000 + index),
                "spanId": str(seed * 1000 + index)} for index in range(1, 9 if resumed else 137)]
        roots = {name: "verify.js." + name for name in sorted(check.ROOT_CASES)}
        return {
            "schema": 1, "version": "6.15.0", "passed": True,
            "checks": dict.fromkeys(check.REQUIRED_CHECKS, True), "state": state,
            "evidence": {
                "before": state["before"], "run": state["run"],
                "after": {"runtimeId": state["runtimeId"], "rcClientId": state["rcClientId"]},
                "ids": ids,
                "features": {"roots": roots, "errorOperation": roots["expectedError"],
                             "rootSpans": [{**value, "name": name}
                                           for name, value in zip(roots.values(), ids[-8:])]},
                "spans": [{"name": operation, "service": check.SERVICE,
                           "error": int(name == "expectedError")} for name, operation in roots.items()],
            },
        }


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name) / "results"
        self.results.mkdir()
        self.deployment = {"imageArn": "image", "imageVersion": "2.0", "version": "Run-1", "start": 100}
        cloud.save(self.results / "deployment.json", self.deployment)

    def load(self, name, results=None):
        return json.loads(((results or self.results) / name).read_text())

    def run_with(self, platform, results=None):
        with patch.object(cloud, "aws", side_effect=platform.aws), \
                patch.object(cloud.urllib.request, "urlopen", side_effect=platform.urlopen):
            cloud.run(results or self.results)

    def assert_cleaned(self, platform, numbers, results=None):
        for number in numbers:
            microvm_id = f"microvm-{number}"
            self.assertEqual(platform.vms[microvm_id]["status"], "TERMINATED")
            evidence = self.load(f"candidates/vm{number}.json", results)
            self.assertEqual(evidence["microvmId"], microvm_id)
            self.assertIs(evidence["terminated"], True)
            self.assertIsInstance(evidence["terminatedAt"], (int, float))

    def test_first_matching_pair_only_runs_three_verifications(self):
        platform = FakeCloud(["A", "B", "A", "A"])
        self.run_with(platform)
        selection = self.load("selection.json")
        self.assertEqual(selection, {
            "status": "matched", "limit": 6, "selected": [1, 3],
            "candidates": [{"number": number, "microvmId": f"microvm-{number}", "beforeRuntimeId": before}
                           for number, before in enumerate(("A", "B", "A"), 1)],
        })
        self.assertEqual(platform.requests, [
            ("microvm-1", "GET", "/state"), ("microvm-2", "GET", "/state"),
            ("microvm-3", "GET", "/state"), ("microvm-1", "POST", "/verify"),
            ("microvm-3", "POST", "/verify"), ("microvm-1", "POST", "/verify"),
        ])
        self.assertEqual(len(platform.vms), 3)
        for number in (1, 2, 3):
            self.assertEqual(self.load(f"candidates/state{number}.json")["verificationCount"], 0)
        self.assertEqual(platform.vms["microvm-2"]["state"]["verificationCount"], 0)
        self.assertEqual(self.load("verification-1.json")["state"]["runtimeId"], "runtime-1")
        self.assertEqual(self.load("verification-2.json")["state"]["runtimeId"], "runtime-3")
        self.assertEqual(self.load("verification-resumed.json")["state"]["verificationCount"], 2)
        first, second = self.load("vm1.json"), self.load("vm2.json")
        self.assertEqual(first, self.load("candidates/vm1.json"))
        self.assertEqual(second, self.load("candidates/vm3.json"))
        self.assertEqual(first["suspended"]["state"], "SUSPENDED")
        self.assertEqual(first["resumed"]["state"], "RUNNING")
        self.assertEqual(second["microvmId"], "microvm-3")
        checks = self.load("clone-checks.json")
        self.assertIs(checks["passed"], True)
        self.assertIs(checks["checks"]["shared_snapshot_before_identity"], True)
        self.assertTrue(all(check.evaluate_guest(self.results).values()))
        self.assert_cleaned(platform, (1, 2, 3))
        self.assertEqual(platform.termination_attempts, ["microvm-1", "microvm-2", "microvm-3"])
        for operation, args in platform.calls:
            if operation == "run-microvm":
                self.assertEqual(args[args.index("--maximum-duration-in-seconds") + 1], "600")
        for path in self.results.rglob("*.json"):
            self.assertNotIn("secret-auth-token", path.read_text())

    def test_match_can_use_later_baseline(self):
        platform = FakeCloud(["A", "B", "B"])
        self.run_with(platform)
        self.assertEqual(self.load("selection.json")["selected"], [2, 3])
        self.assertEqual(self.load("vm1.json")["microvmId"], "microvm-2")
        self.assertEqual(platform.vms["microvm-1"]["state"]["verificationCount"], 0)
        self.assert_cleaned(platform, (1, 2, 3))

    def test_six_unmatched_candidates_are_inconclusive_and_all_cleaned(self):
        platform = FakeCloud(["A", "B", "C", "D", "E", "F", "A"])
        with self.assertRaisesRegex(RuntimeError, "Inconclusive.*6 candidates.*shared_snapshot_before_identity"):
            self.run_with(platform)
        selection = self.load("selection.json")
        self.assertEqual(selection["status"], "inconclusive")
        self.assertEqual(selection["limit"], 6)
        self.assertEqual(len(selection["candidates"]), 6)
        self.assertNotIn("selected", selection)
        self.assertIn("rerun", selection["reason"])
        self.assertEqual(len(platform.vms), 6)
        self.assertEqual(len(platform.requests), 6)
        self.assertTrue(all(method == "GET" for _, method, _ in platform.requests))
        self.assertFalse((self.results / "vm1.json").exists())
        self.assertFalse(list(self.results.glob("verification-*.json")))
        self.assert_cleaned(platform, range(1, 7))

    def test_startup_failure_after_id_is_persisted_still_cleans_candidate(self):
        platform = FakeCloud(["A"], startup_failure=1)
        with self.assertRaisesRegex(RuntimeError, "Wanted RUNNING, got FAILED"):
            self.run_with(platform)
        vm = self.load("candidates/vm1.json")
        self.assertNotIn("endpoint", vm)
        self.assertEqual(vm["imageVersion"], "2.0")
        self.assertEqual(self.load("selection.json")["candidates"], [
            {"number": 1, "microvmId": "microvm-1", "beforeRuntimeId": None},
        ])
        self.assertFalse((self.results / "vm1.json").exists())
        self.assertEqual(platform.requests, [])
        self.assert_cleaned(platform, (1,))

    def test_state_capture_failure_cleans_prior_and_current_candidate(self):
        platform = FakeCloud(["A", "B"], state_failure="microvm-2")
        with self.assertRaisesRegex(OSError, "state capture failed"):
            self.run_with(platform)
        self.assertEqual(self.load("candidates/state1.json")["before"]["runtimeId"], "A")
        self.assertFalse((self.results / "candidates/state2.json").exists())
        self.assertEqual(len(self.load("selection.json")["candidates"]), 2)
        self.assertFalse((self.results / "vm1.json").exists())
        self.assertTrue(all(method == "GET" for _, method, _ in platform.requests))
        self.assert_cleaned(platform, (1, 2))

    def test_interrupted_state_capture_cleans_every_persisted_candidate(self):
        platform = FakeCloud(["A", "B"])
        original = platform.urlopen

        def interrupt(request, timeout):
            if request.full_url == "https://microvm-2.example/state":
                raise KeyboardInterrupt()
            return original(request, timeout)

        platform.urlopen = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(platform)
        self.assert_cleaned(platform, (1, 2))

    def test_invalid_prepared_identity_is_saved_but_never_verified(self):
        invalid_states = [
            {"before": None}, {"before": {"runtimeId": ""}}, {"before": {"runtimeId": "   "}},
            {"before": {"runtimeId": 123}}, {"prepared": False}, {"runCount": 0}, {"runCount": True},
            {"run": None}, {"run": {"runtimeId": ""}}, {"runtimeId": "not-the-run-identity"},
        ]
        for number, changes in enumerate(invalid_states, 1):
            with self.subTest(changes=changes):
                results = self.results / f"invalid-{number}"
                results.mkdir()
                cloud.save(results / "deployment.json", self.deployment)
                platform = FakeCloud(["A"], state_change=changes)
                with self.assertRaisesRegex(ValueError, "Expected prepared=true"):
                    self.run_with(platform, results)
                state = self.load("candidates/state1.json", results)
                for key, value in changes.items():
                    self.assertEqual(state[key], value)
                self.assertEqual(platform.requests, [("microvm-1", "GET", "/state")])
                self.assert_cleaned(platform, (1,), results)

    def test_cleanup_continues_deduplicates_syncs_metadata_and_is_idempotent(self):
        platform = FakeCloud(["A", "B"])
        (self.results / "candidates").mkdir()
        selection = {"status": "inconclusive", "limit": 6, "candidates": []}
        with patch.object(cloud, "aws", side_effect=platform.aws):
            first = cloud.launch(self.results, 1, self.deployment, selection)
            second = cloud.launch(self.results, 2, self.deployment, selection)
            first.update(end=123, suspended={"state": "SUSPENDED", "time": 120},
                         resumed={"state": "RUNNING", "time": 121})
            cloud.save(self.results / "vm1.json", first)
            cloud.save(self.results / "vm2.json", second)
            platform.termination_failures.add("microvm-1")
            with self.assertRaisesRegex(RuntimeError, "microvm-1.*temporary termination failure"):
                cloud.terminate(self.results)
            self.assertNotIn("terminated", self.load("vm1.json"))
            self.assertEqual(self.load("vm2.json"), self.load("candidates/vm2.json"))
            self.assert_cleaned(platform, (2,))
            cloud.terminate(self.results)
            self.assert_cleaned(platform, (1, 2))
            self.assertEqual(self.load("vm1.json"), self.load("candidates/vm1.json"))
            for key in ("end", "suspended", "resumed", "start", "imageArn", "imageVersion"):
                self.assertEqual(self.load("vm1.json")[key], first[key])
            before = {path: path.read_bytes() for path in self.results.rglob("*.json")}
            cloud.terminate(self.results)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(platform.termination_attempts, ["microvm-1", "microvm-2", "microvm-1"])

    def test_insecure_guest_endpoint_rejected_before_minting_a_token(self):
        with patch.object(cloud, "aws") as aws, patch.object(cloud.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                cloud.guest_request({"microvmId": "vm", "endpoint": "http://vm.example"}, "/state")
        aws.assert_not_called()
        urlopen.assert_not_called()

    def test_shell_exit_cleanup_runs_for_candidates_without_selected_aliases(self):
        tools = Path(self.directory.name) / "bin"
        tools.mkdir()
        for name in ("aws", "aws-vault", "dd-auth", "make", "npm"):
            (tools / name).symlink_to("/usr/bin/true")
        runner = tools / "python3"
        runner.write_text(f"#!{sys.executable}\n" + '''import json
from pathlib import Path
import sys

action = sys.argv[2]
results = Path(sys.argv[3])
if action == "run":
    (results / "candidates").mkdir()
    (results / "candidates/vm1.json").write_text(json.dumps({"microvmId": "microvm-1"}))
    raise SystemExit(7)
if action == "terminate":
    with (results / "cleanup-calls.txt").open("a") as output:
        output.write("terminate\\n")
''')
        runner.chmod(0o755)
        results = Path(self.directory.name) / "shell-results"
        response = subprocess.run(
            ["bash", str(cloud.APP / "run-all.sh")], check=False, capture_output=True, text=True,
            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                 "DD_API_KEY": "unused-offline-test-key", "RESULTS_DIR": str(results), "CHECK_DELAY": "0"},
            timeout=20,
        )
        self.assertEqual(response.returncode, 1, response.stdout + response.stderr)
        self.assertFalse((results / "vm1.json").exists())
        self.assertFalse((results / "vm2.json").exists())
        self.assertEqual((results / "cleanup-calls.txt").read_text().splitlines(), ["terminate", "terminate"])
        self.assertIn("90-cleanup exit=0", (results / "steps.txt").read_text())


if __name__ == "__main__":
    unittest.main()
