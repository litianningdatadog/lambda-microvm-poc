"""Capture native exporter traffic, with response-complete delivery evidence."""

import copy
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
import os
import socket
import threading
import time

import msgpack


FORWARD = os.environ.get("VERIFY_FORWARD_TO_AGENT") == "true"
records = {"traces": [], "telemetry": [], "dogstatsd": [], "dogstatsd_alias": [], "errors": [], "cancelled_responses": []}
records_lock = threading.Lock()
trace_requests = []
# Diagnostic: arrival and upstream forwarding time per telemetry request (monotonic clock).
telemetry_requests = []


def _telemetry_kind(message):
    kind = message.get("request_type", "")
    if kind == "message-batch" and isinstance(message.get("payload"), list):
        kind += "[" + ",".join(child.get("request_type", "") for child in message["payload"]) + "]"
    return kind
_sequence = 0
_fault = None


def set_fault(mode=None):
    """Fault only the v0.4 trace endpoint; telemetry and negotiation stay live."""
    if mode not in (None, "http503", "disconnect"):
        raise ValueError(f"Unknown trace intake fault: {mode}")
    global _fault
    with records_lock:
        _fault = mode


def snapshot_requests(after_sequence=0):
    with records_lock:
        return copy.deepcopy([record for record in trace_requests if record["sequence"] > after_sequence])


def _wire_value(value):
    # JSON log pipelines represent numbers as doubles; preserve native nanoseconds.
    if isinstance(value, int) and abs(value) > (1 << 53) - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    return value


def _normalized_spans(traces):
    spans = []
    for trace in traces:
        for span in trace:
            item = _wire_value(span)
            for key in ("trace_id", "span_id", "parent_id"):
                if key in item:
                    item[key] = str(item[key])
            spans.append(item)
    return spans


class Intake(BaseHTTPRequestHandler):
    def do_GET(self):
        self._response_started = False
        try:
            if FORWARD:
                self.forward(b"")
            else:
                self.respond({"endpoints": ["v0.4/traces", "telemetry/proxy/api/v2/apmtelemetry"]})
        except Exception as error:
            self._failed_response(error, None)

    def do_POST(self):
        self.do_PUT()

    def do_PUT(self):
        self._response_started = False
        self._telemetry_timing = None  # Keep-alive reuses the handler across requests.
        wire = None
        fault = None
        received_at = time.monotonic()
        try:
            raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/traces"):
                global _sequence
                with records_lock:
                    _sequence += 1
                    wire = {
                        "sequence": _sequence,
                        "received_at": received_at,
                        "completed_at": None,
                        "status": None,
                        "outcome": "received",
                        "forwarded": False,
                        "bytes": len(raw_body),
                        "spans": [],
                    }
                    trace_requests.append(wire)
                    fault = _fault if self.path == "/v0.4/traces" else None
            body = gzip.decompress(raw_body) if self.headers.get("Content-Encoding") == "gzip" else raw_body
            if wire is not None:
                if self.path != "/v0.4/traces":
                    raise ValueError(f"Expected v0.4 traces, got {self.path}")
                traces = msgpack.unpackb(body, raw=False, strict_map_key=False)
                spans = _normalized_spans(traces)
                with records_lock:
                    records["traces"].extend(traces)
                    wire["spans"] = spans
            elif "telemetry" in self.path:
                telemetry = json.loads(body)
                self._telemetry_timing = {
                    "received_at": received_at,
                    "kind": _telemetry_kind(telemetry),
                    "runtime_id": telemetry.get("runtime_id"),
                    "upstream_seconds": None,
                    "status": None,
                }
                with records_lock:
                    records["telemetry"].append(telemetry)
                    telemetry_requests.append(self._telemetry_timing)
        except Exception as error:
            with records_lock:
                records["errors"].append(f"Intake decode failed: {error}")
                if wire is not None:
                    wire["outcome"] = "decode_error"
            try:
                self.respond({}, 400, wire, "decode_error")
            except Exception as response_error:
                self._failed_response(response_error, wire)
            return

        if fault == "disconnect":
            # No headers or body: a decoded receipt is explicitly not an ACK.
            with records_lock:
                wire["outcome"] = "disconnect"
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        try:
            if fault == "http503":
                self.respond({"error": "controlled trace intake outage"}, 503, wire, "http503")
            elif FORWARD:
                self.forward(raw_body, wire)
            else:
                self.respond({"rate_by_service": {}}, wire=wire)
        except Exception as error:
            self._failed_response(error, wire)

    def forward(self, body, wire=None):
        connection = HTTPConnection("127.0.0.1", 8126, timeout=5)
        try:
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "content-length", "transfer-encoding"}
            }
            forward_started = time.monotonic()
            connection.request(self.command, self.path, body=body, headers=headers)
            if wire is not None:
                with records_lock:
                    wire["forwarded"] = True
            response = connection.getresponse()
            # read() verifies Content-Length/chunked completion before any ACK is published.
            payload = response.read()
            timing = getattr(self, "_telemetry_timing", None)
            if timing is not None and "telemetry" in self.path:
                with records_lock:
                    timing.update(upstream_seconds=round(time.monotonic() - forward_started, 6), status=response.status)
            if not 200 <= response.status < 300:
                with records_lock:
                    records["errors"].append(f"Agent forwarding returned HTTP {response.status}: {self.path}")
            self._respond_bytes(payload, response.status, response.getheader("Content-Type", "application/json"), wire)
        finally:
            connection.close()

    def respond(self, value, status=200, wire=None, outcome=None):
        self._respond_bytes(json.dumps(value).encode(), status, "application/json", wire, outcome)

    def _respond_bytes(self, body, status, content_type, wire, outcome=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._response_started = True
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if wire is not None:
            with records_lock:
                wire.update(
                    status=status,
                    completed_at=time.monotonic(),
                    outcome=outcome or ("accepted" if 200 <= status < 300 else "rejected"),
                )

    def _failed_response(self, error, wire):
        # Exporter teardown can cancel negotiation before reading its response.
        # Retain that cancellation separately; it is not a decoded/forwarded
        # trace delivery failure. Trace response failures remain fatal below.
        if wire is None and self._response_started and isinstance(error, (BrokenPipeError, ConnectionResetError)):
            with records_lock:
                records["cancelled_responses"].append({"path": self.path, "error": str(error)})
            self.close_connection = True
            return
        with records_lock:
            records["errors"].append(f"Intake response failed: {type(error).__name__}: {error}")
            if wire is not None:
                wire["outcome"] = "response_error" if self._response_started else "upstream_error"
        if not self._response_started:
            try:
                self.respond({}, 502, wire, "upstream_error")
            except Exception as response_error:
                with records_lock:
                    records["errors"].append(f"Intake error response failed: {response_error}")
        self.close_connection = True

    def log_message(self, *args):
        pass
