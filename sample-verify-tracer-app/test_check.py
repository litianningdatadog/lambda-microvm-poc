"""Evaluator regressions from the first real AWS run; no credentials required."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check
from feature_checks import FEATURE_OPERATIONS
from feature_checks import evaluate_features
from lifecycle_checks import SCHEMA
from lifecycle_checks import TELEMETRY_CHECKS
from lifecycle_checks import required_telemetry_checks


class BackendLogEvidenceTest(unittest.TestCase):
    def test_backend_feature_log_may_omit_empty_errors(self):
        reference = {"features": {"errors": [], "roots": {"manual": {"span_id": "123"}}}}
        backend = copy.deepcopy(reference)
        del backend["features"]["errors"]
        self.assertTrue(check.log_values_equal(
            check._restore_backend_empty_errors(backend, reference), reference,
        ))
        for invalid in (None, {}, "", ["feature failed"]):
            with self.subTest(invalid=invalid):
                backend["features"]["errors"] = invalid
                self.assertFalse(check.log_values_equal(
                    check._restore_backend_empty_errors(backend, reference), reference,
                ))

    def test_backend_termination_log_may_omit_empty_errors(self):
        from test_delivery_checks import RUNTIME, arm, phase

        reference = phase("termination")
        backend = copy.deepcopy(reference)
        del backend["evidence"]["unexpected_errors"]
        normalized = check._restore_backend_empty_errors(backend, reference)
        self.assertTrue(all(check.evaluate_termination(arm(), normalized, RUNTIME).values()))
        self.assertTrue(check.log_values_equal(normalized, reference))
        for invalid in (None, {}, "", ["upstream failed"]):
            with self.subTest(invalid=invalid):
                backend["evidence"]["unexpected_errors"] = invalid
                normalized = check._restore_backend_empty_errors(backend, reference)
                self.assertFalse(check.evaluate_termination(
                    arm(), normalized, RUNTIME,
                )["delivery_no_unexpected_errors"])

    def test_backend_omissions_cannot_hide_reference_errors_or_other_missing_fields(self):
        for section, field in (("features", "errors"), ("evidence", "unexpected_errors")):
            for errors in (None, ["observed failure"]):
                with self.subTest(section=section, errors=errors):
                    reference = {section: {field: errors, "other": []}}
                    backend = {section: {"other": []}}
                    self.assertFalse(check.log_values_equal(
                        check._restore_backend_empty_errors(backend, reference), reference,
                    ))
            reference = {section: {field: [], "other": []}}
            for backend in ({}, {section: None}, {section: {}}):
                with self.subTest(section=section, backend=backend):
                    self.assertFalse(check.log_values_equal(
                        check._restore_backend_empty_errors(backend, reference), reference,
                    ))


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name)
        self.version = "20261031T233010Z-50769"
        self.guest = {
            "checks": {"identity_rotated": True},
            "passed": True,
            "runtime_id": "new-runtime",
            "verification_id": self.version,
            "wheel_sha256": "wheel",
        }
        self.write("verification.json", self.guest)
        self.write("deployment.json", {"verification_id": self.version, "wheel_sha256": "wheel", "start": 1000})
        self.write("vm.json", {"start": 1010, "end": 1020, "microvmId": "vm-test"})
        self.write(
            "cloudwatch.json",
            {
                "events": [
                    {"logStreamName": "version/vm-test", "message": json.dumps(self.guest)},
                ]
            },
        )
        self.log = {"attributes": {"message": "tracer_verification", "attributes": copy.deepcopy(self.guest)}}
        self.runtime_present = True
        self.log_present = True
        self.stale_present = False
        self.missing_feature = None
        self.feature_errors_present = True

    def write(self, name, value):
        (self.results / name).write_text(json.dumps(value))

    def dd(self, path, params=None, body=None):
        if path.endswith("/all-tags"):
            return {"data": {"attributes": {"tags": ["runtime-id:new-runtime"]}}}
        if path.endswith("/search"):
            query = body["filter"]["query"]
            # msg is remapped into message by the real log pipeline.
            self.assertNotIn("@msg:", query)
            self.assertIn("@verification_id:" + self.version, query)
            return {"data": [self.log] if self.log_present else []}
        query = params["query"]
        # Metric tags are lowercased, but the log's verification_id above is not.
        self.assertIn("version:" + self.version.lower(), query)
        self.assertNotIn("version:" + self.version + ",", query)
        if "runtime.python.mem.rss" in query:
            self.assertIn("runtime-id:new-runtime", query)
            value = 100 if self.runtime_present else None
        elif "trace.verify.fresh" in query:
            value = 1
        elif "trace.verify.feature." in query:
            value = 1
            if self.missing_feature and "trace." + FEATURE_OPERATIONS[self.missing_feature] + ".hits" in query:
                value = None
            if ".errors{" in query and not self.feature_errors_present:
                value = None
        else:
            value = 1 if self.stale_present else None
        return {"series": [{"pointlist": [[1010000, value]]}]}

    def evaluate(self, output=None, check_runtime_metrics=False):
        with patch.object(check, "dd", self.dd), contextlib.redirect_stdout(io.StringIO()):
            status = check.evaluate(self.results, output, check_runtime_metrics=check_runtime_metrics)
        return status, json.loads((output or self.results / "evaluation.json").read_text())

    def enable_features(self):
        deployment = json.loads((self.results / "deployment.json").read_text())
        deployment["verification_schema"] = 2
        self.write("deployment.json", deployment)
        self.guest["verification_schema"] = 2
        self.guest["checks"].update({name: True for name in TELEMETRY_CHECKS})
        self.guest["features"] = {"roots": {case: {} for case in FEATURE_OPERATIONS}}
        self.guest["checks"].update({name: True for name in evaluate_features([], {}, "new-runtime")})
        self.save_guest()

    def save_guest(self):
        self.write("verification.json", self.guest)
        self.log["attributes"]["attributes"] = copy.deepcopy(self.guest)
        self.write(
            "cloudwatch.json", {"events": [{"logStreamName": "version/vm-test", "message": json.dumps(self.guest)}]}
        )

    def test_normalized_metric_tags_and_remapped_log(self):
        status, result = self.evaluate()
        self.assertEqual(status, 0)
        self.assertTrue(result["passed"])

    def test_missing_runtime_metric_still_fails(self):
        self.runtime_present = False
        status, result = self.evaluate(check_runtime_metrics=True)
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_runtime_metrics_new_identity"])
        self.assertTrue(result["diagnostics"]["runtime_id_present_in_metric_tag_inventory"])

    def test_runtime_metric_explicitly_skipped_by_default(self):
        self.runtime_present = False
        original = self.dd

        def without_rss(path, params=None, body=None):
            self.assertNotIn("runtime.python.mem.rss", path)
            self.assertNotIn("runtime.python.mem.rss", (params or {}).get("query", ""))
            return original(path, params, body)

        with patch.object(check, "dd", without_rss), contextlib.redirect_stdout(io.StringIO()) as output:
            status = check.evaluate(self.results)
        result = json.loads((self.results / "evaluation.json").read_text())
        self.assertEqual(status, 0)
        self.assertIn(check.RSS_CHECK, result["skipped"])
        self.assertNotIn(check.RSS_CHECK, result["checks"])
        self.assertIn("SKIP " + check.RSS_CHECK, output.getvalue())

    def test_feature_suite_requires_backend_arrival(self):
        self.enable_features()
        self.missing_feature = "database"
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_feature_database_arrived"])

    def test_feature_suite_requires_backend_errors(self):
        self.enable_features()
        self.feature_errors_present = False
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_feature_error_reported"])

    def test_feature_suite_cannot_omit_guest_assertions(self):
        self.enable_features()
        del self.guest["checks"]["feature_async_parenting"]
        self.save_guest()
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["guest_feature_scenarios_present"])

    def test_expanded_suite_passes_with_rss_skipped(self):
        self.enable_features()
        self.runtime_present = False
        status, result = self.evaluate()
        self.assertEqual(status, 0)
        self.assertTrue(result["checks"]["guest_feature_scenarios_present"])
        self.assertTrue(all(result["checks"].values()))
        self.assertIn(check.RSS_CHECK, result["skipped"])

    def test_telemetry_checks_cannot_be_omitted_in_schema_two(self):
        self.enable_features()
        for name in TELEMETRY_CHECKS:
            with self.subTest(name=name):
                removed = self.guest["checks"].pop(name)
                self.save_guest()
                status, result = self.evaluate()
                self.assertEqual(status, 1)
                self.assertFalse(result["checks"]["guest_telemetry_scenarios_present"])
                self.guest["checks"][name] = removed

    def test_historical_schema_does_not_require_new_duplicate_instrumentation_check(self):
        for schema in (2, 3):
            self.enable_features()
            deployment = json.loads((self.results / "deployment.json").read_text())
            deployment["verification_schema"] = schema
            self.write("deployment.json", deployment)
            self.guest["verification_schema"] = schema
            self.guest["checks"].update({name: True for name in required_telemetry_checks(schema)})
            self.guest["checks"].pop("feature_http_single_instrumentation")
            self.save_guest()
            self.assertEqual(self.evaluate()[0], 0)

    def test_schema_three_requires_every_new_telemetry_check(self):
        self.enable_features()
        deployment = json.loads((self.results / "deployment.json").read_text())
        deployment["verification_schema"] = 3
        self.write("deployment.json", deployment)
        self.guest["verification_schema"] = 3
        self.guest["checks"].update({name: True for name in required_telemetry_checks(3)})
        self.save_guest()
        self.assertEqual(self.evaluate()[0], 0)
        for name in required_telemetry_checks(3):
            with self.subTest(name=name):
                self.guest["checks"].pop(name)
                self.save_guest()
                self.assertEqual(self.evaluate()[0], 1)
                self.guest["checks"][name] = False
                self.save_guest()
                self.assertEqual(self.evaluate()[0], 1)
                self.guest["checks"][name] = True

    def test_diagnostic_copy_does_not_hide_missing_standard_metric(self):
        self.runtime_present = False
        deployment = json.loads((self.results / "deployment.json").read_text())
        deployment["runtime_rss_alias"] = "mvmtest.runtime_rss_control"
        self.write("deployment.json", deployment)
        standard_dd = self.dd

        def with_control(path, params=None, body=None):
            if params and "mvmtest.runtime_rss_control" in params.get("query", ""):
                return {"series": [{"pointlist": [[1010000, 100]]}]}
            return standard_dd(path, params, body)

        with patch.object(check, "dd", with_control), contextlib.redirect_stdout(io.StringIO()):
            status = check.evaluate(self.results, check_runtime_metrics=True)
        result = json.loads((self.results / "evaluation.json").read_text())
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_runtime_metrics_new_identity"])
        self.assertTrue(result["diagnostics"]["runtime_rss_alias_arrived"])

    def test_missing_log_still_fails(self):
        self.log_present = False
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_verification_log_arrived"])

    def test_mismatched_log_evidence_fails(self):
        self.log["attributes"]["attributes"]["checks"] = {"identity_rotated": False}
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_verification_log_arrived"])

    def test_stale_trace_leak_fails(self):
        self.stale_present = True
        status, result = self.evaluate()
        self.assertEqual(status, 1)
        self.assertFalse(result["checks"]["datadog_stale_absent"])

    def test_output_override_preserves_original_evaluation(self):
        original = self.results / "evaluation.json"
        original.write_text("original evidence\n")
        status, result = self.evaluate(self.results / "recheck.json")
        self.assertEqual(status, 0)
        self.assertTrue(result["passed"])
        self.assertEqual(original.read_text(), "original evidence\n")


if __name__ == "__main__":
    unittest.main()
