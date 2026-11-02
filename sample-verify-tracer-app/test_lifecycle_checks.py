"""Negative cases for subscriber membership and native telemetry evidence."""

import copy
import gc
from types import SimpleNamespace
import unittest
import weakref

from lifecycle_checks import exporter_metrics
from lifecycle_checks import subscriber_count


class Subscriber:
    def _on_telemetry_worker_changed(self, worker):
        pass


class LifecycleCheckTest(unittest.TestCase):
    def test_membership_ignores_bookkeeping_flag(self):
        old, current = Subscriber(), Subscriber()
        old._telemetry_worker_subscribed = False
        refs = [
            weakref.WeakMethod(old._on_telemetry_worker_changed),
            weakref.WeakMethod(current._on_telemetry_worker_changed),
        ]
        writer = SimpleNamespace(_worker_subscribers=refs)
        self.assertEqual(subscriber_count(writer, old), 1)
        refs.pop(0)
        self.assertEqual(subscriber_count(writer, old), 0)
        refs.append(weakref.WeakMethod(current._on_telemetry_worker_changed))
        self.assertEqual(subscriber_count(writer, current), 2)
        del current
        gc.collect()
        self.assertEqual(subscriber_count(writer, old), 0)

    def payload(self):
        return {
            "runtime_id": "new",
            "request_type": "message-batch",
            "payload": [
                {
                    "request_type": "generate-metrics",
                    "payload": {
                        "series": [
                            {
                                "namespace": "tracers",
                                "metric": name,
                                "tags": ["src_library:libdatadog"],
                                "points": [[1, 2]],
                            }
                            for name in ("trace_api.requests", "trace_api.responses")
                        ],
                    },
                }
            ],
        }

    def test_native_batch_metrics_counted(self):
        self.assertEqual(exporter_metrics([self.payload()], "new"), {"trace_api.requests": 2, "trace_api.responses": 2})

    def test_direct_metrics_counted(self):
        payload = self.payload()["payload"][0]
        payload["runtime_id"] = "new"
        self.assertEqual(exporter_metrics([payload], "new")["trace_api.requests"], 2)

    def test_old_runtime_python_metrics_and_marker_cannot_pass(self):
        native = self.payload()
        native["runtime_id"] = "old"
        python = self.payload()
        for series in python["payload"][0]["payload"]["series"]:
            series["tags"] = []
        marker = copy.deepcopy(python)
        marker["payload"][0]["payload"]["series"][0]["metric"] = "verify.after_refresh"
        self.assertEqual(
            exporter_metrics([native, python, marker], "new"), {"trace_api.requests": 0, "trace_api.responses": 0}
        )

    def test_wrong_namespace_cannot_pass(self):
        payload = self.payload()
        for series in payload["payload"][0]["payload"]["series"]:
            series["namespace"] = "appsec"
        self.assertFalse(any(exporter_metrics([payload], "new").values()))


if __name__ == "__main__":
    unittest.main()
