"""Local controls with real native exporters; each mode needs a fresh process."""

import gc
import json
import os
import sys
import threading
import time
import weakref

from lifecycle_checks import exporter_metrics
from lifecycle_checks import subscriber_count


def run(mode):
    microvm = bool(os.environ.get("AWS_LAMBDA_MICROVM_IMAGE_ARN", "").strip())
    if microvm != (mode != "regular"):
        raise ValueError("regular requires MicroVM detection disabled; other modes require it enabled")
    # Set before importing ddtrace: all traffic stays on the in-process intake.
    os.environ["VERIFY_FORWARD_TO_AGENT"] = "false"
    from http.server import ThreadingHTTPServer
    from unittest.mock import Mock

    from intake import Intake
    from intake import records
    from intake import records_lock
    import msgpack

    from ddtrace.internal.runtime import get_runtime_id
    from ddtrace.internal.runtime import refresh_identity
    from ddtrace.internal.telemetry import telemetry_writer
    from ddtrace.internal.writer.writer import NativeWriter

    entered, release = threading.Event(), threading.Event()
    block_next = threading.Event()
    errors = []

    class ControlledIntake(Intake):
        def do_PUT(self):
            if self.path.endswith("/traces") and block_next.is_set():
                block_next.clear()
                entered.set()
                if not release.wait(10):
                    errors.append("blocked intake timed out")
            super().do_PUT()

    server = ThreadingHTTPServer(("127.0.0.1", 18126), ControlledIntake)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    writers = []
    threads = []
    checks = {}
    try:
        if mode == "retirement-races":
            checks.update(retirement_races(block_next, entered, release, errors))
            checks["intake_succeeded"] = not records["errors"] and not errors
            return {
                "mode": mode, "passed": all(checks.values()), "checks": checks,
                "errors": errors + list(records["errors"]),
                "cancelled_responses": list(records["cancelled_responses"]),
            }
        writers = [
            NativeWriter("http://127.0.0.1:18126", api_version="v0.4", sync_mode=True)
            for _ in range(3 if mode == "regular" else 1)
        ]
        survivor = writers[-1]
        exporter = survivor._exporter = Mock(wraps=survivor._exporter)
        previous = telemetry_writer._get_shared_worker()
        if mode == "regular":
            barrier = threading.Barrier(3)

            def teardown(writer):
                try:
                    barrier.wait(5)
                    writer.shutdown_exporter()
                except Exception as error:
                    errors.append(repr(error))

            threads = [threading.Thread(target=teardown, args=(writer,)) for writer in writers[:-1]]
            for thread in threads:
                thread.start()
            barrier.wait(5)
            for thread in threads:
                thread.join(5)
            checks["concurrent_teardown_completed"] = all(not thread.is_alive() for thread in threads) and not errors
            checks["regular_weak_subscriptions_retained"] = all(
                subscriber_count(telemetry_writer, w) == 1 for w in writers
            )
            checks["regular_has_no_pending_lock"] = all(
                not hasattr(w, "_pending_telemetry_worker_lock") for w in writers
            )
            # The test-session API rebuilds the real worker and sends real change notifications.
            telemetry_writer.set_test_session_token("verify-regular-worker-replacement")
            replacement = telemetry_writer._worker
            checks["worker_replaced"] = replacement is not None and replacement is not previous
            checks["survivor_received_replacement"] = exporter.set_telemetry_handle.call_args.args == (replacement,)
            checks["survivor_callback_once"] = subscriber_count(telemetry_writer, survivor) == 1
            retired_refs = [weakref.ref(writer) for writer in writers[:-1]]
            writers[:] = [survivor]
            threads.clear()
            gc.collect()
            checks["retired_writers_collected"] = all(reference() is None for reference in retired_refs)
        else:
            payload = msgpack.packb(
                [
                    [
                        {
                            "trace_id": 100,
                            "span_id": 200,
                            "parent_id": 0,
                            "name": "verify.blocked_send",
                            "resource": "verify.blocked_send",
                            "service": "sample-verify-tracer-app",
                            "start": time.time_ns(),
                            "duration": 1,
                            "error": 0,
                            "meta": {},
                            "metrics": {},
                        }
                    ]
                ],
                use_bin_type=True,
            )

            def send():
                try:
                    survivor._send_payload(payload, 1, survivor._clients[0])
                except Exception as error:
                    errors.append(repr(error))

            block_next.set()
            sender = threading.Thread(target=send)
            threads.append(sender)
            sender.start()
            if not entered.wait(5):
                raise TimeoutError("native send did not reach the controlled intake")
            started = time.monotonic()
            refresh_identity(raise_on_error=True)
            elapsed = time.monotonic() - started
            replacement = telemetry_writer._worker
            checks["refresh_returned_while_send_blocked"] = elapsed < 1 and sender.is_alive() and not release.is_set()
            checks["worker_replaced"] = replacement is not None and replacement is not previous
            pending = survivor._pending_telemetry_worker
            checks["replacement_pending"] = pending is not None and pending[0] is replacement
            release.set()
            sender.join(5)
            checks["blocked_send_completed"] = not sender.is_alive() and not errors
            if sender.is_alive():
                raise TimeoutError("native send did not finish after release")
            send()
            checks["next_send_applied_replacement"] = (
                survivor._pending_telemetry_worker is None
                and exporter.set_telemetry_handle.call_args.args == (replacement,)
                and not errors
            )
            deadline = time.monotonic() + 5
            native_metrics = {}
            while time.monotonic() < deadline:
                telemetry_writer.periodic(force_flush=True)
                with records_lock:
                    native_metrics = exporter_metrics(records["telemetry"], get_runtime_id())
                if all(value > 0 for value in native_metrics.values()):
                    break
                time.sleep(0.05)
            checks["new_worker_exporter_metrics"] = all(value > 0 for value in native_metrics.values())
        checks["intake_succeeded"] = not records["errors"] and not errors
        return {
            "mode": mode, "passed": all(checks.values()), "checks": checks,
            "errors": errors + list(records["errors"]),
            "cancelled_responses": list(records["cancelled_responses"]),
        }
    finally:
        release.set()
        for thread in threads:
            thread.join(12)
        for writer in writers:
            writer.shutdown_exporter()
        telemetry_writer.disable()
        server.shutdown()
        server.server_close()


