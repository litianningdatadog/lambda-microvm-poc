"""MicroVM tracer identity, feature, and native-delivery verification guest.

Private tracer fields are intentional probes tied to the wheel under test.
The intake records native exporter payloads and forwards them to the local Agent
when VERIFY_FORWARD_TO_AGENT is enabled for the AWS workflow.
"""

import gc
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
import time

from delivery import Delivery
from feature_checks import evaluate_features
from features import run_features
from features import span_identity
from intake import FORWARD
from intake import Intake
from intake import records
from intake import records_lock
from intake import telemetry_requests
from flask import Flask
from flask import jsonify
from flask import request
from lifecycle_checks import SCHEMA
from lifecycle_checks import exporter_metrics
from lifecycle_checks import subscriber_count
from retirement_checks import evaluate_retirements
from retirement_control import run_retirements

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
scenario_lock = threading.Lock()
RSS_METRIC = "runtime.python.mem.rss"
RSS_ALIAS = os.environ.get("VERIFY_RUNTIME_RSS_ALIAS", "")
HOOK_BASE = "/aws/lambda-microvms/runtime/v1"
# "mvp" tests a wheel without #20956/#20957, so their probes would read fields it lacks.
SCOPE = os.environ.get("VERIFY_SCOPE", "full")
MVP = SCOPE == "mvp"
delivery = Delivery(state)


def lifecycle_state():
    return dict(state["lifecycle"])


def flush_tracer():
    state["manual_flush_count"] += 1
    tracer.flush()


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


def record_run_step(step, started):
    # Diagnostic only, recorded during /run; offsets are relative to the /run request start.
    if not state.get("recording_run"):
        return
    now = time.monotonic()
    state.setdefault("run_timings", []).append({
        "step": step,
        "start": round(started - state.get("run_start", started), 6),
        "seconds": round(now - started, 6),
        "thread": threading.current_thread().name,
    })


def time_calls(owner, names, prefix):
    """Wrap methods with timers; skips names this wheel lacks. Not for subscribed callbacks."""
    for name in names:
        original = getattr(owner, name, None)
        if original is None:
            continue

        def timed(*args, _original=original, _label=prefix + name, **kwargs):
            if not state.get("recording_run"):
                return _original(*args, **kwargs)
            started = time.monotonic()
            try:
                return _original(*args, **kwargs)
            finally:
                record_run_step(_label, started)

        setattr(owner, name, timed)


def callback_name(callback):
    owner = getattr(callback, "__self__", None)
    name = getattr(callback, "__qualname__", None) or repr(callback)
    return f"{type(owner).__name__}.{callback.__name__}" if owner is not None else name


BENCH_BUFFER_BYTES = 32 << 20
# A/B switch: the pre-snapshot benchmark itself may change post-restore speed.
PLATFORM_BENCH = os.environ.get("VERIFY_PLATFORM_BENCH", "true") != "false"


