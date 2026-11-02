"""Negative cases ensure exported-span checks detect common tracing regressions."""

import unittest
import copy

from feature_checks import EXPECTED_ERROR
from feature_checks import FEATURE_OPERATIONS
from feature_checks import evaluate_features


class FeatureCheckTest(unittest.TestCase):
    def setUp(self):
        self.spans = []
        self.evidence = {"roots": {}, "errors": []}
        for index, (case, operation) in enumerate(FEATURE_OPERATIONS.items()):
            identity = {"trace_id": str(index + 100), "span_id": str(index + 1000), "full_trace_id": str(index + 100)}
            self.evidence["roots"][case] = identity
            self.add_span(case, int(identity["span_id"]), 0, name=operation, meta={"runtime-id": "new-runtime"})
        self.root("manual")["resource"] = "manual.after_refresh"
        child = self.add_span("manual", 2000, meta={"verify.case": "nested"}, metrics={"verify.count": 3})
        self.evidence["manual"] = {
            "child": child,
            "log": {"dd.trace_id": "100", "dd.span_id": "1000", "dd.service": "sample-verify-tracer-app"},
            "expected_log": {"trace_id": "100", "span_id": "1000"},
        }
        self.root("error").update(
            error=1,
            meta={
                "runtime-id": "new-runtime",
                "error.type": "builtins.ValueError",
                "error.message": EXPECTED_ERROR,
                "error.stack": "stack",
            },
        )
        self.add_span("database", 2001, type="sql", resource="SELECT 42 AS answer", meta={"db.system": "sqlite"})
        self.evidence["database"] = {"answer": 42}
        for index, case in enumerate(("async", "threads")):
            tasks = [{**self.add_span(case, 2100 + index * 2 + offset), "isolated": True} for offset in range(2)]
            self.evidence[case] = {"tasks": tasks, "parent_restored": True}
        for offset, status in enumerate(("200", "500")):
            error = int(status == "500")
            self.add_span(
                "http", 2200 + offset, error=error, meta={"component": "requests", "http.status_code": status}
            )
            server = self.add_span(
                "http",
                2300 + offset,
                2200 + offset,
                error=error,
                meta={"component": "flask", "http.status_code": status, "runtime-id": "new-runtime"},
            )
            if status == "200":
                self.evidence["http"] = {
                    "server": server,
                    "runtime_id": "new-runtime",
                    "traceparent": "00-...",
                    "w3c_trace_id": self.evidence["roots"]["http"]["full_trace_id"],
                    "w3c_parent_id": "2200",
                    "error_status": 500,
                }

    def root(self, case):
        name = FEATURE_OPERATIONS[case]
        return next(span for span in self.spans if span.get("name") == name)

    def add_span(self, case, span_id, parent_id=None, **fields):
        root = self.evidence["roots"][case]
        span = {
            "trace_id": int(root["trace_id"]),
            "span_id": span_id,
            "parent_id": int(root["span_id"]) if parent_id is None else parent_id,
            "service": "sample-verify-tracer-app",
            **fields,
        }
        self.spans.append(span)
        return {"trace_id": root["trace_id"], "span_id": str(span_id)}

    def checks(self):
        return evaluate_features(self.spans, self.evidence, "new-runtime")

    def test_complete_export_passes(self):
        checks = self.checks()
        self.assertTrue(all(checks.values()), checks)

    def test_missing_scenarios_cannot_pass(self):
        self.assertFalse(evaluate_features([], {}, "new-runtime")["feature_scenarios_completed"])

    def test_old_runtime_id_fails(self):
        self.root("database")["meta"]["runtime-id"] = "old-runtime"
        self.assertFalse(self.checks()["feature_runtime_identity"])

    def test_missing_database_instrumentation_fails(self):
        self.spans = [span for span in self.spans if span.get("type") != "sql"]
        self.assertFalse(self.checks()["feature_sqlite_instrumentation"])

    def test_error_without_stack_fails(self):
        del self.root("error")["meta"]["error.stack"]
        self.assertFalse(self.checks()["feature_error_capture"])

    def test_thread_context_crossing_fails(self):
        child = next(span for span in self.spans if span["span_id"] == 2102)
        child["parent_id"] = int(self.evidence["roots"]["async"]["span_id"])
        self.assertFalse(self.checks()["feature_thread_parenting"])

    def test_async_context_not_restored_fails(self):
        self.evidence["async"]["parent_restored"] = False
        self.assertFalse(self.checks()["feature_async_context_isolation"])

    def test_http_trace_break_fails(self):
        server = next(span for span in self.spans if span["span_id"] == 2300)
        server["parent_id"] = 0
        self.assertFalse(self.checks()["feature_flask_distributed_parenting"])

    def test_http_500_not_marked_error_fails(self):
        server = next(span for span in self.spans if span["span_id"] == 2301)
        server["error"] = 0
        self.assertFalse(self.checks()["feature_http_error_capture"])

    def test_log_without_correlation_fails(self):
        self.evidence["manual"]["log"]["dd.trace_id"] = "0"
        self.assertFalse(self.checks()["feature_log_correlation"])

    def test_retransmitted_span_ids_do_not_look_like_reinstrumentation(self):
        self.spans.extend(copy.deepcopy(self.spans))
        self.assertTrue(all(self.checks().values()))

    def test_additional_unique_client_or_server_span_is_duplicate_instrumentation(self):
        for component, parent in (("requests", None), ("flask", 2200)):
            with self.subTest(component=component):
                self.add_span("http", 9900, parent, meta={"component": component, "http.status_code": "200"})
                self.assertFalse(self.checks()["feature_http_single_instrumentation"])
                self.spans.pop()

    def test_w3c_parent_mismatch_fails(self):
        self.evidence["http"]["w3c_parent_id"] = "unrelated"
        self.assertFalse(self.checks()["feature_w3c_propagation"])


if __name__ == "__main__":
    unittest.main()
