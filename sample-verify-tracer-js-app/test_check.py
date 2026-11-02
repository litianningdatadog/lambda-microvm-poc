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
        for sample in check.SAMPLES:
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

if __name__ == "__main__":
    unittest.main()
