"""Run with python3 -m unittest test_delivery (no live AWS or Datadog calls)."""
import copy
import http.client
import json
import socket
import threading
import unittest
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from unittest.mock import Mock, patch

import app
from datadog import DogStatsd
from delivery import Delivery, DeliveryError, RelayServer, relay_handler, start_relays


@contextmanager
def serving(handler):
    server = RelayServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.statsd = Mock()
        self.delivery = Delivery(self.statsd, emit=lambda row: self.rows.append(copy.deepcopy(row)))

    def test_snapshot_run_identity_and_three_cycles(self):
        self.delivery.on_hook("ready")
        self.assertIsNone(self.delivery.state()["runId"])
        with self.assertRaises(DeliveryError):
            self.delivery.marker("initial", str(uuid.uuid4()))
        self.delivery.on_hook("run")
        run_id = self.delivery.state()["runId"]
        initial = self.delivery.marker("initial", str(uuid.uuid4()))
        self.assertEqual(initial["counters"], dict(runCount=1, resumeCount=0, suspendCount=0, terminateCount=0))
        for cycle in range(1, 4):
            self.delivery.on_hook("suspend")
            self.delivery.on_hook("resume")
            row = self.delivery.marker(f"resumed-{cycle}", str(uuid.uuid4()))
            self.assertEqual(row["runId"], run_id)
            self.assertEqual(row["counters"], dict(runCount=1, resumeCount=cycle, suspendCount=cycle, terminateCount=0))
        marker = str(uuid.uuid4())
        arm = self.delivery.arm(marker)
        self.assertEqual(arm, self.delivery.arm(marker))
        self.assertTrue(arm["armed"])
        self.assertEqual(len(self.rows), 4)
        with self.assertRaises(DeliveryError):
            self.delivery.arm(str(uuid.uuid4()))
        with self.assertRaises(DeliveryError):
            self.delivery.marker("termination", marker)
        self.delivery.on_hook("terminate")
        self.delivery.on_hook("terminate")
        final = self.rows[-1]
        self.assertEqual(len(self.rows), 5)
        self.assertEqual(final["marker"], marker)
        self.assertEqual(final["scenario"], "termination")
        self.assertGreaterEqual(final["wall"], arm["wall"])
        self.assertEqual(final["counters"], dict(runCount=1, resumeCount=3, suspendCount=3, terminateCount=1))
        self.assertEqual(arm["counters"]["terminateCount"], 0)
        self.assertEqual(self.statsd.gauge.call_count, 5)

    def test_marker_idempotency_and_validation(self):
        self.delivery.on_hook("run")
        marker = str(uuid.uuid4())
        row = self.delivery.marker("initial", marker)
        returned = self.delivery.marker("initial", marker)
        self.assertEqual(row, returned)
        returned["counters"]["runCount"] = 99
        self.assertEqual(self.delivery.marker("initial", marker), row)
        self.assertEqual(len(self.rows), 1)
        for scenario, invalid in (("unknown", str(uuid.uuid4())), ("initial", "bad"), ("resumed-1", marker)):
            with self.assertRaises(DeliveryError):
                self.delivery.marker(scenario, invalid)
        with self.assertRaises(DeliveryError):
            self.delivery.arm(marker)

    def test_real_udp_marker_has_service_and_no_idempotent_duplicate(self):
        for configured_service in (None, "delivery-test-service"):
            with self.subTest(service=configured_service):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    receiver.bind(("127.0.0.1", 0))
                    receiver.settimeout(1)
                    statsd = DogStatsd(host="127.0.0.1", port=receiver.getsockname()[1],
                                      disable_buffering=True, disable_aggregation=True)
                    rows = []
                    delivery = Delivery(statsd, emit=rows.append)
                    delivery.on_hook("run")
                    marker = str(uuid.uuid4())
                    environment = {} if configured_service is None else {"DD_SERVICE": configured_service}
                    with patch.dict("os.environ", environment) as configured:
                        if configured_service is None:
                            configured.pop("DD_SERVICE", None)
                        row = delivery.marker("initial", marker)
                    packet = receiver.recv(65535).decode()
                    metric, tags = packet.split("|#", 1)
                    self.assertEqual(metric, "mvmtest.delivery:42|g")
                    self.assertTrue({
                        f"marker:{marker}", "scenario:initial", f"run_id:{row['runId']}",
                        f"service:{configured_service or 'sample-verify-app'}"
                    }.issubset(set(tags.partition("|")[0].strip().split(","))))
                    self.assertEqual(delivery.marker("initial", marker), row)
                    receiver.settimeout(0.1)
                    with self.assertRaises(socket.timeout):
                        receiver.recv(65535)
                    self.assertEqual(rows, [row])
                    statsd.close_socket()

    def test_bounded_evidence_is_not_silently_truncated(self):
        delivery = Delivery(self.statsd, max_requests=1)
        row = delivery.begin_request("logs")
        delivery.finish_request(row, "injected-http503", 503)
        self.assertIsNone(delivery.begin_request("metrics"))
        self.assertEqual(len(delivery.requests), 1)
        with self.assertRaises(DeliveryError) as caught:
            delivery.state()
        self.assertEqual(caught.exception.status, 507)
        delivery.mode = "http503"
        with self.assertRaises(DeliveryError):
            delivery.fault("normal")
        self.assertEqual(delivery.mode, "normal")

    def test_http_routes_and_hooks(self):
        with patch.object(app, "delivery", self.delivery), patch.object(app.tracer, "flush") as flush:
            with serving(app.Handler) as port, serving(app.HookHandler) as hook_port:
                self.assertEqual(request(port, "GET", "/"), (200, b"ok"))
                self.assertEqual(request(port, "GET", "/unknown")[0], 404)
                self.assertEqual(request(port, "POST", "/unknown")[0], 404)
                self.assertEqual(request(hook_port, "POST", "/unknown/run")[0], 404)
                self.assertEqual(request(hook_port, "POST", "/run")[0], 200)
                marker = str(uuid.uuid4())
                headers = {"Content-Type": "application/json"}
                for invalid in ("{", "[]", '{"marker":"bad"}', '{"scenario":[],"marker":"bad"}'):
                    self.assertEqual(request(port, "POST", "/delivery/marker", invalid, headers)[0], 400)
                body = json.dumps(dict(scenario="initial", marker=marker))
                self.assertEqual(request(port, "POST", "/delivery/marker", body)[0], 400)
                status, data = request(port, "POST", "/delivery/marker", body, headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data), self.rows[-1])
                self.assertEqual(request(port, "POST", "/delivery/marker", body, headers), (status, data))
                self.assertEqual(flush.call_count, 1)  # Existing hook flush only; never marker-triggered.
                self.assertEqual(json.loads(request(port, "GET", "/delivery/state")[1])["runId"], self.rows[-1]["runId"])
        with patch.object(app, "delivery", None), serving(app.Handler) as port:
            self.assertEqual(request(port, "GET", "/legacy"), (200, b"ok"))
            self.assertEqual(request(port, "POST", "/delivery/fault", b"{}")[0], 404)

    def test_real_wire_faults_and_recovery_both_pipelines(self):
        captured = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                captured.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(202)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        with serving(Upstream) as upstream_port:
            real_connection = http.client.HTTPConnection
            with patch("delivery.http.client.HTTPSConnection", side_effect=lambda host, timeout: real_connection("127.0.0.1", upstream_port, timeout=timeout)) as tls:
                for pipeline in ("logs", "metrics"):
                    with serving(relay_handler(self.delivery, pipeline, "fixed.datadoghq.com")) as port:
                        body = b"\x1f\x8bcompressed\x00payload"
                        headers = {"DD-API-KEY": "test-secret", "Content-Encoding": "gzip", "Connection": "close, X-Hop", "X-Hop": "remove", "X-End-To-End": "keep"}
                        for mode in ("normal", "http503", "disconnect", "normal"):
                            self.delivery.fault(mode)
                            if mode == "disconnect":
                                with self.assertRaises(http.client.RemoteDisconnected):
                                    request(port, "POST", "/api/v2/series?test=1", body, headers)
                            else:
                                expected = 503 if mode == "http503" else 202
                                self.assertEqual(request(port, "POST", "/api/v2/series?test=1", body, headers)[0], expected)
                self.assertEqual(tls.call_count, 4)
        self.assertEqual(len(captured), 4)
        for path, headers, received in captured:
            self.assertEqual(path, "/api/v2/series?test=1")
            self.assertEqual(received, body)
            self.assertEqual(headers["DD-API-KEY"], "test-secret")
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertEqual(headers["X-End-To-End"], "keep")
            self.assertNotIn("X-Hop", headers)
        rows = self.delivery.state()["requests"]
        self.assertEqual(len(rows), 8)
        for pipeline in ("logs", "metrics"):
            outcomes = [row["outcome"] for row in rows if row["pipeline"] == pipeline]
            self.assertEqual(outcomes, ["accepted", "injected-http503", "injected-disconnect", "accepted"])
        for row in rows:
            self.assertEqual(set(row), {"pipeline", "mode", "outcome", "status", "receivedAt", "completedAt"})
            self.assertGreaterEqual(row["completedAt"], row["receivedAt"])
        self.assertNotIn("test-secret", json.dumps(rows))
        self.assertNotIn("payload", json.dumps(rows))

    def test_chunked_agent_body_is_forwarded_without_reencoding(self):
        captured = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                captured.append((self.rfile.read(int(self.headers["Content-Length"])),
                                 self.headers.get("Transfer-Encoding"), self.headers.get("DD-API-KEY")))
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        with serving(Upstream) as upstream_port:
            real_connection = http.client.HTTPConnection
            with patch("delivery.http.client.HTTPSConnection", side_effect=lambda host, timeout: real_connection("127.0.0.1", upstream_port, timeout=timeout)):
                with serving(relay_handler(self.delivery, "metrics", "app.datadoghq.com")) as port:
                    connection = real_connection("127.0.0.1", port, timeout=3)
                    try:
                        connection.request("POST", "/api/v2/series?api_key=not-recorded",
                                           body=iter([b"\x1f\x8bfirst", b"second\x00"]),
                                           headers={"DD-API-KEY": "not-recorded"}, encode_chunked=True)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 202)
                        response.read()
                    finally:
                        connection.close()
        self.assertEqual(captured, [(b"\x1f\x8bfirstsecond\x00", None, "not-recorded")])
        self.assertNotIn("not-recorded", json.dumps(self.delivery.state()))

    def test_metrics_validation_get_bypasses_faults_without_recording_secret(self):
        captured = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                captured.append((self.path, self.headers.get("DD-API-KEY"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "15")
                self.end_headers()
                self.wfile.write(b'{"valid":false}')

        with serving(Upstream) as upstream_port:
            real_connection = http.client.HTTPConnection
            with patch("delivery.http.client.HTTPSConnection", side_effect=lambda host, timeout: real_connection("127.0.0.1", upstream_port, timeout=timeout)):
                with serving(relay_handler(self.delivery, "metrics", "app.datadoghq.com")) as port:
                    for mode in ("normal", "http503", "disconnect"):
                        self.delivery.fault(mode)
                        result = request(port, "GET", "/api/v1/validate?api_key=validation-secret",
                                         b"original", {"DD-API-KEY": "validation-header"})
                        self.assertEqual(result, (403, b'{"valid":false}'))
                    self.assertEqual(request(port, "GET", "/api/v1/validate")[0], 403)
                    for path in ("/", "/api/v1/validate/other", "/api/v2/series",
                                 "https://attacker.example/api/v1/validate"):
                        self.assertEqual(request(port, "GET", path)[0], 404)
                with serving(relay_handler(self.delivery, "logs", "http-intake.logs.datadoghq.com")) as port:
                    self.assertEqual(request(port, "GET", "/api/v1/validate?api_key=validation-secret")[0], 404)
        self.assertEqual(len(captured), 4)
        self.assertEqual(captured[:3], [
            ("/api/v1/validate?api_key=validation-secret", "validation-header", b"original")
        ] * 3)
        self.assertEqual(captured[3], ("/api/v1/validate", None, b""))
        self.assertEqual(self.delivery.state()["requests"], [])
        self.assertNotIn("validation-secret", json.dumps(self.delivery.state()))
        self.assertNotIn("validation-header", json.dumps(self.delivery.state()))

    def test_upstream_error_not_accepted(self):
        with patch("delivery.http.client.HTTPSConnection", side_effect=OSError("private detail")):
            with serving(relay_handler(self.delivery, "logs", "fixed.datadoghq.com")) as port:
                self.assertEqual(request(port, "POST", "/api/v2/logs", b"data")[0], 502)
        row = self.delivery.state()["requests"][0]
        self.assertEqual(row["outcome"], "upstream-error")
        self.assertIsNone(row["status"])
        self.assertNotIn("private detail", json.dumps(row))

    def test_untrusted_site_cannot_redirect_agent_credentials(self):
        with patch.dict("os.environ", {"DD_SITE": "attacker.example/path"}):
            with self.assertRaises(ValueError):
                start_relays(self.delivery)


if __name__ == "__main__":
    unittest.main()
