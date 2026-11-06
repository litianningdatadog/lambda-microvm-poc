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

    def __init__(self, baselines, startup_failure=None, state_failure=None, state_change=None,
                 delivery_failure=None, cycle_failure=None):
        self.baselines = baselines
        self.startup_failure = startup_failure
        self.state_failure = state_failure
        self.state_change = state_change or {}
        self.delivery_failure = delivery_failure
        self.cycle_failure = cycle_failure
        self.vms = {}
        self.calls = []
        self.requests = []
        self.events = []
        self.termination_attempts = []
        self.termination_failures = set()

    def aws(self, service, operation, *args):
        if service != "lambda-microvms":
            raise AssertionError(f"Unexpected service: {service}")
        self.calls.append((operation, args))
        self.events.append((operation, args))
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
            if vm["state"]["suspendCount"] == self.cycle_failure:
                raise RuntimeError(f"resume cycle {self.cycle_failure} failed")
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
        self.events.append((request.get_method() + " " + url.path, microvm_id))
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
        elif url.path in ("/delivery/automatic", "/delivery/recovery", "/delivery/termination"):
            if request.get_method() != "POST" or request.data != b"{}":
                raise AssertionError("Delivery scenarios require POST with JSON")
            result = self.delivery(microvm_id, url.path.rsplit("/", 1)[1])
        else:
            raise AssertionError("Only read-only state and selected verification/delivery routes are permitted")
        return io.StringIO(json.dumps(result))

    def verification(self, microvm_id):
        state = copy.deepcopy(self.vms[microvm_id]["state"])
        resumed = state["resumeCount"] > 0
        seed = int(microvm_id.split("-")[-1]) * 10 + state["verificationCount"]
        ids = [{"traceId": str((seed << 64) + seed * 1000 + index),
                "spanId": str(seed * 1000 + index)} for index in range(1, 9 if resumed else 137)]
        roots = {name: "verify.js." + name for name in sorted(check.ROOT_CASES)}
        return {
            "schema": 2, "version": "6.15.0", "passed": True,
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

    def delivery(self, microvm_id, scenario):
        state = self.vms[microvm_id]["state"]
        runtime = state["runtimeId"]
        seed = int(microvm_id.split("-")[-1]) * 1000 + 100000

        def identity(number):
            return {"traceId": str((seed << 64) + number), "spanId": str(number)}

        def request(number, span, operation, marker, outcome="accepted", received=1000):
            return {
                "sequence": number, "receivedAt": received, "completedAt": received + 100,
                "status": 200 if outcome == "accepted" else 503 if outcome == "injected-http503" else None,
                "outcome": outcome, "forwarded": outcome == "accepted", "bytes": 256,
                "spans": [{
                    "trace_id": str(int(span["traceId"]) & ((1 << 64) - 1)),
                    "span_id": span["spanId"], "parent_id": "0", "name": operation,
                    "service": check.SERVICE, "meta": {
                        "runtime-id": runtime, "verify.delivery.marker": marker,
                    }, "metrics": {"_dd.measured": 1},
                }],
            }

        if scenario == "termination":
            self.vms[microvm_id]["armedAtVerificationCount"] = state["verificationCount"]
            self.vms[microvm_id]["armedAtResumeCount"] = state["resumeCount"]
            return {"schema": 2, "scenario": "termination-armed", "armed": True,
                    "marker": f"terminate-{microvm_id}", "runtimeId": runtime,
                    "operation": "verify.js.termination"}
        evidence = {"runtimeId": runtime, "forwardToAgent": True, "flushIntervalMs": 2000,
                    "manualFlushCountBefore": 1, "manualFlushCountAfter": 1}
        if scenario == "automatic":
            span = identity(seed + 1)
            marker, operation = f"automatic-{microvm_id}", "verify.js.automatic"
            evidence.update(marker=marker, operation=operation, span=span, elapsedMs=2100, deadlineMs=10000,
                            successfulRequests=[request(1, span, operation, marker)])
            required = check.AUTOMATIC_CHECKS
        elif scenario == "recovery":
            cases = []
            for index, mode in enumerate(("http503", "disconnect")):
                start = 10000 * (index + 1)
                marker, operation = f"{mode}-{microvm_id}", f"verify.js.recovery.{mode}"
                fresh_marker = marker + "-fresh"
                offered = [identity(seed + index * 10 + offset) for offset in range(2, 5)]
                fresh = identity(seed + index * 10 + 5)
                cases.append({
                    "mode": mode, "marker": marker, "operation": operation, "generated": len(offered),
                    "offeredSpans": offered, "freshSpan": fresh, "freshMarker": fresh_marker,
                    "faultActiveAt": start, "restoredAt": start + 1000, "freshCreatedAt": start + 1050,
                    "baselineEncoderCount": 0, "unexpectedErrors": [],
                    "failureAttempts": [request(index * 2 + 2, offered[0], operation, marker,
                                                "injected-" + ("http503" if mode == "http503" else "disconnect"),
                                                start + 100)],
                    "freshAcknowledgements": [request(index * 2 + 3, fresh, operation, fresh_marker,
                                                      received=start + 1100)],
                    "probes": [{"at": start + offset, "status": 200, "runtimeId": runtime, "elapsedMs": 1}
                               for offset in (250, 500, 750)],
                    "encoderCounts": [
                        {"at": start - 100, "count": 0}, {"at": start + 50, "count": 3},
                        {"at": start + 1000, "count": 0}, {"at": start + 1050, "count": 1},
                        {"at": start + 1200, "count": 0},
                    ],
                    "encoderPeak": 3, "queueDrained": True, "elapsedMs": 1500, "deadlineMs": 20000,
                })
            evidence.update(cases=cases, unexpectedErrors=[])
            required = check.RECOVERY_CHECKS
        else:
            raise AssertionError(f"Unknown scenario: {scenario}")
        passed = scenario != self.delivery_failure
        return {"schema": 2, "scenario": scenario, "passed": passed,
                "checks": dict.fromkeys(required, passed), "evidence": evidence}


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name) / "results"
        self.results.mkdir()
        self.deployment = {"schema": 2, "imageArn": "image", "imageVersion": "2.0",
                           "version": "Run-1", "start": 100}
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

    def test_first_matching_pair_runs_all_delivery_and_five_feature_samples(self):
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
            ("microvm-3", "POST", "/verify"),
            ("microvm-1", "POST", "/delivery/automatic"),
            ("microvm-1", "POST", "/delivery/recovery"),
            *[("microvm-1", "POST", "/verify") for _ in range(3)],
            ("microvm-1", "POST", "/delivery/termination"),
        ])
        self.assertEqual(len(platform.vms), 3)
        for number in (1, 2, 3):
            self.assertEqual(self.load(f"candidates/state{number}.json")["verificationCount"], 0)
        self.assertEqual(platform.vms["microvm-2"]["state"]["verificationCount"], 0)
        self.assertEqual(self.load("verification-1.json")["state"]["runtimeId"], "runtime-1")
        self.assertEqual(self.load("verification-2.json")["state"]["runtimeId"], "runtime-3")
        for cycle in range(1, 4):
            state = self.load(f"verification-resumed-{cycle}.json")["state"]
            self.assertEqual(state["verificationCount"], 1 + cycle)
            self.assertEqual(state["runCount"], 1)
            self.assertEqual(state["suspendCount"], cycle)
            self.assertEqual(state["resumeCount"], cycle)
            self.assertEqual(state["runtimeId"], "runtime-1")
        self.assertFalse((self.results / "verification-resumed.json").exists())
        first, second = self.load("vm1.json"), self.load("vm2.json")
        self.assertEqual(first, self.load("candidates/vm1.json"))
        self.assertEqual(second, self.load("candidates/vm3.json"))
        self.assertEqual([cycle["cycle"] for cycle in first["cycles"]], [1, 2, 3])
        for cycle in first["cycles"]:
            self.assertEqual(cycle["suspended"]["state"], "SUSPENDED")
            self.assertEqual(cycle["resumed"]["state"], "RUNNING")
            self.assertLessEqual(cycle["suspended"]["time"], cycle["resumed"]["time"])
        self.assertNotIn("cycles", second)
        self.assertEqual(second["microvmId"], "microvm-3")
        checks = self.load("clone-checks.json")
        self.assertIs(checks["passed"], True)
        self.assertIs(checks["checks"]["shared_snapshot_before_identity"], True)
        self.assertTrue(all(check.evaluate_guest(self.results).values()))
        self.assert_cleaned(platform, (1, 2, 3))
        self.assertEqual(platform.termination_attempts, ["microvm-1", "microvm-2", "microvm-3"])
        self.assertEqual(self.load("termination-armed.json")["runtimeId"], "runtime-1")
        self.assertEqual(platform.events[-7][0], "POST /delivery/termination")
        self.assertEqual([event[0] for event in platform.events[-6:]],
                         ["terminate-microvm", "get-microvm"] * 3)
        self.assertEqual(platform.vms["microvm-1"]["armedAtVerificationCount"], 4)
        self.assertEqual(platform.vms["microvm-1"]["armedAtResumeCount"], 3)
        self.assertNotIn("armedAtVerificationCount", platform.vms["microvm-2"])
        self.assertNotIn("armedAtVerificationCount", platform.vms["microvm-3"])
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
        self.assertEqual(self.load("termination-armed.json")["runtimeId"], "runtime-2")
        self.assertNotIn("armedAtVerificationCount", platform.vms["microvm-1"])
        self.assert_cleaned(platform, (1, 2, 3))

    def test_missing_resume_sample_fails_before_arming_and_cleans_every_candidate(self):
        platform = FakeCloud(["A", "B", "A"])
        original_save = cloud.save

        def omit_second_resume(path, value):
            if path.name != "verification-resumed-2.json":
                original_save(path, value)

        with patch.object(cloud, "save", side_effect=omit_second_resume):
            with self.assertRaises(FileNotFoundError):
                self.run_with(platform)
        self.assertTrue((self.results / "verification-resumed-3.json").exists())
        self.assertFalse((self.results / "termination-armed.json").exists())
        self.assertFalse(any(path == "/delivery/termination" for _, _, path in platform.requests))
        self.assert_cleaned(platform, (1, 2, 3))

    def test_missing_aws_cycle_metadata_fails_before_arming_and_cleans_every_candidate(self):
        platform = FakeCloud(["A", "B", "A"])
        original_save = cloud.save

        def omit_second_cycle(path, value):
            value = copy.deepcopy(value)
            if isinstance(value, dict) and "cycles" in value:
                value["cycles"] = [cycle for cycle in value["cycles"] if cycle["cycle"] != 2]
            original_save(path, value)

        with patch.object(cloud, "save", side_effect=omit_second_cycle):
            with self.assertRaisesRegex(RuntimeError, "Snapshot clone/resume assertions failed"):
                self.run_with(platform)
        self.assertIs(self.load("clone-checks.json")["passed"], False)
        self.assertFalse((self.results / "termination-armed.json").exists())
        self.assert_cleaned(platform, (1, 2, 3))

    def test_midcycle_failure_preserves_partial_evidence_and_cleans_every_candidate(self):
        platform = FakeCloud(["A", "B", "A"], cycle_failure=2)
        with self.assertRaisesRegex(RuntimeError, "resume cycle 2 failed"):
            self.run_with(platform)
        first = self.load("vm1.json")
        self.assertEqual(first, self.load("candidates/vm1.json"))
        self.assertEqual([cycle["cycle"] for cycle in first["cycles"]], [1, 2])
        self.assertEqual(first["cycles"][1]["suspended"]["state"], "SUSPENDED")
        self.assertNotIn("resumed", first["cycles"][1])
        self.assertTrue((self.results / "verification-resumed-1.json").exists())
        self.assertFalse((self.results / "verification-resumed-2.json").exists())
        self.assertFalse((self.results / "termination-armed.json").exists())
        self.assertFalse(any(path == "/delivery/termination" for _, _, path in platform.requests))
        self.assert_cleaned(platform, (1, 2, 3))

    def test_delivery_failure_is_saved_and_cleans_every_candidate_without_arming(self):
        for scenario in ("automatic", "recovery"):
            with self.subTest(scenario=scenario):
                results = self.results / scenario
                results.mkdir()
                cloud.save(results / "deployment.json", self.deployment)
                platform = FakeCloud(["A", "B", "A"], delivery_failure=scenario)
                with self.assertRaisesRegex(RuntimeError, f"delivery-{scenario}.json"):
                    self.run_with(platform, results)
                self.assertIs(self.load(f"delivery-{scenario}.json", results)["passed"], False)
                self.assertFalse((results / "verification-resumed-1.json").exists())
                self.assertFalse((results / "termination-armed.json").exists())
                self.assertFalse(any(path == "/delivery/termination" for _, _, path in platform.requests))
                self.assert_cleaned(platform, (1, 2, 3), results)

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
            first.update(end=123, cycles=[{"cycle": 1,
                         "suspended": {"state": "SUSPENDED", "time": 120},
                         "resumed": {"state": "RUNNING", "time": 121}}])
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
            for key in ("end", "cycles", "start", "imageArn", "imageVersion"):
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
        for name in ("aws", "aws-vault", "dd-auth"):
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
