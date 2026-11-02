"""Real HTTP regressions: decoded/forwarded receipts are not completed ACKs."""

import gzip
from http.client import HTTPConnection
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

import intake
import msgpack


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.flush()
        self.server.headers_sent.set()
        if self.server.mode == "hold":
            self.server.release.wait(5)
        self.wfile.write(b"{}")

    def do_PUT(self):
        self.server.received.append({
            "path": self.path,
            "method": self.command,
            "body": self.rfile.read(int(self.headers["Content-Length"])),
            "headers": dict(self.headers),
        })
        body = b'{"rate_by_service": {}}'
        self.send_response(503 if self.server.mode == "reject" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.flush()
        self.server.headers_sent.set()
        if self.server.mode == "hold" and not self.server.release.wait(5):
            return
        if self.server.mode == "partial":
            self.wfile.write(body[:3])
            self.close_connection = True
        else:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class IntakeDeliveryTest(unittest.TestCase):
    def setUp(self):
        intake.set_fault()
        with intake.records_lock:
            for values in intake.records.values():
                values.clear()
            intake.trace_requests.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.mode = "accept"
        self.upstream.received = []
        self.upstream.headers_sent = threading.Event()
        self.upstream.release = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), intake.Intake)
        self.server.daemon_threads = True
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (self.upstream, self.server)]
        for thread in self.threads:
            thread.start()
        self.forward_patch = patch.object(intake, "FORWARD", False)
        self.forward_patch.start()
        # Only redirect the fixed Agent address; all sockets/requests/responses are real.
        self.connection_patch = patch.object(
            intake,
            "HTTPConnection",
            lambda host, port, timeout: HTTPConnection("127.0.0.1", self.upstream.server_port, timeout=timeout),
        )
        self.connection_patch.start()
        self.clients = []

    def tearDown(self):
        self.upstream.release.set()
        intake.set_fault()
        for client in self.clients:
            client.join(6)
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        self.connection_patch.stop()
        self.forward_patch.stop()

    def payload(self):
        return msgpack.packb([[{
            "trace_id": (1 << 63) + 11,
            "span_id": (1 << 63) + 12,
            "parent_id": 0,
            "name": "verify.delivery.automatic",
            "resource": "verify.delivery.automatic",
            "service": "sample-verify-tracer-app",
            "start": 1793500000000000000,
            "duration": 100,
            "meta": {"runtime-id": "runtime", "verify.marker": "marker"},
            "metrics": {"_dd.measured": 1},
        }]], use_bin_type=True)

    def request(self, path="/v0.4/traces", body=None, headers=None, method="PUT"):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=6)
        try:
            connection.request(method, path, body=self.payload() if body is None else body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Expected wire evidence did not arrive")

    def completed(self):
        return [record for record in intake.snapshot_requests() if record["completed_at"] is not None]

    def test_capture_preserves_old_records_but_does_not_claim_forwarding(self):
        self.assertEqual(self.request()[0], 200)
        wire = self.wait_for(self.completed)[0]
        self.assertEqual(wire["outcome"], "accepted")
        self.assertFalse(wire["forwarded"])
        self.assertGreater(wire["completed_at"], wire["received_at"])
        self.assertEqual(wire["bytes"], len(self.payload()))
        self.assertEqual(wire["spans"][0]["trace_id"], str((1 << 63) + 11))
        self.assertEqual(wire["spans"][0]["span_id"], str((1 << 63) + 12))
        self.assertEqual(wire["spans"][0]["start"], "1793500000000000000")
        self.assertIsInstance(intake.records["traces"][0][0]["trace_id"], int)
        self.assertIsInstance(intake.records["traces"][0][0]["start"], int)
        wire["spans"].clear()
        self.assertTrue(intake.snapshot_requests()[0]["spans"])
        self.assertEqual(intake.snapshot_requests(wire["sequence"]), [])

    def test_forwarded_gzip_and_headers_reach_actual_agent(self):
        intake.FORWARD = True
        payload = gzip.compress(self.payload())
        self.assertEqual(self.request(body=payload, headers={"Content-Encoding": "gzip", "X-Datadog-Trace-Count": "1"})[0], 200)
        wire = self.wait_for(self.completed)[0]
        self.assertEqual(wire["outcome"], "accepted")
        self.assertTrue(wire["forwarded"])
        self.assertEqual(self.upstream.received[0]["body"], payload)
        self.assertEqual(self.upstream.received[0]["path"], "/v0.4/traces")
        self.assertEqual(self.upstream.received[0]["headers"]["Content-Encoding"], "gzip")
        self.assertEqual(self.upstream.received[0]["headers"]["X-Datadog-Trace-Count"], "1")
        self.assertEqual(intake.records["errors"], [])

    def test_upstream_headers_are_not_delivery_before_body_completes(self):
        intake.FORWARD = True
        self.upstream.mode = "hold"
        results = []
        client = threading.Thread(target=lambda: results.append(self.request()))
        self.clients.append(client)
        client.start()
        self.assertTrue(self.upstream.headers_sent.wait(3))
        wire = intake.snapshot_requests()[0]
        self.assertTrue(wire["forwarded"])
        self.assertIsNone(wire["completed_at"])
        self.assertNotEqual(wire["outcome"], "accepted")
        self.assertEqual(results, [])
        self.upstream.release.set()
        client.join(3)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.wait_for(self.completed)[0]["outcome"], "accepted")

    def test_partial_upstream_200_cannot_acknowledge_decoded_trace(self):
        intake.FORWARD = True
        self.upstream.mode = "partial"
        self.assertEqual(self.request()[0], 502)
        wire = self.wait_for(self.completed)[0]
        self.assertEqual(wire["status"], 502)
        self.assertEqual(wire["outcome"], "upstream_error")
        self.assertTrue(wire["forwarded"])
        self.assertTrue(wire["spans"])
        self.assertTrue(intake.records["traces"])
        self.assertTrue(any("IncompleteRead" in error for error in intake.records["errors"]))

    def test_unexpected_agent_rejection_is_not_ack_and_is_reported(self):
        intake.FORWARD = True
        self.upstream.mode = "reject"
        self.assertEqual(self.request()[0], 503)
        wire = self.wait_for(self.completed)[0]
        self.assertEqual(wire["outcome"], "rejected")
        self.assertTrue(wire["forwarded"])
        self.assertTrue(any("HTTP 503" in error for error in intake.records["errors"]))

    def test_disconnected_downstream_cannot_acknowledge_upstream_success(self):
        intake.FORWARD = True
        self.upstream.mode = "hold"
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            payload = self.payload()
            client.sendall(b"PUT /v0.4/traces HTTP/1.1\r\nHost: localhost\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            self.assertTrue(self.upstream.headers_sent.wait(3))
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            client.close()
        self.upstream.release.set()
        wire = self.wait_for(lambda: next((record for record in intake.snapshot_requests() if record["outcome"] == "response_error"), None))
        self.assertIsNone(wire["completed_at"])
        self.assertTrue(wire["forwarded"])
        self.assertTrue(intake.records["errors"])

    def test_cancelled_negotiation_is_retained_without_claiming_trace_delivery(self):
        intake.FORWARD = True
        self.upstream.mode = "hold"
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            client.sendall(b"GET /info HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(self.upstream.headers_sent.wait(3))
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            client.close()
        self.upstream.release.set()
        cancellation = self.wait_for(lambda: intake.records["cancelled_responses"])
        self.assertEqual(cancellation[0]["path"], "/info")
        self.assertEqual(intake.records["errors"], [])
        self.assertEqual(intake.snapshot_requests(), [])

    def test_controlled_faults_never_count_as_ack_and_do_not_pollute_errors(self):
        intake.FORWARD = True
        intake.set_fault("http503")
        self.assertEqual(self.request()[0], 503)
        rejected = self.wait_for(self.completed)[0]
        self.assertEqual(rejected["outcome"], "http503")
        self.assertFalse(rejected["forwarded"])
        intake.set_fault("disconnect")
        with self.assertRaises((RemoteDisconnected, ConnectionResetError)):
            self.request()
        wire = intake.snapshot_requests()[-1]
        self.assertEqual(wire["outcome"], "disconnect")
        self.assertIsNone(wire["completed_at"])
        self.assertIsNone(wire["status"])
        self.assertFalse(wire["forwarded"])
        self.assertEqual(intake.records["errors"], [])
        self.assertEqual(self.upstream.received, [])
        intake.set_fault()
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.wait_for(lambda: [record for record in self.completed() if record["outcome"] == "accepted"])[0]["forwarded"], True)

    def test_fault_is_trace_only_and_storage_remains_compatible(self):
        intake.set_fault("disconnect")
        telemetry = {"runtime_id": "runtime", "request_type": "app-started", "payload": {}}
        self.assertEqual(self.request("/telemetry/proxy/api/v2/apmtelemetry", json.dumps(telemetry).encode(), method="POST")[0], 200)
        self.assertEqual(intake.records["telemetry"], [telemetry])
        status, body = self.request("/info", b"", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("v0.4/traces", json.loads(body)["endpoints"])
        self.assertEqual(intake.snapshot_requests(), [])

    def test_decode_failures_are_recorded_not_accepted(self):
        self.assertEqual(self.request(body=b"not messagepack")[0], 400)
        wire = self.wait_for(self.completed)[0]
        self.assertEqual(wire["outcome"], "decode_error")
        self.assertEqual(wire["spans"], [])
        self.assertTrue(intake.records["errors"])

    def test_invalid_fault_does_not_silently_change_mode(self):
        with self.assertRaises(ValueError):
            intake.set_fault("unknown")
        self.assertEqual(self.request()[0], 200)


if __name__ == "__main__":
    unittest.main()
