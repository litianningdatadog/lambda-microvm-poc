"""Focused offline evaluator regressions; no AWS/Datadog credentials or calls."""

import copy
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import check


def guest(runtime, seed, resumed=False):
    roots = {name: "verify.js." + name for name in check.ROOT_CASES}
    ids = [{"traceId": str((seed << 64) + seed * 1000 + index),
            "spanId": str(seed * 1000 + index)} for index in range(1, 9 if resumed else 137)]
    root_spans = [{**value, "name": name} for name, value in zip(roots.values(), ids[-8:])]
    state = {"runtimeId": runtime, "rcClientId": None, "prepared": True, "bundledOpenSSL": False,
             "runCount": 1, "suspendCount": int(resumed), "resumeCount": int(resumed), "terminateCount": 0,
             "verificationCount": 2 if resumed else 1,
             "before": {"runtimeId": "snapshot-runtime", "rcClientId": None},
             "run": {"runtimeId": runtime, "rcClientId": None}}
    return {"schema": 1, "version": "6.15.0", "passed": True,
            "checks": dict.fromkeys(check.REQUIRED_CHECKS, True),
            "state": state,
            "evidence": {"before": {"runtimeId": "snapshot-runtime", "rcClientId": None},
                         "run": {"runtimeId": runtime, "rcClientId": None},
                         "after": {"runtimeId": runtime, "rcClientId": None},
                         "ids": ids,
                         "features": {"roots": roots, "rootSpans": root_spans, "errorOperation": roots["expectedError"]},
                         "spans": [{"name": operation, "service": check.SERVICE,
                                    "error": int(name == "expectedError")} for name, operation in roots.items()]}}


def trace_request(identity, operation, runtime, marker, sequence=1, received=2000, accepted=True, mode="http503"):
    return {"sequence": sequence, "receivedAt": received, "completedAt": received + 1,
            "status": 200 if accepted else (503 if mode == "http503" else None),
            "outcome": "accepted" if accepted else "injected-" + mode,
            "forwarded": accepted, "bytes": 300,
            "spans": [{"trace_id": str(int(identity["traceId"]) & ((1 << 64) - 1)),
                       "span_id": identity["spanId"], "parent_id": "0", "name": operation,
                       "service": check.SERVICE, "meta": {"runtime-id": runtime, "verify.delivery.marker": marker},
                       "metrics": {"_dd.measured": 1}}]}


def delivery_result(scenario, runtime, seed=900):
    marker, operation = "marker-" + str(seed), "verify.js." + scenario
    identity = {"traceId": str(seed), "spanId": str(seed + 10000)}
    names = check.AUTOMATIC_CHECKS if scenario == "automatic" else check.TERMINATION_CHECKS
    evidence = {"marker": marker, "runtimeId": runtime, "operation": operation, "span": identity,
                "flushIntervalMs": 2000, "manualFlushCountBefore": 2, "manualFlushCountAfter": 2,
                "forwardToAgent": True, "elapsedMs": 2001, "deadlineMs": 10000,
                "successfulRequests": [trace_request(identity, operation, runtime, marker)]}
    return {"schema": 2, "scenario": scenario, "marker": marker, "passed": True,
            "checks": dict.fromkeys(names, True), "evidence": evidence}


def recovery_result(runtime):
    cases = []
    for index, mode in enumerate(("http503", "disconnect")):
        seed, at = 10000 + index * 100, 1000 + index * 10000
        marker, fresh_marker = "fault-" + mode, "fresh-" + mode
        operation = "verify.js.recovery." + mode
        offered = [{"traceId": str(seed + value), "spanId": str(seed + value + 1000)} for value in range(1, 4)]
        fresh = {"traceId": str(seed + 99), "spanId": str(seed + 1099)}
        cases.append({
            "mode": mode, "marker": marker, "freshMarker": fresh_marker, "operation": operation,
            "generated": 3, "offeredSpans": offered, "freshSpan": fresh, "baselineEncoderCount": 0,
            "faultActiveAt": at, "restoredAt": at + 3000, "freshCreatedAt": at + 3001,
            "failureAttempts": [trace_request(offered[0], operation, runtime, marker,
                                               received=at + 2000, accepted=False, mode=mode)],
            "freshAcknowledgements": [trace_request(fresh, operation, runtime, fresh_marker,
                                                     sequence=2, received=at + 5000)],
            "probes": [{"at": at + value, "status": 200, "runtimeId": runtime, "elapsedMs": 1}
                       for value in (100, 200, 300)],
            "encoderCounts": [{"at": at - 1, "count": 0}, {"at": at + 1, "count": 3},
                              {"at": at + 5001, "count": 0}],
            "encoderPeak": 3, "queueDrained": True, "unexpectedErrors": [],
            "elapsedMs": 5002, "deadlineMs": 20000})
    return {"schema": 2, "scenario": "recovery", "passed": True,
            "checks": dict.fromkeys(check.RECOVERY_CHECKS, True),
            "evidence": {"runtimeId": runtime, "flushIntervalMs": 2000,
                         "manualFlushCountBefore": 2, "manualFlushCountAfter": 2,
                         "forwardToAgent": True, "unexpectedErrors": [], "cases": cases}}


