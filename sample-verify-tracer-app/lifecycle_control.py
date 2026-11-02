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
    if microvm != (mode == "blocked-send"):
        raise ValueError("regular requires MicroVM detection disabled; blocked-send requires it enabled")
    # Set before importing ddtrace: all traffic stays on the in-process intake.
    os.environ["VERIFY_FORWARD_TO_AGENT"] = "false"
    from http.server import ThreadingHTTPServer
    from unittest.mock import Mock

    from app import Intake
    from app import records
    from app import records_lock
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
        return {"mode": mode, "passed": all(checks.values()), "checks": checks, "errors": errors}
    finally:
        release.set()
        for thread in threads:
            thread.join(12)
        for writer in writers:
            writer.shutdown_exporter()
        telemetry_writer.disable()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"regular", "blocked-send"}:
        sys.exit("Usage: lifecycle_control.py regular|blocked-send")
    try:
        result = run(sys.argv[1])
    except Exception as error:
        result = {"passed": False, "error": repr(error)}
    print(json.dumps(result, indent=2), flush=True)
    sys.exit(0 if result["passed"] else 1)
