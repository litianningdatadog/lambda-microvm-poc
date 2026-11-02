"""Opt-in delivery markers and metadata-only Agent intake fault relays."""
import copy
import http.client
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIOS = {"initial", "resumed-1", "resumed-2", "resumed-3",
             "recovery-http503", "recovery-disconnect", "termination"}
MODES = {"normal", "http503", "disconnect"}
SITES = {"datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com",
         "ap1.datadoghq.com", "ap2.datadoghq.com", "ddog-gov.com"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
MAX_BODY = 16 * 1024 * 1024


class DeliveryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def marker_id(value):
    if not isinstance(value, str):
        raise DeliveryError("marker must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise DeliveryError("marker must be a UUID") from None
    if str(parsed) != value:
        raise DeliveryError("marker must be a canonical UUID")
    return value


class Delivery:
    def __init__(self, statsd, emit=None, max_requests=4096):
        self.statsd = statsd
        self.emit = emit or (lambda row: print(json.dumps(row), flush=True))
        self.lock = threading.RLock()
        self.run_id = None
        self.counters = dict(runCount=0, resumeCount=0, suspendCount=0, terminateCount=0)
        self.markers = {}
        self.armed = None
        self.terminated = False
        self.mode = "normal"
        self.requests = []
        self.max_requests = max_requests
        self.overflow = False

    def state(self):
        with self.lock:
            if self.overflow:
                raise DeliveryError("relay evidence retention exceeded", 507)
            return copy.deepcopy(dict(schema=1, runId=self.run_id, counters=self.counters,
                                      mode=self.mode, overflow=self.overflow, requests=self.requests))

    def on_hook(self, hook):
        key = {"run": "runCount", "resume": "resumeCount", "suspend": "suspendCount",
               "terminate": "terminateCount"}.get(hook)
        with self.lock:
            if hook == "terminate" and self.terminated:
                return
            if key:
                self.counters[key] += 1
            if hook == "run" and self.run_id is None:
                self.run_id = str(uuid.uuid4())
            if hook == "terminate":
                self.terminated = True
                if self.armed:
                    self._emit("termination", self.armed["marker"])

    def _row(self, scenario, marker):
        if self.run_id is None:
            raise DeliveryError("run hook has not initialized this VM", 409)
        return dict(msg="mvmtest_delivery", schema=1, scenario=scenario, marker=marker,
                    runId=self.run_id, wall=time.time(), value=42, counters=self.counters.copy())

    def _emit(self, scenario, marker):
        row = self._row(scenario, marker)
        self.emit(row)
        self.statsd.gauge("mvmtest.delivery", 42,
                          tags=[f"marker:{marker}", f"scenario:{scenario}", f"run_id:{self.run_id}",
                                f"service:{os.getenv('DD_SERVICE', 'sample-verify-app')}"])
        self.markers[marker] = row
        return copy.deepcopy(row)

    def marker(self, scenario, marker):
        marker_id(marker)
        if scenario not in SCENARIOS or scenario == "termination":
            raise DeliveryError("scenario must be a non-termination delivery scenario")
        with self.lock:
            if self.armed and self.armed["marker"] == marker:
                raise DeliveryError("marker already armed for termination", 409)
            if marker in self.markers:
                if self.markers[marker]["scenario"] != scenario:
                    raise DeliveryError("marker scenario mismatch", 409)
                return copy.deepcopy(self.markers[marker])
            if self.terminated:
                raise DeliveryError("VM already terminated", 409)
            if len(self.markers) >= 256:
                raise DeliveryError("marker retention exceeded", 507)
            return self._emit(scenario, marker)

    def arm(self, marker):
        marker_id(marker)
        with self.lock:
            if self.armed:
                if self.armed["marker"] != marker:
                    raise DeliveryError("termination already armed with another marker", 409)
                return copy.deepcopy(self.armed)
            if self.terminated or marker in self.markers:
                raise DeliveryError("marker already used or VM terminated", 409)
            self.armed = dict(self._row("termination", marker), armed=True)
            return copy.deepcopy(self.armed)

    def fault(self, mode):
        if mode not in MODES:
            raise DeliveryError("invalid fault mode")
        with self.lock:
            self.mode = mode
            return self.state()

    def begin_request(self, pipeline):
        with self.lock:
            if len(self.requests) >= self.max_requests:
                self.overflow = True
                return None
            row = dict(pipeline=pipeline, mode=self.mode, outcome=None, status=None,
                       receivedAt=time.time(), completedAt=None)
            self.requests.append(row)
            return row

    def finish_request(self, row, outcome, status=None):
        with self.lock:
            row.update(outcome=outcome, status=status, completedAt=time.time())


def end_to_end(headers):
    excluded = HOP_HEADERS | {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    return [(key, value) for key, value in headers.items() if key.lower() not in excluded | {"host", "content-length"}]


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # No tracebacks: exceptions can contain upstream URLs or headers.
        pass


def relay_handler(delivery, pipeline, host):
    class RelayHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", headers=()):
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Forwarder credential validation is control traffic, not fault evidence.
            if pipeline != "metrics" or self.path.partition("?")[0] != "/api/v1/validate":
                self.reply(404)
                return
            self.connection.settimeout(15)
            conn = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 <= length <= MAX_BODY:
                    raise ValueError("invalid validation body")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete validation body")
                conn = http.client.HTTPSConnection(host, timeout=15)
                conn.putrequest("GET", self.path, skip_accept_encoding=True)
                for key, value in end_to_end(self.headers):
                    conn.putheader(key, value)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                response = conn.getresponse()
                response_body = response.read(MAX_BODY + 1)
                if len(response_body) > MAX_BODY:
                    raise ValueError("upstream response too large")
                self.reply(response.status, response_body, end_to_end(response.headers))
            except (OSError, ValueError, http.client.HTTPException):
                self.reply(502)
            finally:
                if conn:
                    conn.close()

        def do_POST(self):
            row = delivery.begin_request(pipeline)
            if row is None:
                self.reply(507)
                return
            self.connection.settimeout(15)
            conn = None
            try:
                if not self.path.startswith("/") or self.path.startswith("//"):
                    raise ValueError("invalid request target")
                if self.headers.get("Transfer-Encoding"):
                    if self.headers["Transfer-Encoding"].lower() != "chunked":
                        raise ValueError("unsupported transfer encoding")
                    chunks, size = [], 0
                    while True:
                        count = int(self.rfile.readline(128).split(b";", 1)[0], 16)
                        if count == 0:
                            while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        size += count
                        if size > MAX_BODY or count < 0:
                            raise ValueError("request too large")
                        chunk = self.rfile.read(count)
                        if len(chunk) != count or self.rfile.read(2) != b"\r\n":
                            raise ValueError("invalid chunk")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_BODY:
                        raise ValueError("invalid length")
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError("incomplete body")
                if row["mode"] == "http503":
                    delivery.finish_request(row, "injected-http503", 503)
                    self.reply(503)
                    return
                if row["mode"] == "disconnect":
                    delivery.finish_request(row, "injected-disconnect")
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                conn = http.client.HTTPSConnection(host, timeout=15)
                conn.putrequest("POST", self.path, skip_accept_encoding=True)
                for key, value in end_to_end(self.headers):
                    conn.putheader(key, value)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                response = conn.getresponse()
                response_body = response.read(MAX_BODY + 1)
                if len(response_body) > MAX_BODY:
                    raise ValueError("upstream response too large")
                delivery.finish_request(row, "accepted" if 200 <= response.status < 300 else "upstream-error", response.status)
                self.reply(response.status, response_body, end_to_end(response.headers))
            except (OSError, ValueError, http.client.HTTPException):
                if row["completedAt"] is None:
                    delivery.finish_request(row, "upstream-error")
                    self.reply(502)
            finally:
                if conn:
                    conn.close()
    return RelayHandler


def start_relays(delivery):
    site = os.getenv("DD_SITE", "datadoghq.com")
    if site not in SITES:
        raise ValueError("unsupported DD_SITE for delivery relays")
    servers = []
    for pipeline, port, host in (("metrics", 18127, "app." + site),
                                 ("logs", 18128, "http-intake.logs." + site)):
        server = RelayServer(("127.0.0.1", port), relay_handler(delivery, pipeline, host))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers
