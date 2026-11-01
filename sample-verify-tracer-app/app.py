"""One-shot MicroVM tracer identity-refresh verification guest.

Private tracer fields are intentional probes tied to the wheel under test.
The intake records native exporter payloads and forwards them to the local Agent
when VERIFY_FORWARD_TO_AGENT is enabled for the AWS workflow.
"""

import gc
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
import time

from feature_checks import evaluate_features
from features import run_features
from features import span_identity
from flask import Flask
from flask import jsonify
from flask import request
import msgpack

from ddtrace import __version__
from ddtrace import tracer
from ddtrace.internal.runtime import get_runtime_id
from ddtrace.internal.runtime.runtime_metrics import RuntimeWorker
from ddtrace.internal.serverless import MICROVM_RUN_HOOK_PATH
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_NAMESPACE
from ddtrace.propagation.http import HTTPPropagator


app = Flask(__name__)
state = {}
records = {"traces": [], "telemetry": [], "dogstatsd": [], "dogstatsd_alias": [], "errors": []}
records_lock = threading.Lock()
scenario_lock = threading.Lock()
FORWARD = os.environ.get("VERIFY_FORWARD_TO_AGENT") == "true"
RSS_METRIC = "runtime.python.mem.rss"
RSS_ALIAS = os.environ.get("VERIFY_RUNTIME_RSS_ALIAS", "")
HOOK_BASE = "/aws/lambda-microvms/runtime/v1"