def platform_bench(buffer):
    """Fixed CPU and page-touch work, to compare post-restore speed with the pre-snapshot baseline."""
    started = time.perf_counter()
    total = 0
    for value in range(1_000_000):
        total += value
    cpu = time.perf_counter() - started
    started = time.perf_counter()
    for offset in range(0, len(buffer), 4096):
        buffer[offset] = (buffer[offset] + 1) & 255
    pages = time.perf_counter() - started
    return {"cpu_loop_seconds": round(cpu, 6), "page_touch_seconds": round(pages, 6), "pages": len(buffer) // 4096}


def build_inputs():
    def read(name):
        try:
            return Path("/app", name).read_text().splitlines()
        except OSError:
            return None

    return {"os": read("build-os.txt"), "pip": read("build-pip.txt"), "rpm": read("build-rpm.txt")}


def run_telemetry_requests():
    # Telemetry the intake saw from 1s before /run to 2s after it, relative to the /run start.
    start = state.get("run_start")
    if start is None:
        return []
    end = start + (state.get("hook_elapsed") or 0) + 2
    with records_lock:
        rows = [dict(row) for row in telemetry_requests if start - 1 <= row["received_at"] <= end]
    for row in rows:
        row["start"] = round(row.pop("received_at") - start, 6)
    return rows


def time_identity_refresh():
    """Time each refresh step without wrapping callbacks, so unsubscribe-by-identity keeps working."""
    import ddtrace.internal._runtime_id as runtime_id

    notify_refresh = getattr(runtime_id, "_notify_runtime_identity_refresh_callbacks", None)
    notify_change = getattr(runtime_id, "_notify_runtime_id_callbacks", None)
    refresh = getattr(runtime_id, "refresh_identity", None)
    if not (notify_refresh and notify_change and refresh):
        return

    def timed_callback(prefix, callback):
        started = time.monotonic()
        try:
            callback(runtime_id._RUNTIME_ID)
        finally:
            record_run_step(prefix + callback_name(callback), started)

    # Same isolation and logging as the tracer's own loops.
    def timed_notify_change(callbacks):
        for callback in list(callbacks):
            try:
                timed_callback("id_change:", callback)
            except Exception:
                runtime_id.log.exception("Exception ignored in runtime ID callback %r", callback)

    def timed_notify_refresh(*, raise_on_error=False):
        for callback in list(runtime_id._ON_RUNTIME_IDENTITY_REFRESH):
            if raise_on_error:
                timed_callback("refresh:", callback)
                continue
            try:
                timed_callback("refresh:", callback)
            except Exception:
                runtime_id.log.exception("Exception ignored in runtime ID callback %r", callback)

    def timed_refresh(*args, **kwargs):
        started = time.monotonic()
        try:
            return refresh(*args, **kwargs)
        finally:
            record_run_step("refresh_identity_total", started)

    runtime_id._notify_runtime_identity_refresh_callbacks = timed_notify_refresh
    runtime_id._notify_runtime_id_callbacks = timed_notify_change
    runtime_id.refresh_identity = timed_refresh

    # Inside the worker swap: which native or Python call owns the time after a snapshot restore.
    from ddtrace._trace.processor import SpanAggregator
    from ddtrace.internal.writer.writer import NativeWriter

    time_calls(telemetry_writer, ("_discard_worker", "enable", "app_started", "_get_shared_worker"), "telemetry.")
    tracker = getattr(telemetry_writer, "_dependency_tracker", None)
    if tracker is not None:
        time_calls(tracker, ("refresh",), "telemetry.dependencies.")
    time_calls(NativeWriter, (
        "_create_exporter", "_drop_exporter", "_shutdown_exporter", "drop_buffered_traces",
        "recreate", "_recreate", "_set_telemetry_handle", "_unsubscribe_telemetry_worker",
    ), "writer.")
    time_calls(SpanAggregator, ("reset",), "aggregator.")


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
def acknowledge_hook():
    return jsonify(status="ok")


@app.post(HOOK_BASE + "/suspend")
def suspend_hook():
    state["lifecycle"]["suspend"] += 1
    return jsonify(lifecycle=lifecycle_state(), runtime_id=get_runtime_id())


@app.post(HOOK_BASE + "/resume")
def resume_hook():
    # Resume preserves the identity assigned by the initial native /run listener.
    state["lifecycle"]["resume"] += 1
    return jsonify(lifecycle=lifecycle_state(), runtime_id=get_runtime_id())


@app.post(HOOK_BASE + "/terminate")
def terminate_hook():
    if not state:
        return jsonify(status="ok")
    state["lifecycle"]["terminate"] += 1
    return jsonify(delivery.terminate())


@app.post("/delivery/automatic")
def automatic_delivery():
    return jsonify(delivery.automatic())


@app.post("/delivery/recovery")
def recover_delivery():
    return jsonify(delivery.recovery())


@app.post("/delivery/termination")
def arm_termination():
    return jsonify(delivery.arm())


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
    # Keep the seeded trace buffered without changing the replacement writer's
    # native one-second timer. A normal stop would flush and retire the exporter.
    if old._worker is None:
        old.start()
    old._worker._before_fork()
    old._worker.join(2)
    state.update(
        manual_flush_count=0,
        lifecycle={"run": 0, "suspend": 0, "resume": 0, "terminate": 0},
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
    state["pre_run_writer"] = tracer._span_aggregator.writer
    time_identity_refresh()
    # Pages touched here are part of the snapshot; /run touches them again after restore.
    if PLATFORM_BENCH:
        state["bench_buffer"] = bytearray(BENCH_BUFFER_BYTES)
        state["bench_pre_snapshot"] = platform_bench(state["bench_buffer"])
    state["run_start"] = time.monotonic()
    return jsonify(old_runtime_id=state["old_id"], drain_held=True)


@app.post(MICROVM_RUN_HOOK_PATH)
def run_hook():
    # The Flask request-start listener must refresh identity before this handler.
    state["lifecycle"]["run"] += 1
    record_run_step("run_handler_entered", time.monotonic())
    return jsonify(runtime_id=get_runtime_id())


flask_wsgi_app = app.wsgi_app


def timed_wsgi_app(environ, start_response):
    is_run = environ.get("REQUEST_METHOD") == "POST" and environ.get("PATH_INFO") == MICROVM_RUN_HOOK_PATH
    if is_run:
        state["run_start"] = time.monotonic()
        state["recording_run"] = True
    try:
        response = flask_wsgi_app(environ, start_response)
    finally:
        if is_run:
            state["hook_elapsed"] = time.monotonic() - state["run_start"]
            state["recording_run"] = False
            # After hook_elapsed is recorded, so the benchmark cannot affect run_seconds.
            if "bench_buffer" in state and "bench_post_restore" not in state:
                state["bench_post_restore"] = platform_bench(state["bench_buffer"])
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
    }
    if not MVP:
        checks.update({
            "20956_worker_replaced": telemetry_writer._worker is not state["old_worker"],
            "20956_worker_identity": telemetry_writer._worker_runtime_id == new_id,
            "20956_old_exporter_unsubscribed": not old._telemetry_worker_subscribed,
            "20956_new_exporter_subscribed": current._telemetry_worker_subscribed,
            "20956_retired_callback_removed": subscriber_count(telemetry_writer, old) == 0,
            "20956_pre_run_callback_removed": subscriber_count(telemetry_writer, state["pre_run_writer"]) == 0,
            "20956_current_callback_once": subscriber_count(telemetry_writer, current) == 1,
            "20957_collector_identity": RuntimeWorker._instance._collectors_runtime_id == new_id,
            "20957_wall_baseline_reset": baselines["wall"] >= state["run_start"],
            "20957_cpu_baseline_reset": sum(baselines["cpu"].values()) > sum(state["before"]["cpu"].values()),
            "20957_gc_baseline_reset": all(a >= b for a, b in zip(baselines["gc"], state["gc_before_run"])),
        })
    span_start = time.monotonic()
    with tracer.start_span("verify.during_drain", child_of=None):
        pass
    checks["21429_span_work_during_drain"] = time.monotonic() - span_start < 1
    state["release"].set()
    state["thread"].join(5)
    checks["21429_drain_completed"] = not state["thread"].is_alive() and not state["errors"]
    active_context = tracer.context_provider.active()
    try:
        retirements = run_retirements()
    finally:
        tracer.context_provider.activate(active_context)
    checks.update(evaluate_retirements(retirements))
    state["stale"].finish()
    with tracer.start_span("verify.stale_child", child_of=state["context"]):
        pass
    with tracer.start_span("verify.fresh", child_of=None) as fresh:
        state["fresh_trace_id"] = fresh.trace_id & ((1 << 64) - 1)
    feature_evidence = run_features()
    flush_tracer()
    RuntimeWorker._instance.flush()
    telemetry_writer.add_count_metric(TELEMETRY_NAMESPACE.TRACERS, "verify.after_refresh", 1)
    telemetry_writer.periodic(force_flush=True)
    # UDP intake and native exporter callbacks complete on separate threads.
    deadline = time.monotonic() + 5
    next_flush = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        if time.monotonic() >= next_flush:
            telemetry_writer.periodic(force_flush=True)
            next_flush = time.monotonic() + 0.25
        with records_lock:
            spans = [span for trace in records["traces"] for span in trace]
            metrics = list(records["dogstatsd"])
            native_metrics = exporter_metrics(records["telemetry"], new_id)
            telemetry_received = any(
                payload.get("runtime_id") == new_id and "verify.after_refresh" in json.dumps(payload)
                for payload in records["telemetry"]
            )
        if (
            any(s["name"] == "verify.fresh" for s in spans)
            and all(evaluate_features(spans, feature_evidence, new_id).values())
            and (MVP or (
                rss_packets_for_runtime(metrics, new_id)
                and telemetry_received
                and all(value > 0 for value in native_metrics.values())
            ))
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
            "intake_decode_succeeded": not records["errors"],
        }
    )
    if not MVP:
        checks.update({
            "20957_new_metrics_received": bool(rss_packets_for_runtime(metrics, new_id)),
            "20956_new_telemetry_received": telemetry_received,
            "20956_exporter_requests_received": native_metrics["trace_api.requests"] > 0,
            "20956_exporter_responses_received": native_metrics["trace_api.responses"] > 0,
        })
    checks["run_under_one_second"] = state.get("hook_elapsed", float("inf")) < 1
    checks.update(evaluate_features(spans, feature_evidence, new_id))
    result = dict(
        verification_schema=SCHEMA,
        verification_scope=SCOPE,
        exporter_telemetry=native_metrics,
        retirements=retirements,
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
        run_timings=state.get("run_timings", []),
        run_telemetry=run_telemetry_requests(),
        platform_bench={
            "enabled": PLATFORM_BENCH,
            "pre_snapshot": state.get("bench_pre_snapshot"),
            "post_restore": state.get("bench_post_restore"),
        },
        build_inputs=build_inputs(),
        verification_id=os.environ.get("DD_VERSION", "local"),
        runtime_rss_packets=rss_packets_for_runtime(metrics, new_id),
        runtime_rss_alias=RSS_ALIAS or None,
        features=feature_evidence,
        lifecycle=lifecycle_state(),
        manual_flush_count=state["manual_flush_count"],
        feature_spans=[
            span
            for span in spans
            if str(span["trace_id"]) in {root["trace_id"] for root in feature_evidence["roots"].values()}
        ],
    )
    state["result"] = result
    print(json.dumps({"msg": "tracer_verification", **result}), flush=True)
    return jsonify(result)