def retirement_races(block_next, entered, release, errors):
    """#21429 04f1660893 races, with the old writer's native send held at the intake."""
    import signal
    from unittest.mock import patch

    from intake import records
    from intake import records_lock

    from ddtrace._trace.tracer import Tracer
    import ddtrace.internal._runtime_id as runtime_impl

    class ProbeTracer(Tracer):
        pass

    def hold_next_send():
        entered.clear()
        release.clear()
        block_next.set()

    def received(span, wait=0.0):
        deadline = time.monotonic() + wait
        while True:
            with records_lock:
                if any(row["span_id"] == span.span_id for trace in records["traces"] for row in trace):
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def spawn(name, target):
        def body():
            try:
                target()
            except BaseException as error:
                errors.append(name + ": " + repr(error))

        thread = threading.Thread(target=body, name=name, daemon=True)
        thread.start()
        return thread

    def deadlocked():
        result = {"mode": "retirement-races", "passed": False, "error": "deadlocked", "checks": checks}
        print(json.dumps(result, indent=2), flush=True)
        os._exit(1)

    # Without the exporter-lock guard, the default-timeout signal case never returns.
    watchdog = threading.Timer(30, deadlocked)
    watchdog.daemon = True
    watchdog.start()
    checks = {}
    try:
        # r4472488681: a shutdown whose join times out must leave the retirement visible to refresh.
        tracer = ProbeTracer()
        aggregator = tracer._span_aggregator
        old = aggregator.writer
        old.interval = 60
        finish, recreate = aggregator._finish_retired_writer, tracer._recreate
        at_retirement, resume_retirement = threading.Event(), threading.Event()
        at_refresh, resume_refresh = threading.Event(), threading.Event()

        def pause_retirement(*args, **kwargs):
            if threading.current_thread().name == "verify-configure":
                at_retirement.set()
                resume_retirement.wait(10)
            return finish(*args, **kwargs)

        def pause_refresh(*args, **kwargs):
            if threading.current_thread().name == "verify-refresh":
                at_refresh.set()
                resume_refresh.wait(10)
            return recreate(*args, **kwargs)

        try:
            first = tracer.trace("verify.retirement_race.in_flight")
            first.finish()
            hold_next_send()
            old._worker.awake()
            if not entered.wait(5):
                raise TimeoutError("old writer send did not reach the controlled intake")
            stale = tracer.trace("verify.retirement_race.buffered")
            stale.finish()
            checks["join_trace_buffered"] = len(old._encoder) == 1
            with (
                patch.object(aggregator, "_finish_retired_writer", side_effect=pause_retirement),
                patch.object(tracer, "_recreate", side_effect=pause_refresh),
            ):
                configure = spawn("verify-configure", lambda: tracer.configure(trace_processors=[]))
                if not at_retirement.wait(5):
                    raise TimeoutError("configuration did not publish a replacement writer")
                runtime_impl._refresh_runtime_id()
                refresh = spawn(
                    "verify-refresh", lambda: tracer._refresh_runtime_identity(runtime_impl.get_runtime_id())
                )
                if not at_refresh.wait(5):
                    raise TimeoutError("refresh did not pass its shutdown guard")
                tracer.shutdown(timeout=0)
                retirement = aggregator._retired_writers.get(old)
                checks["join_timed_out_retirement_kept"] = retirement is not None and not retirement._finished
                resume_refresh.set()
                refresh.join(5)
                checks["join_refresh_discarded_old_writer"] = not refresh.is_alive() and old._discarded_by_refresh
                # The resumed drain joins the old worker, so release its held send first.
                resume_retirement.set()
                release.set()
                configure.join(5)
                checks["join_configuration_completed"] = not configure.is_alive()
            old.join(5)
            checks["join_in_flight_send_completed"] = received(first, wait=3)
            checks["join_buffered_stale_trace_not_sent"] = not received(stale)
            tracer.shutdown(timeout=0)
            checks["join_exited_retirement_reaped"] = old not in aggregator._retired_writers
        finally:
            resume_refresh.set()
            resume_retirement.set()
            release.set()
            tracer.shutdown(timeout=2)

        # r4472488685: a signal during a manual flush must not wait on a retirement that needs its lock.
        # Bounded first: without the fix the default case deadlocks and ends the run.
        for timeout in (2, None):
            name = "signal_" + ("default" if timeout is None else "bounded") + "_"
            tracer = ProbeTracer()
            aggregator = tracer._span_aggregator
            old = aggregator.writer
            old.interval = 60
            flush = old.flush_queue
            drain_started = threading.Event()
            state = {}

            def draining(*args, **kwargs):
                if threading.current_thread().name == "verify-configure":
                    drain_started.set()
                return flush(*args, **kwargs)

            def on_sigterm(signum, frame):
                state["exporter_lock_owned"] = old._exporter_lock._is_owned()
                started = time.monotonic()
                try:
                    tracer.shutdown(timeout=timeout)
                except BaseException as error:
                    # Raising here would surface inside the interrupted flush instead of failing the check.
                    errors.append("signal shutdown: " + repr(error))
                finally:
                    state["seconds"] = time.monotonic() - started

            def interrupt():
                if not entered.wait(5):
                    raise TimeoutError("manual flush did not reach the controlled intake")
                state["pending"] = tracer.trace("verify.retirement_race.pending_drain")
                state["pending"].finish()
                state["configure"] = spawn("verify-configure", lambda: tracer.configure(trace_processors=[]))
                if not drain_started.wait(5):
                    raise TimeoutError("configuration did not start draining the old writer")
                time.sleep(0.1)  # Let the drain block on the exporter lock owned by the manual flush.
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)  # The handler then runs as soon as the native send returns.
                release.set()

            previous = signal.signal(signal.SIGTERM, on_sigterm)
            try:
                tracer.trace("verify.retirement_race.manual_flush").finish()
                hold_next_send()
                helper = spawn("verify-interrupt", interrupt)
                with patch.object(old, "flush_queue", side_effect=draining):
                    tracer.flush()
                    helper.join(5)
                    configure = state.get("configure")
                    if configure is not None:
                        configure.join(5)
                checks[name + "interrupted_exporter_owner"] = state.get("exporter_lock_owned") is True
                checks[name + "returned_without_waiting"] = state.get("seconds", float("inf")) < 1
                checks[name + "configuration_completed"] = configure is not None and not configure.is_alive()
                checks[name + "retirement_finished"] = old not in aggregator._retired_writers
                checks[name + "pending_drain_sent"] = "pending" in state and received(state["pending"], wait=3)
            finally:
                signal.signal(signal.SIGTERM, previous)
                release.set()
                tracer.shutdown(timeout=2)
    finally:
        watchdog.cancel()
    return checks


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"regular", "blocked-send", "retirement-races"}:
        sys.exit("Usage: lifecycle_control.py regular|blocked-send|retirement-races")
    try:
        result = run(sys.argv[1])
    except Exception as error:
        result = {"passed": False, "error": repr(error)}
    print(json.dumps(result, indent=2), flush=True)
    sys.exit(0 if result["passed"] else 1)
