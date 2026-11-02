"""Telemetry generator for verifying serverless-init across MicroVM suspend/resume/terminate.

Emits, continuously and independent of inbound traffic:
  - a JSON "tick" log line every 100ms with an increasing seq (detects lost log batches)
  - a DogStatsD counter mvmtest.count incremented with every tick (detects a
    counter bucket overwritten by a late second point after a resume)
  - a DogStatsD gauge mvmtest.seq and one traced span mvmtest.tick every 1s
    (detects lost metrics / trace stats on terminate)
  - a DogStatsD distribution mvmtest.distribution every 1s, as a control for
    the sketch payload path also used by enhanced lifecycle metrics
  - a JSON "clock" log line every 5s with wall, monotonic and /proc/uptime
    (shows how guest clocks behave across a suspend)
  - if SPANS_PER_SEC > 0, that many mvmtest.burst spans per second, plus a JSON
    "spans" log line per wall-clock second with how many were created in it
    (detects a trace stats bucket overwritten by a second, partial point)

Port 8080 serves GET / -> 200 so the endpoint can be curled. Lifecycle hooks
on port 9000 are answered by serverless-init itself (no forwarding), unless
HOOK_PORT is set (image `fwd`): serverless-init then forwards each enabled hook
to POST /<hook> on that port. The handler emits, before answering 200, a JSON
"apphook" log line, an mvmtest.hook span (resource = hook name, flushed to the
agent before the response) and a DogStatsD counter mvmtest.hook (tag hook:<name>).
On /terminate, serverless-init flushes after the app answered, so all three
must arrive (R3).
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from datadog import DogStatsd
from ddtrace import tracer

from delivery import Delivery, DeliveryError, start_relays

PAD = "x" * int(os.getenv("LOG_PAD_BYTES", "1024"))
SPANS_PER_SEC = int(os.getenv("SPANS_PER_SEC", "0"))
HOOK_PORT = int(os.getenv("HOOK_PORT", "0"))
# Unbuffered and unaggregated so each increment reaches the agent immediately,
# in the same 10s bucket as the tick log line printed with it.
statsd = DogStatsd(host="127.0.0.1", port=8125, disable_buffering=True, disable_aggregation=True)
delivery = Delivery(statsd) if os.getenv("DELIVERY_ENABLED") == "true" else None


def uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def loop():
    seq = 0
    while True:
        seq += 1
        print(json.dumps({"msg": "tick", "seq": seq, "pad": PAD}), flush=True)
        statsd.increment("mvmtest.count")  # 10/s: per-bucket count must equal tick logs in that bucket
        if seq % 10 == 0:
            statsd.gauge("mvmtest.seq", seq // 10)
            statsd.distribution("mvmtest.distribution", 1)
            with tracer.trace("mvmtest.tick", resource="tick"):
                pass
        if seq % 50 == 0:
            print(json.dumps({"msg": "clock", "seq": seq, "wall": time.time(),
                              "mono": time.monotonic(), "uptime": uptime()}), flush=True)
        time.sleep(0.1)


def burst_loop():
    # Counts per wall-clock second, so the sum over a 10s window is exactly what
    # trace.mvmtest.burst.hits should report for that stats bucket.
    sec, n = int(time.time()), 0
    while True:
        with tracer.trace("mvmtest.burst", resource="burst"):
            pass
        now = int(time.time())
        if now != sec:
            print(json.dumps({"msg": "spans", "sec": sec, "n": n}), flush=True)
            sec, n = now, 0
        n += 1
        time.sleep(1 / SPANS_PER_SEC)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if delivery is not None:
            if self.path == "/delivery/state":
                try:
                    self.json_response(200, delivery.state())
                except DeliveryError as exc:
                    self.json_response(exc.status, {"error": str(exc)})
                return
            if self.path != "/":
                self.json_response(404, {"error": "not found"})
                return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def json_response(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        routes = {"/delivery/marker": {"scenario", "marker"},
                  "/delivery/termination": {"marker"}, "/delivery/fault": {"mode"}}
        if delivery is None or self.path not in routes:
            self.json_response(404, {"error": "not found"})
            return
        try:
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise DeliveryError("expected length-delimited application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise DeliveryError("invalid JSON body length")
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != routes[self.path]:
                raise DeliveryError("invalid request fields")
            if not all(isinstance(value, str) for value in body.values()):
                raise DeliveryError("request values must be strings")
            if self.path == "/delivery/marker":
                result = delivery.marker(body["scenario"], body["marker"])
            elif self.path == "/delivery/termination":
                result = delivery.arm(body["marker"])
            else:
                result = delivery.fault(body["mode"])
            self.json_response(200, result)
        except DeliveryError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError, OSError):
            self.json_response(400, {"error": "invalid JSON request"})

    def log_message(self, *args):
        pass


class HookHandler(BaseHTTPRequestHandler):
    # Answers forwarded lifecycle hooks. serverless-init's forward timeout is 1s,
    # so everything here must be quick.
    def do_POST(self):
        hook = self.path.rstrip("/").rsplit("/", 1)[-1]  # /aws/lambda-microvms/runtime/v1/<hook>
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if delivery is not None:
            if hook not in {"validate", "ready", "run", "resume", "suspend", "terminate"} or self.path not in {
                f"/{hook}", f"/aws/lambda-microvms/runtime/v1/{hook}"
            }:
                self.send_error(404)
                return
            delivery.on_hook(hook)
        print(json.dumps({"msg": "apphook", "hook": hook, "wall": time.time()}), flush=True)
        statsd.increment("mvmtest.hook", tags=[f"hook:{hook}"])
        with tracer.trace("mvmtest.hook", resource=hook):
            pass
        tracer.flush()  # hand the span to the agent before answering, not on the next 1s writer tick
        body = f"app:{hook}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if delivery is not None:
        start_relays(delivery)
    threading.Thread(target=loop, daemon=True).start()
    if SPANS_PER_SEC > 0:
        threading.Thread(target=burst_loop, daemon=True).start()
    if HOOK_PORT > 0:
        hooks = ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), HookHandler)
        threading.Thread(target=hooks.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