class Intake(BaseHTTPRequestHandler):
    def do_GET(self):
        if FORWARD:
            self.forward(b"")
            return
        self.respond({"endpoints": ["v0.4/traces", "telemetry/proxy/api/v2/apmtelemetry"]})

    def do_POST(self):
        self.do_PUT()

    def do_PUT(self):
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = raw_body
        try:
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            with records_lock:
                if self.path.endswith("/traces"):
                    if not self.path.startswith("/v0.4/"):
                        raise ValueError(f"Expected v0.4 traces, got {self.path}")
                    records["traces"].extend(msgpack.unpackb(body, raw=False, strict_map_key=False))
                elif "telemetry" in self.path:
                    records["telemetry"].append(json.loads(body))
        except Exception as error:
            with records_lock:
                records["errors"].append(str(error))
            self.respond({}, 400)
            return
        if FORWARD:
            self.forward(raw_body)
        else:
            self.respond({"rate_by_service": {}})

    def forward(self, body):
        connection = HTTPConnection("127.0.0.1", 8126, timeout=5)
        try:
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in {"host", "connection", "content-length", "transfer-encoding"}
            }
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError as error:
            with records_lock:
                records["errors"].append(f"Agent forwarding failed: {error}")
            self.respond({}, 502)
        finally:
            connection.close()

    def respond(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def receive_metrics(sock):
    forward_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        raw_packet = sock.recv(65535)
        packet = raw_packet.decode()
        lines = packet.splitlines()
        alias_lines = [RSS_ALIAS + line[len(RSS_METRIC) :] for line in lines if line.startswith(RSS_METRIC + ":")]
        try:
            if FORWARD:
                forward_socket.sendto(raw_packet, ("127.0.0.1", 8125))
                # Optional diagnostic copy: preserve type, value, and every tag.
                # Always send the original packet, even while its backend assertion is skipped.
                if RSS_ALIAS and alias_lines:
                    forward_socket.sendto("\n".join(alias_lines).encode(), ("127.0.0.1", 8125))
        except OSError as error:
            with records_lock:
                records["errors"].append(f"DogStatsD forwarding failed: {error}")
        with records_lock:
            records["dogstatsd"].extend(lines)
            if RSS_ALIAS:
                records["dogstatsd_alias"].extend(alias_lines)


def rss_packets_for_runtime(metrics, runtime_id):
    # Match the complete metric name and tag; another runtime metric is not RSS evidence.
    packets = []
    for line in metrics:
        if not line.startswith(RSS_METRIC + ":"):
            continue
        tags = [tag for part in line.split("|") if part.startswith("#") for tag in part[1:].split(",")]
        if f"runtime-id:{runtime_id}" in tags:
            packets.append(line)
    return packets


def collector_baselines():
    collectors = RuntimeWorker._instance._runtime_metrics._collectors
    return {
        "cpu": next(dict(c.stored_cpu_times) for c in collectors if hasattr(c, "stored_cpu_times")),
        "wall": next(c._last_wall_time for c in collectors if hasattr(c, "_last_wall_time")),
        "gc": next(list(c._prev_collections) for c in collectors if hasattr(c, "_prev_collections")),
    }


@app.get("/health")
def health():
    return jsonify(status="ready", ddtrace=__version__, runtime_id=get_runtime_id())


@app.get("/feature/echo")
def feature_echo():
    root = tracer.current_root_span()
    w3c = HTTPPropagator.extract({"traceparent": request.headers.get("traceparent", "")})
    return jsonify(
        server=span_identity(root) if root else {},
        runtime_id=get_runtime_id(),
        traceparent=request.headers.get("traceparent"),
        w3c_trace_id=str(w3c.trace_id),
        w3c_parent_id=str(w3c.span_id),
    )


@app.get("/feature/error")
def feature_error():
    raise ValueError("microvm verification expected HTTP error")


@app.post(HOOK_BASE + "/ready")
def ready():
    # Seed the state in the image, so each real snapshot clone inherits it.
    if not state:
        return prepare()
    return jsonify(prepared=True)


@app.post(HOOK_BASE + "/validate")
@app.post(HOOK_BASE + "/terminate")
def acknowledge_hook():
    return jsonify(status="ok")


@app.post("/prepare")
def prepare():
    if not scenario_lock.acquire(blocking=False):
        return jsonify(error="One scenario per process; restart the container to repeat"), 409
    # Pause only the periodic thread. Keep collectors and refresh subscriptions live
    # so a timer cannot consume the interval seeded below before /run resets it.
    runtime = RuntimeWorker._instance
    runtime._worker.stop()
    runtime.join(2)
    old = tracer._span_aggregator.writer
    state.update(
        old_id=get_runtime_id(),
        old_writer=old,
        old_worker=telemetry_writer._worker,
        entered=threading.Event(),
        release=threading.Event(),
        errors=[],
    )
    stale = tracer.start_span("verify.stale", child_of=None)
    state["stale"] = stale
    state["context"] = stale.context
    with tracer.start_span("verify.buffered", child_of=None):
        pass
    state["before"] = collector_baselines()
    # Make the pre-refresh interval nonempty, including a visible GC delta.
    until = time.process_time() + 0.1
    while time.process_time() < until:
        pass
    for _ in range(5):
        gc.collect(2)
    state["gc_before_run"] = [row["collections"] for row in gc.get_stats()]
    original_flush = old.flush_queue

    def held_flush(*args, **kwargs):
        if threading.current_thread().name == "verify-configure":
            state["entered"].set()
            if not state["release"].wait(None if FORWARD else 30):
                raise TimeoutError("Writer drain was not released within 30s")
        return original_flush(*args, **kwargs)

    old.flush_queue = held_flush

    def configure():
        try:
            tracer.configure(trace_processors=[])
        except Exception as error:
            state["errors"].append(repr(error))

    state["thread"] = threading.Thread(target=configure, name="verify-configure", daemon=True)
    state["thread"].start()
    if not state["entered"].wait(5):
        return jsonify(error="configure() did not enter the writer drain"), 500
    state["run_start"] = time.monotonic()
    return jsonify(old_runtime_id=state["old_id"], drain_held=True)


@app.post(MICROVM_RUN_HOOK_PATH)
def run_hook():
    # The Flask request-start listener must refresh identity before this handler.
    return jsonify(runtime_id=get_runtime_id())


flask_wsgi_app = app.wsgi_app


def timed_wsgi_app(environ, start_response):
    is_run = environ.get("REQUEST_METHOD") == "POST" and environ.get("PATH_INFO") == MICROVM_RUN_HOOK_PATH
    if is_run:
        state["run_start"] = time.monotonic()
    response = flask_wsgi_app(environ, start_response)
    if is_run:
        state["hook_elapsed"] = time.monotonic() - state["run_start"]
    return response


app.wsgi_app = timed_wsgi_app


@app.post("/release")
def release_drain():
    if state:
        state["release"].set()
    return jsonify(released=True)


@app.post("/verify")
def verify():
    if not state:
        return jsonify(error="Call /prepare and /run first"), 409
    if "result" in state:
        return jsonify(state["result"])
    new_id = get_runtime_id()
    baselines = collector_baselines()
    old = state["old_writer"]
    current = tracer._span_aggregator.writer
    checks = {
        "identity_rotated": new_id != state["old_id"],
        "20955_old_writer_discarded": old._discarded_by_refresh and not old._accepting_writes,
        "21429_refresh_while_drain_held": state["thread"].is_alive() and not state["release"].is_set(),
        "21429_replacement_published": current is not old,
        "20956_worker_replaced": telemetry_writer._worker is not state["old_worker"],
        "20956_worker_identity": telemetry_writer._worker_runtime_id == new_id,
        "20956_old_exporter_unsubscribed": not old._telemetry_worker_subscribed,
        "20956_new_exporter_subscribed": current._telemetry_worker_subscribed,
        "20957_collector_identity": RuntimeWorker._instance._collectors_runtime_id == new_id,
        "20957_wall_baseline_reset": baselines["wall"] >= state["run_start"],
        "20957_cpu_baseline_reset": sum(baselines["cpu"].values()) > sum(state["before"]["cpu"].values()),
        "20957_gc_baseline_reset": all(a >= b for a, b in zip(baselines["gc"], state["gc_before_run"])),
    }
    span_start = time.monotonic()
    with tracer.start_span("verify.during_drain", child_of=None):
        pass
    checks["21429_span_work_during_drain"] = time.monotonic() - span_start < 1
    state["release"].set()
    state["thread"].join(5)
    checks["21429_drain_completed"] = not state["thread"].is_alive() and not state["errors"]
    state["stale"].finish()
    with tracer.start_span("verify.stale_child", child_of=state["context"]):
        pass
    with tracer.start_span("verify.fresh", child_of=None) as fresh:
        state["fresh_trace_id"] = fresh.trace_id & ((1 << 64) - 1)
    feature_evidence = run_features()
    tracer.flush()
    RuntimeWorker._instance.flush()
    telemetry_writer.add_count_metric(TELEMETRY_NAMESPACE.TRACERS, "verify.after_refresh", 1)
    telemetry_writer.periodic(force_flush=True)
    # UDP intake and native exporter callbacks complete on separate threads.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with records_lock:
            spans = [span for trace in records["traces"] for span in trace]
            metrics = list(records["dogstatsd"])
            telemetry_received = any(
                payload.get("runtime_id") == new_id and "verify.after_refresh" in json.dumps(payload)
                for payload in records["telemetry"]
            )
        if (
            any(s["name"] == "verify.fresh" for s in spans)
            and rss_packets_for_runtime(metrics, new_id)
            and telemetry_received
            and all(evaluate_features(spans, feature_evidence, new_id).values())
        ):
            break
        time.sleep(0.05)
    names = [s["name"] for s in spans]
    checks.update(
        {
            "20955_stale_spans_absent": not {"verify.stale", "verify.stale_child", "verify.buffered"}.intersection(
                names
            ),
            "fresh_trace_received": any(
                s["name"] == "verify.fresh" and s["trace_id"] == state["fresh_trace_id"] for s in spans
            ),
            "fresh_trace_identity": any(
                s["name"] == "verify.fresh" and s.get("meta", {}).get("runtime-id") == new_id for s in spans
            ),
            "20957_new_metrics_received": bool(rss_packets_for_runtime(metrics, new_id)),
            "20956_new_telemetry_received": telemetry_received,
            "intake_decode_succeeded": not records["errors"],
        }
    )
    checks["run_under_one_second"] = state.get("hook_elapsed", float("inf")) < 1
    checks.update(evaluate_features(spans, feature_evidence, new_id))
    result = dict(
        verification_schema=2,
        passed=all(checks.values()),
        checks=checks,
        old_runtime_id=state["old_id"],
        runtime_id=new_id,
        baselines=baselines,
        span_names=names,
        errors=state["errors"] + records["errors"],
        ddtrace=__version__,
        wheel_sha256=Path("/app/wheel.sha256").read_text().split()[0],
        run_seconds=state.get("hook_elapsed"),
        verification_id=os.environ.get("DD_VERSION", "local"),
        runtime_rss_packets=rss_packets_for_runtime(metrics, new_id),
        runtime_rss_alias=RSS_ALIAS or None,
        features=feature_evidence,
        feature_spans=[
            span
            for span in spans
            if str(span["trace_id"]) in {root["trace_id"] for root in feature_evidence["roots"].values()}
        ],
    )
    state["result"] = result
    print(json.dumps({"msg": "tracer_verification", **result}), flush=True)
    return jsonify(result)


if __name__ == "__main__":
    intake = ThreadingHTTPServer(("127.0.0.1", 18126), Intake)
    threading.Thread(target=intake.serve_forever, daemon=True).start()
    metrics_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    metrics_socket.bind(("127.0.0.1", 18125))
    threading.Thread(target=receive_metrics, args=(metrics_socket,), daemon=True).start()
    # Bind on the guest interface so platform hooks and authenticated ingress can reach Flask.
    app.run(host="0.0.0.0", port=8080, threaded=True)  # nosec B104