def schema2_fixture(directory):
    """Consumer-only evidence fixtures: no guest/native behavior is simulated."""
    def save(name, value):
        (directory / name).write_text(json.dumps(value))

    save("deployment.json", {"schema": 2, "imageArn": "image", "imageVersion": "1.0",
                             "version": "Run-1", "start": 100})
    guests = {}
    for index, sample in enumerate(check.SAMPLES, 1):
        cycle = index - 2 if sample.startswith("resumed") else 0
        value = guest("runtime-2" if sample == "2" else "runtime-1", index, resumed=bool(cycle))
        value["schema"] = 2
        value["state"].update(suspendCount=cycle, resumeCount=cycle, verificationCount=1 + cycle)
        guests[sample] = value
        save(f"verification-{sample}.json", value)
    for number in (1, 2):
        vm = {"microvmId": f"vm-{number}", "imageArn": "image", "imageVersion": "1.0",
              "start": 110, "end": 120, "terminated": True, "terminatedAt": 125}
        if number == 1:
            vm["cycles"] = [{"cycle": cycle, "suspended": {"state": "SUSPENDED", "time": 110 + cycle * 2},
                             "resumed": {"state": "RUNNING", "time": 111 + cycle * 2}} for cycle in range(1, 4)]
        save(f"vm{number}.json", vm)
    automatic, recovery = delivery_result("automatic", "runtime-1"), recovery_result("runtime-1")
    termination = delivery_result("termination", "runtime-1", seed=901)
    termination["state"] = {**copy.deepcopy(guests["resumed-3"]["state"]), "terminateCount": 1}
    arm = {"schema": 2, "scenario": "termination-armed", "armed": True,
           "marker": termination["marker"], "runtimeId": "runtime-1", "operation": "verify.js.termination"}
    save("delivery-automatic.json", automatic)
    save("delivery-recovery.json", recovery)
    save("termination-armed.json", arm)
    rows = [{"msg": "microvm_js_verification", "version": "Run-1", "result": value} for value in guests.values()]
    rows.extend([
        {"msg": "microvm_js_termination_result", "version": "Run-1", "result": termination},
        {"msg": "microvm_js_termination_log", "version": "Run-1", "marker": arm["marker"],
         "runtimeId": arm["runtimeId"], **termination["evidence"]["span"]}])
    cloudwatch = {"events": [{"logStreamName": "stream/vm-2" if index == 1 else "stream/vm-1",
                              "message": json.dumps(row)} for index, row in enumerate(rows)]}
    save("cloudwatch.json", cloudwatch)
    backend = {"feature_" + name: {"series": [{"pointlist": [[100, 5]]}]} for name in check.ROOT_CASES}
    backend["feature_errors"] = {"series": [{"pointlist": [[100, 5]]}]}
    for name in ("automatic", "recovery_http503", "recovery_disconnect", "termination"):
        backend["delivery_" + name] = {"series": [{"pointlist": [[100, 1]]}]}
    backend["logs"] = {"data": [{"attributes": {"message": row["msg"],
                                               "attributes": {key: value for key, value in row.items() if key != "msg"}}}
                                for row in rows]}
    return {"guests": guests, "automatic": automatic, "recovery": recovery, "arm": arm,
            "termination": termination, "cloudwatch": cloudwatch, "backend": backend}


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name)
        self.write("deployment.json", {"imageArn": "image", "imageVersion": "1.0", "version": "Run-1", "start": 100})
        for number in (1, 2):
            self.write(f"vm{number}.json", {"microvmId": f"vm-{number}", "imageArn": "image", "imageVersion": "1.0",
                                           "start": 110, "end": 120, "terminated": True, "terminatedAt": 125,
                                           "suspended": {"state": "SUSPENDED"}, "resumed": {"state": "RUNNING"}})
        self.guests = {"1": guest("runtime-1", 1), "2": guest("runtime-2", 2),
                       "resumed": guest("runtime-1", 3, resumed=True)}
        for sample, value in self.guests.items():
            self.write(f"verification-{sample}.json", value)
        self.write("cloudwatch.json", {"events": [
            {"logStreamName": "stream/vm-2" if sample == "2" else "stream/vm-1",
             "message": json.dumps({"msg": "microvm_js_verification", "version": "Run-1", "result": value})}
            for sample, value in self.guests.items()]})

    def write(self, name, value):
        (self.results / name).write_text(json.dumps(value))

    def test_valid_snapshot_and_resume(self):
        self.assertTrue(all(check.evaluate_guest(self.results).values()))

    def test_low64_collision_not_hidden_by_timestamp(self):
        changed = copy.deepcopy(self.guests["2"])
        changed["evidence"]["ids"][0]["traceId"] = str((999 << 64) + 1001)
        self.write("verification-2.json", changed)
        self.assertFalse(check.evaluate_guest(self.results)["unique_trace_low64_1_2"])

    def test_unexpected_rc_client_fails(self):
        changed = copy.deepcopy(self.guests["2"])
        changed["evidence"]["run"]["rcClientId"] = "unexpected-rc"
        self.write("verification-2.json", changed)
        self.assertFalse(check.evaluate_guest(self.results)["guest_2_rc_client_absent"])

    def test_resume_new_identity_fails(self):
        changed = copy.deepcopy(self.guests["resumed"])
        changed["evidence"]["after"]["runtimeId"] = "new-runtime"
        self.write("verification-resumed.json", changed)
        self.assertFalse(check.evaluate_guest(self.results)["resume_stable_runtimeId"])

    def test_bundled_openssl_rejected_on_aws(self):
        changed = copy.deepcopy(self.guests["1"])
        changed["state"]["bundledOpenSSL"] = True
        self.write("verification-1.json", changed)
        self.assertFalse(check.evaluate_guest(self.results)["guest_1_system_openssl"])

    def test_missing_required_guest_check_fails(self):
        changed = copy.deepcopy(self.guests["1"])
        del changed["checks"]["http_404_client_error"]
        self.write("verification-1.json", changed)
        checks = check.evaluate_guest(self.results)
        self.assertFalse(checks["guest_1_required_checks_present"])
        self.assertFalse(checks["guest_1_http_404_client_error"])

    def test_duplicate_trace_low64_within_sample_fails(self):
        changed = copy.deepcopy(self.guests["1"])
        changed["evidence"]["ids"][1]["traceId"] = str((999 << 64) + 1001)
        self.write("verification-1.json", changed)
        with self.assertRaisesRegex(ValueError, "Duplicate trace low64"):
            check.evaluate_guest(self.results)

    def test_duplicate_span_id_within_sample_fails(self):
        changed = copy.deepcopy(self.guests["1"])
        changed["evidence"]["ids"][1]["spanId"] = changed["evidence"]["ids"][0]["spanId"]
        self.write("verification-1.json", changed)
        with self.assertRaisesRegex(ValueError, "Duplicate span ID"):
            check.evaluate_guest(self.results)

    def test_missing_log_result_fails(self):
        self.assertFalse(check.log_matches({"result": None}, self.guests["1"], "Run-1"))

    def test_missing_backend_data_is_failure(self):
        with patch.object(check, "dd", return_value={"series": [], "data": []}):
            self.assertEqual(check.evaluate(self.results), 1)
        result = json.loads((self.results / "evaluation.json").read_text())
        self.assertFalse(result["checks"]["datadog_feature_manual_hits"])
        self.assertFalse(result["checks"]["datadog_1_verification_log_matches"])

    def test_offline_mode_does_not_query_backend(self):
        with patch.object(check, "dd", side_effect=AssertionError("No backend in offline mode")):
            self.assertEqual(check.evaluate(self.results, guest_only=True), 0)
        result = json.loads((self.results / "evaluation.json").read_text())
        self.assertEqual(result["mode"], "guest-and-cloudwatch-only")
        self.assertEqual(result["coverage"]["schema"], 1)
        self.assertTrue(result["coverage"]["historical"])
        self.assertEqual(result["coverage"]["samples"], list(check.HISTORICAL_SAMPLES))
        self.assertEqual(len(result["coverage"]["notCovered"]), 4)
        self.assertNotIn("automatic_trace_acknowledged", result["checks"])

    def log_row(self):
        return {"msg": "microvm_js_verification", "version": "Run-1",
                "result": copy.deepcopy(self.guests["1"])}

    def identity_rows(self, result):
        return [result["state"], result["state"]["before"], result["state"]["run"],
                *(result["evidence"][phase] for phase in ("before", "run", "after"))]

    def pipeline_row(self, value):
        result = copy.deepcopy(value)
        for identity in self.identity_rows(result):
            del identity["rcClientId"]
        return {"msg": "microvm_js_verification", "version": "Run-1", "result": result}

    def test_pipeline_omitted_null_rc_matches_without_mutating_inputs(self):
        row = self.pipeline_row(self.guests["1"])
        original_row, original_guest = copy.deepcopy(row), copy.deepcopy(self.guests["1"])
        self.assertTrue(check.log_matches(row, self.guests["1"], "Run-1"))
        self.assertEqual(row, original_row)
        self.assertEqual(self.guests["1"], original_guest)
        self.assertTrue(check.log_matches(self.log_row(), self.guests["1"], "Run-1"))

    def test_pipeline_rc_omission_requires_matching_disabled_assertion(self):
        for assertion in (False, None, "missing"):
            with self.subTest(assertion=assertion):
                value = copy.deepcopy(self.guests["1"])
                if assertion == "missing":
                    del value["checks"]["remote_config_disabled_in_serverless"]
                else:
                    value["checks"]["remote_config_disabled_in_serverless"] = assertion
                self.assertFalse(check.log_matches(self.pipeline_row(value), value, "Run-1"))
        row = self.pipeline_row(self.guests["1"])
        row["result"]["checks"]["remote_config_disabled_in_serverless"] = False
        self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))

    def test_pipeline_identity_corruption_fails_at_each_known_path(self):
        for index in range(6):
            for corruption in ("unexpected_rc", "changed_runtime", "missing_runtime"):
                with self.subTest(index=index, corruption=corruption):
                    row = self.pipeline_row(self.guests["1"])
                    identity = self.identity_rows(row["result"])[index]
                    if corruption == "unexpected_rc":
                        identity["rcClientId"] = "unexpected"
                    elif corruption == "changed_runtime":
                        identity["runtimeId"] = "wrong"
                    else:
                        del identity["runtimeId"]
                    self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))

    def test_missing_identity_objects_fail(self):
        for parent, key in (("state", "before"), ("state", "run"),
                            ("evidence", "before"), ("evidence", "run"), ("evidence", "after")):
            with self.subTest(parent=parent, key=key):
                row = self.pipeline_row(self.guests["1"])
                del row["result"][parent][key]
                self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))
        row = self.pipeline_row(self.guests["1"])
        del row["result"]["state"]
        self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))

    def test_missing_or_changed_counters_fail(self):
        for counter in ("runCount", "resumeCount", "suspendCount", "terminateCount", "verificationCount"):
            for missing in (True, False):
                with self.subTest(counter=counter, missing=missing):
                    row = self.pipeline_row(self.guests["1"])
                    if missing:
                        del row["result"]["state"][counter]
                    else:
                        row["result"]["state"][counter] += 1
                    self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))
        row = self.pipeline_row(self.guests["1"])
        del row["result"]["checks"]["http_404_client_error"]
        self.assertFalse(check.log_matches(row, self.guests["1"], "Run-1"))

    def test_unrelated_null_omission_is_not_normalized(self):
        for index in range(6):
            with self.subTest(index=index):
                value = copy.deepcopy(self.guests["1"])
                self.identity_rows(value)[index]["unrelated"] = None
                row = self.pipeline_row(value)
                del self.identity_rows(row["result"])[index]["unrelated"]
                self.assertFalse(check.log_matches(row, value, "Run-1"))
        value = copy.deepcopy(self.guests["1"])
        value["state"]["nested"] = {"rcClientId": None}
        row = self.pipeline_row(value)
        del row["result"]["state"]["nested"]["rcClientId"]
        self.assertFalse(check.log_matches(row, value, "Run-1"))

    def cache(self):
        evidence = {"feature_" + name: {"series": [{"pointlist": [[100, 3]]}]}
                    for name in check.ROOT_CASES}
        evidence["feature_errors"] = {"series": [{"pointlist": [[100, 3]]}]}
        evidence["logs"] = {"data": [
            {"attributes": {"message": "microvm_js_verification",
                            "attributes": {key: item for key, item in self.pipeline_row(value).items()
                                           if key != "msg"}}}
            for value in self.guests.values()]}
        self.write("evaluation.json", {"passed": False, "checks": {"fake": False},
                                       "mode": "aws-and-datadog", "evidence": evidence})
        return self.results / "evaluation.json"

    def test_cached_mode_recomputes_without_network_and_preserves_evidence(self):
        source = self.cache()
        original = source.read_bytes()
        output = self.results / "reevaluation.json"
        with patch.object(check, "dd", side_effect=AssertionError("No network")):
            self.assertEqual(check.evaluate(self.results, output, cached_evaluation=source), 0)
        result = json.loads(output.read_text())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(result["mode"], "aws-and-cached-datadog")
        self.assertEqual(result["evidence"], json.loads(original)["evidence"])
        self.assertEqual(result["provenance"]["cachedEvaluation"], str(source.resolve()))
        self.assertNotIn("fake", result["checks"])
        for sample in check.HISTORICAL_SAMPLES:
            self.assertTrue(result["checks"][f"datadog_{sample}_verification_log_matches"])

    def test_cached_checks_cannot_hide_snapshot_failure(self):
        source = self.cache()
        cached = json.loads(source.read_text())
        cached.update(passed=True, checks={"shared_snapshot_before_identity": True})
        source.write_text(json.dumps(cached))
        changed = copy.deepcopy(self.guests["2"])
        changed["evidence"]["before"]["runtimeId"] = "different-build-snapshot"
        changed["state"]["before"]["runtimeId"] = "different-build-snapshot"
        self.write("verification-2.json", changed)
        with patch.object(check, "dd", side_effect=AssertionError("No network")):
            output = self.results / "reevaluation.json"
            self.assertEqual(check.evaluate(self.results, output, cached_evaluation=source), 1)
        self.assertFalse(json.loads(output.read_text())["checks"]["shared_snapshot_before_identity"])

    def test_missing_cache_or_payload_never_falls_back_to_network(self):
        with patch.object(check, "dd", side_effect=AssertionError("No network")):
            with self.assertRaises(FileNotFoundError):
                check.evaluate(self.results, self.results / "new.json",
                               cached_evaluation=self.results / "missing.json")
            source = self.cache()
            for missing in ("evidence", "feature_manual", "logs"):
                with self.subTest(missing=missing):
                    payload = json.loads(source.read_text())
                    if missing == "evidence":
                        del payload["evidence"]
                    else:
                        del payload["evidence"][missing]
                    self.write("incomplete.json", payload)
                    with self.assertRaisesRegex(ValueError, "missing backend"):
                        check.evaluate(self.results, self.results / "new.json",
                                       cached_evaluation=self.results / "incomplete.json")

    def test_cached_output_safety(self):
        source = self.cache()
        self.write("other-cache.json", json.loads(source.read_text()))
        original = source.read_bytes()
        cases = ((None, source, False), (source, source, False),
                 (source, self.results / "other-cache.json", False),
                 (self.results / "new.json", source, True))
        for output, cache, guest_only in cases:
            with self.subTest(output=output, cache=cache, guest_only=guest_only):
                with self.assertRaises(ValueError):
                    check.evaluate(self.results, output, guest_only, cache)
        alias = self.results / "alias.json"
        alias.symlink_to(source)
        with self.assertRaises(ValueError):
            check.evaluate(self.results, alias, cached_evaluation=source)
        self.assertEqual(source.read_bytes(), original)

    def test_cached_cli_requires_safe_explicit_output(self):
        source = self.cache()
        original = source.read_bytes()
        for extra in ([], ["--output", str(source)]):
            with self.subTest(extra=extra), patch.object(
                    sys, "argv", ["check.py", str(self.results), "--cached-evaluation", str(source), *extra]):
                with self.assertRaises(SystemExit) as error:
                    check.main()
                self.assertEqual(error.exception.code, 2)
        self.assertEqual(source.read_bytes(), original)