@app.post("/verify/features")
def verify_resumed_features():
    if "result" not in state:
        return jsonify(error="Complete initial /verify first"), 409
    if not delivery.lock.acquire(blocking=False):
        return jsonify(error="A delivery scenario is already running"), 409
    try:
        return resumed_feature_result()
    finally:
        delivery.lock.release()


def resumed_feature_result():
    runtime_id = get_runtime_id()
    features = run_features()
    flush_tracer()
    with records_lock:
        trace_ids = {root["trace_id"] for root in features["roots"].values()}
        spans = [
            span
            for trace in records["traces"]
            for span in trace
            if str(span["trace_id"]) in trace_ids
        ]
    checks = evaluate_features(spans, features, runtime_id)
    lifecycle = lifecycle_state()
    checks.update(
        runtime_identity_stable=runtime_id == state["result"]["runtime_id"],
        lifecycle_counters=(
            lifecycle["run"] == 1
            and lifecycle["suspend"] == lifecycle["resume"]
            and 1 <= lifecycle["resume"] <= 3
            and lifecycle["terminate"] == 0
        ),
    )
    result = dict(
        verification_schema=SCHEMA,
        scenario="resumed-features",
        cycle=lifecycle["resume"],
        runtime_id=runtime_id,
        verification_id=os.environ.get("DD_VERSION", "local"),
        passed=all(checks.values()),
        checks=checks,
        features=features,
        feature_spans=spans,
        lifecycle=lifecycle,
        manual_flush_count=state["manual_flush_count"],
    )
    print(json.dumps({"msg": "tracer_resumed_features", **result}), flush=True)
    return jsonify(result)


if __name__ == "__main__":
    intake = ThreadingHTTPServer(("127.0.0.1", 18126), Intake)
    threading.Thread(target=intake.serve_forever, daemon=True).start()
    metrics_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    metrics_socket.bind(("127.0.0.1", 18125))
    threading.Thread(target=receive_metrics, args=(metrics_socket,), daemon=True).start()
    # Bind on the guest interface so platform hooks and authenticated ingress can reach Flask.
    app.run(host="0.0.0.0", port=8080, threaded=True)  # nosec B104