class Schema2EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name)
        self.fixture = schema2_fixture(self.results)

    def write(self, name, value):
        (self.results / name).write_text(json.dumps(value))

    def cached_evaluate(self, backend=None):
        source = self.results / "cached.json"
        self.write("cached.json", {"mode": "aws-and-datadog", "evidence": (
            self.fixture["backend"] if backend is None else backend)})
        with patch.object(check, "dd", side_effect=AssertionError("Consumer test must not call backend")):
            status = check.evaluate(self.results, self.results / "replayed.json", cached_evaluation=source)
        return status, json.loads((self.results / "replayed.json").read_text())

    def test_all_five_samples_and_cached_named_evidence_pass(self):
        self.assertTrue(all(check.evaluate_guest(self.results).values()))
        status, result = self.cached_evaluate()
        self.assertEqual(status, 0)
        self.assertEqual(result["coverage"], {"schema": 2, "samples": list(check.SAMPLES),
                                            "historical": False, "notCovered": []})
        self.assertEqual(result["evidence"], self.fixture["backend"])

    def test_schema2_cannot_use_only_historical_three_results(self):
        for sample in check.SAMPLES[2:]:
            (self.results / f"verification-{sample}.json").unlink()
        self.write("verification-resumed.json", guest("runtime-1", 3, resumed=True))
        with self.assertRaises(FileNotFoundError):
            check.evaluate_guest(self.results)

    def test_missing_phase_or_cycle_is_not_coverage(self):
        for filename in ("delivery-automatic.json", "delivery-recovery.json", "verification-resumed-2.json"):
            with self.subTest(filename=filename):
                path = self.results / filename
                original = path.read_text()
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    check.evaluate_guest(self.results)
                path.write_text(original)
        vm = json.loads((self.results / "vm1.json").read_text())
        vm["cycles"].pop(1)
        self.write("vm1.json", vm)
        self.assertFalse(check.evaluate_guest(self.results)["aws_three_cycles_complete"])

    def test_each_cycle_counter_and_identity_are_independently_required(self):
        for cycle in range(1, 4):
            sample = f"resumed-{cycle}"
            for field in ("suspendCount", "resumeCount", "verificationCount", "runtimeId"):
                with self.subTest(cycle=cycle, field=field):
                    value = copy.deepcopy(self.fixture["guests"][sample])
                    value["state"][field] = 99 if field != "runtimeId" else "wrong"
                    self.write(f"verification-{sample}.json", value)
                    self.assertFalse(all(check.evaluate_guest(self.results).values()))
                    self.write(f"verification-{sample}.json", self.fixture["guests"][sample])

    def test_delivery_does_not_trust_summary_or_failed_payloads(self):
        for field, invalid in (("outcome", "injected-http503"), ("status", 503),
                               ("forwarded", False), ("completedAt", None)):
            with self.subTest(field=field):
                value = copy.deepcopy(self.fixture["automatic"])
                value["evidence"]["successfulRequests"][0][field] = invalid
                self.write("delivery-automatic.json", value)
                self.assertFalse(check.evaluate_guest(self.results)["automatic_trace_acknowledged"])
        for field, invalid in (("manualFlushCountAfter", 3), ("runtimeId", "wrong"), ("forwardToAgent", False)):
            with self.subTest(field=field):
                value = copy.deepcopy(self.fixture["automatic"])
                value["evidence"][field] = invalid
                self.write("delivery-automatic.json", value)
                self.assertFalse(all(check.evaluate_guest(self.results).values()))

    def test_manual_counter_must_be_continuous_between_delivery_phases(self):
        value = copy.deepcopy(self.fixture["recovery"])
        value["evidence"].update(manualFlushCountBefore=3, manualFlushCountAfter=3)
        self.write("delivery-recovery.json", value)
        self.assertFalse(check.evaluate_guest(self.results)["delivery_manual_counter_continuity"])

    def test_named_delivery_checks_are_required(self):
        for scenario, names in (("automatic", check.AUTOMATIC_CHECKS), ("recovery", check.RECOVERY_CHECKS)):
            for name in names:
                with self.subTest(scenario=scenario, name=name):
                    value = copy.deepcopy(self.fixture[scenario])
                    del value["checks"][name]
                    self.write(f"delivery-{scenario}.json", value)
                    self.assertFalse(check.evaluate_guest(self.results)[scenario + "_required_checks"])
                    self.write(f"delivery-{scenario}.json", self.fixture[scenario])

    def test_recovery_requires_outage_health_queue_and_fresh_delivery_evidence(self):
        mutations = (
            ("probes", []), ("faultActiveAt", None), ("encoderCounts", []),
            ("baselineEncoderCount", 1), ("encoderPeak", 999), ("queueDrained", False),
            ("freshAcknowledgements", []), ("failureAttempts", []), ("unexpectedErrors", ["relay failure"]),
            ("freshCreatedAt", 1), ("freshMarker", "fault-http503"),
        )
        for field, invalid in mutations:
            with self.subTest(field=field):
                value = copy.deepcopy(self.fixture["recovery"])
                value["evidence"]["cases"][0][field] = invalid
                self.write("delivery-recovery.json", value)
                self.assertFalse(all(check.evaluate_guest(self.results).values()))
        value = copy.deepcopy(self.fixture["recovery"])
        value["evidence"]["cases"][0]["freshAcknowledgements"][0]["outcome"] = "injected-http503"
        self.write("delivery-recovery.json", value)
        self.assertFalse(check.evaluate_guest(self.results)["recovery_http503_fresh_acknowledged"])

    def test_capture_only_success_is_not_native_agent_acknowledgement(self):
        value = copy.deepcopy(self.fixture["automatic"])
        value["evidence"]["forwardToAgent"] = False
        value["evidence"]["successfulRequests"][0]["forwarded"] = False
        self.write("delivery-automatic.json", value)
        self.assertFalse(check.evaluate_guest(self.results)["automatic_trace_acknowledged"])

    def test_final_hook_evidence_only_required_after_cleanup(self):
        (self.results / "termination-armed.json").unlink()
        self.assertTrue(all(check.evaluate_guest(self.results).values()))
        with self.assertRaises(FileNotFoundError):
            check.evaluate(self.results, guest_only=True)

    def test_missing_or_mismatched_termination_marker_log_fails(self):
        for mutation in ("missing", "marker", "spanId", "runtimeId", "stream"):
            with self.subTest(mutation=mutation):
                cloudwatch = copy.deepcopy(self.fixture["cloudwatch"])
                event = cloudwatch["events"][-1]
                row = json.loads(event["message"])
                if mutation == "missing":
                    cloudwatch["events"].pop()
                elif mutation == "stream":
                    event["logStreamName"] = "stream/vm-2"
                else:
                    row[mutation] = "wrong"
                    event["message"] = json.dumps(row)
                self.write("cloudwatch.json", cloudwatch)
                status, result = self.cached_evaluate()
                self.assertEqual(status, 1)
                self.assertFalse(result["checks"]["cloudwatch_termination_marker_log_matches"])

    def test_termination_result_cannot_reuse_pretermination_feature_result(self):
        cloudwatch = copy.deepcopy(self.fixture["cloudwatch"])
        row = json.loads(cloudwatch["events"][-2]["message"])
        row["result"]["state"]["terminateCount"] = 0
        cloudwatch["events"][-2]["message"] = json.dumps(row)
        self.write("cloudwatch.json", cloudwatch)
        status, result = self.cached_evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["cloudwatch_termination_result_matches"])

    def test_each_named_termination_check_and_native_ack_required(self):
        for field in (*check.TERMINATION_CHECKS, "failed_request", "manual_flush"):
            with self.subTest(field=field):
                row = {"msg": "microvm_js_termination_result", "version": "Run-1",
                       "result": copy.deepcopy(self.fixture["termination"])}
                if field == "failed_request":
                    row["result"]["evidence"]["successfulRequests"][0]["outcome"] = "injected-disconnect"
                elif field == "manual_flush":
                    row["result"]["evidence"]["manualFlushCountAfter"] += 1
                else:
                    del row["result"]["checks"][field]
                self.assertFalse(check.termination_result_matches(
                    row, self.fixture["arm"], self.fixture["guests"]["resumed-3"], "Run-1"))

    def test_missing_new_backend_metric_or_final_logs_fails(self):
        for name in ("automatic", "recovery_http503", "recovery_disconnect", "termination"):
            with self.subTest(name=name):
                backend = copy.deepcopy(self.fixture["backend"])
                backend["delivery_" + name] = {"series": []}
                status, result = self.cached_evaluate(backend)
                self.assertEqual(status, 1)
                self.assertFalse(result["checks"]["datadog_delivery_" + name + "_hits"])
        for remove in ("microvm_js_termination_log", "microvm_js_termination_result"):
            with self.subTest(remove=remove):
                backend = copy.deepcopy(self.fixture["backend"])
                backend["logs"]["data"] = [row for row in backend["logs"]["data"]
                                           if row["attributes"]["message"] != remove]
                status, result = self.cached_evaluate(backend)
                self.assertEqual(status, 1)
                self.assertFalse(result["checks"]["datadog_termination_marker_log_matches"])

    def test_missing_named_backend_response_never_falls_back_to_network(self):
        for name in ("delivery_automatic", "delivery_recovery_http503", "delivery_recovery_disconnect",
                     "delivery_termination", "logs"):
            with self.subTest(name=name):
                backend = copy.deepcopy(self.fixture["backend"])
                del backend[name]
                with self.assertRaisesRegex(ValueError, "missing backend payload"):
                    self.cached_evaluate(backend)

    def test_backend_nested_span_fields_match_final_delivery(self):
        backend = copy.deepcopy(self.fixture["backend"])
        result = backend["logs"]["data"][-2]["attributes"]["attributes"]["result"]
        span = result["evidence"]["successfulRequests"][0]["spans"][0]
        span["meta"]["verify"] = {"delivery": {"marker": span["meta"].pop("verify.delivery.marker")}}
        span["metrics"]["_dd"] = {"measured": span["metrics"].pop("_dd.measured")}
        status, replay = self.cached_evaluate(backend)
        self.assertEqual(status, 0)
        for name in ("termination_result_matches", "termination_marker_log_matches",
                     "termination_span_matches_cloudwatch"):
            self.assertTrue(replay["checks"]["datadog_" + name])
        self.assertEqual(replay["evidence"], backend)

    def test_nested_span_fields_still_require_exact_marker_and_measured_value(self):
        for field in ("marker", "measured"):
            for invalid in (None, "wrong", 0):
                with self.subTest(field=field, invalid=invalid):
                    row = {"msg": "microvm_js_termination_result", "version": "Run-1",
                           "result": copy.deepcopy(self.fixture["termination"])}
                    span = row["result"]["evidence"]["successfulRequests"][0]["spans"][0]
                    marker = span["meta"].pop("verify.delivery.marker")
                    measured = span["metrics"].pop("_dd.measured")
                    span["meta"]["verify"] = {"delivery": {"marker": marker}}
                    span["metrics"]["_dd"] = {"measured": measured}
                    target = span["meta"]["verify"]["delivery"] if field == "marker" else span["metrics"]["_dd"]
                    if invalid is None:
                        del target[field]
                    else:
                        target[field] = invalid
                    self.assertFalse(check.termination_result_matches(
                        row, self.fixture["arm"], self.fixture["guests"]["resumed-3"], "Run-1"))

    def test_backend_termination_span_must_match_cloudwatch_native_result(self):
        backend = copy.deepcopy(self.fixture["backend"])
        result = backend["logs"]["data"][-2]["attributes"]["attributes"]["result"]
        identity = {"traceId": "999", "spanId": "19999"}
        result["evidence"]["span"] = identity
        result["evidence"]["successfulRequests"] = [trace_request(
            identity, "verify.js.termination", "runtime-1", self.fixture["arm"]["marker"])]
        backend["logs"]["data"][-1]["attributes"]["attributes"].update(identity)
        status, replay = self.cached_evaluate(backend)
        self.assertEqual(status, 1)
        self.assertFalse(replay["checks"]["datadog_termination_span_matches_cloudwatch"])

    def test_termination_rc_normalization_is_limited_to_known_identity_nulls(self):
        row = {"msg": "microvm_js_termination_result", "version": "Run-1",
               "result": copy.deepcopy(self.fixture["termination"])}
        state = row["result"]["state"]
        for value in (state, state["before"], state["run"]):
            del value["rcClientId"]
        self.assertTrue(check.termination_result_matches(
            row, self.fixture["arm"], self.fixture["guests"]["resumed-3"], "Run-1"))
        for value in (state, state["before"], state["run"]):
            value["rcClientId"] = "unexpected"
            self.assertFalse(check.termination_result_matches(
                row, self.fixture["arm"], self.fixture["guests"]["resumed-3"], "Run-1"))
            del value["rcClientId"]
        value = copy.deepcopy(self.fixture["guests"]["resumed-3"])
        value["state"]["unrelated"] = None
        self.assertFalse(check.termination_result_matches(row, self.fixture["arm"], value, "Run-1"))


if __name__ == "__main__":
    unittest.main()
