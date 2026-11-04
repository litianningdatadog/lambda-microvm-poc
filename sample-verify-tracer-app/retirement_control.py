"""Real-tracer cleanup controls; only the failing operation is fault-injected."""

from contextlib import contextmanager, ExitStack
import json
import logging
import os
import sys
import threading
import time
from unittest.mock import patch

from retirement_checks import evaluate_retirements


@contextmanager
def capture_warnings():
    from ddtrace.internal.logger import _RATE_LIMITS

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                records.append(record)

    records = []
    logger = logging.getLogger("ddtrace._trace.processor")
    handler = Capture()
    logger.addHandler(handler)
    try:
        # Repeated injected errors share callsites; observe each real warning rather
        # than losing later cases to the tracer's default one-minute rate limit.
        with patch.dict(_RATE_LIMITS, {
            "Failed to flush and stop retired trace writer during reconfiguration": 0,
            "Failed to stop retired trace writer during shutdown": 0,
        }):
            yield records
    finally:
        logger.removeHandler(handler)


def logged(records, message, error):
    return any(record.getMessage() == message and record.exc_info and record.exc_info[1] is error for record in records)


def caught(call):
    try:
        call()
    except Exception as error:
        return error
    return None


def tracer_closed(tracer):
    from ddtrace.internal import atexit, forksafe

    return (
        not tracer.enabled
        and tracer._wrap_executor is None
        and tracer.context_provider.active() is None
        and tracer.start_span == tracer._start_span_after_shutdown
        and tracer._atexit not in atexit._atexit_wrapped
        and tracer._sample_before_fork not in forksafe._registry_before_fork
        and tracer._child_after_fork not in forksafe._registry
    )


def run_retirements():
    # Imports stay inside the entrypoint so host evaluator tests need no tracer wheel.
    from ddtrace._trace.processor import TraceProcessor
    from ddtrace._trace.provider import DefaultContextProvider
    from ddtrace._trace.tracer import Tracer
    from ddtrace.internal.runtime import get_runtime_id
    from ddtrace.internal.service import ServiceStatus
    from ddtrace.internal.writer import NativeWriter
    from ddtrace.trace import Span
    from intake import records, records_lock

    class ProbeTracer(Tracer):
        # A dedicated subclass leaves the application's global tracer instance alone.
        pass

    class TagTrace(TraceProcessor):
        def process_trace(self, trace):
            trace[0].set_tag("verify.retirement_processor", "applied")
            return trace

    microvm = bool(os.environ.get("AWS_LAMBDA_MICROVM_IMAGE_ARN", "").strip())
    evidence = {}
    for operation in ("flush_queue", "stop"):
        tracer = ProbeTracer()
        aggregator = tracer._span_aggregator
        old = aggregator.writer
        if not isinstance(old, NativeWriter) or aggregator._identity_refresh_enabled is not microvm:
            tracer.shutdown()
            raise RuntimeError("Retirement controls require the native writer and matching MicroVM detection")
        old_processors, old_provider = tracer._span_processors, tracer.context_provider
        provider = DefaultContextProvider()
        processors = [TagTrace()]
        error = RuntimeError("injected retired writer " + operation + " failure")
        case = evidence["configure_" + operation] = {}
        try:
            with patch.object(old, operation, side_effect=error) as fault, capture_warnings() as warnings:
                observed = caught(lambda: tracer.configure(context_provider=provider, trace_processors=processors))
            case["failure_injected"] = fault.call_count == 1
            if microvm:
                case.update(
                    configuration_completed=observed is None,
                    replacement_published=aggregator.writer is not old,
                    span_processors_rebuilt=tracer._span_processors is not old_processors,
                    context_provider_updated=tracer.context_provider is provider,
                    warning_logged=logged(warnings, "Failed to flush and stop retired trace writer during reconfiguration", error),
                    retirement_state_correct=(old in aggregator._retired_writers) is (operation == "stop"),
                )
                # Exercise the installed processor and native exporter after the injected failure.
                with tracer.start_span("verify.retirement." + operation, child_of=None) as span:
                    pass
                case["user_processor_applied"] = (
                    aggregator.user_processors is processors and span.get_tag("verify.retirement_processor") == "applied"
                )
                tracer.flush()
                deadline = time.monotonic() + 3
                received = False
                while time.monotonic() < deadline:
                    with records_lock:
                        received = any(
                            row["span_id"] == span.span_id
                            and row.get("meta", {}).get("runtime-id") == get_runtime_id()
                            and row.get("meta", {}).get("verify.retirement_processor") == "applied"
                            for trace in records["traces"] for row in trace
                        )
                    if received:
                        break
                    time.sleep(0.02)
                case["fresh_trace_received"] = received
            else:
                case["error_preserved"] = observed is error
                case["configuration_unchanged"] = (
                    aggregator.writer is old and tracer._span_processors is old_processors
                    and tracer.context_provider is old_provider and not aggregator._retired_writers
                )
        finally:
            tracer.shutdown()
        case["cleanup_completed"] = tracer_closed(tracer) and not aggregator._retired_writers

    for fail_active in ((False, True) if microvm else (True,)):
        tracer = ProbeTracer()
        aggregator = tracer._span_aggregator
        retired = []
        errors = [RuntimeError("injected first retirement failure"), ValueError("injected second retirement failure")]
        active_error = RuntimeError("injected active writer failure")
        case = evidence["shutdown_active_failure" if fail_active else "shutdown_retired_failures"] = {}
        try:
            if microvm:
                for error in errors:
                    writer = aggregator.writer
                    # Start a real service so retained failed workers must actually be stopped on retry.
                    writer.start()
                    retired.append(writer)
                    with patch.object(writer, "stop", side_effect=error):
                        # Keep going on an older wheel so this case diagnoses shutdown independently.
                        caught(lambda: tracer.configure(trace_processors=[]))
                case["retirements_created"] = all(writer in aggregator._retired_writers for writer in retired)
            active = aggregator.writer
            active.start()
            stops = []

            def fail(index):
                def stop(*args, **kwargs):
                    stops.append(index)
                    raise errors[index]
                return stop

            original_stop = active.stop

            def stop_active(*args, **kwargs):
                stops.append("active")
                if fail_active:
                    raise active_error
                return original_stop(*args, **kwargs)

            tracer.context_provider.activate(Span("verify.retirement.unfinished"))
            tracer._wrap_executor = lambda *args: None
            with ExitStack() as patches, capture_warnings() as warnings:
                for index, writer in enumerate(retired):
                    patches.enter_context(patch.object(writer, "stop", side_effect=fail(index)))
                patches.enter_context(patch.object(active, "stop", side_effect=stop_active))
                observed = caught(lambda: tracer.shutdown(timeout=2))
            if microvm:
                case.update(
                    all_writers_attempted=stops == [0, 1, "active"],
                    all_failures_logged=all(logged(warnings, "Failed to stop retired trace writer during shutdown", error) for error in errors),
                    active_error_preserved=observed is active_error if fail_active else observed is None,
                    failed_retirements_retained=all(writer in aggregator._retired_writers for writer in retired),
                )
            else:
                case["error_preserved"] = observed is active_error
            case["tracer_state_correct"] = (
                tracer.enabled and tracer.start_span != tracer._start_span_after_shutdown
                if fail_active else tracer_closed(tracer)
            )
        finally:
            tracer.shutdown(timeout=2)
        case["retry_completed"] = (
            tracer_closed(tracer) and not aggregator._retired_writers
            and all(writer.status is ServiceStatus.STOPPED for writer in retired + [active])
        )

    if microvm:
        tracer = ProbeTracer()
        aggregator = tracer._span_aggregator
        old, old_processors, provider = aggregator.writer, tracer._span_processors, tracer.context_provider
        error = RuntimeError("injected replacement construction failure")
        case = evidence["replacement_build_failure"] = {}
        try:
            with patch.object(old, "_recreate", side_effect=error):
                observed = caught(lambda: tracer.configure(context_provider=DefaultContextProvider(), trace_processors=[]))
            case["error_preserved"] = observed is error
            case["configuration_unchanged"] = (
                aggregator.writer is old and old._accepting_writes and not aggregator._retired_writers
                and tracer._span_processors is old_processors and tracer.context_provider is provider
            )
        finally:
            tracer.shutdown()
        case["cleanup_completed"] = tracer_closed(tracer)
    return evidence


if __name__ == "__main__":
    # Standalone credential-free controls use the same real HTTP intake as the guest.
    os.environ["VERIFY_FORWARD_TO_AGENT"] = "false"
    from http.server import ThreadingHTTPServer
    from intake import Intake

    server = ThreadingHTTPServer(("127.0.0.1", 18126), Intake)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        evidence = run_retirements()
        checks = evaluate_retirements(evidence, bool(os.environ.get("AWS_LAMBDA_MICROVM_IMAGE_ARN", "").strip()))
        result = {"passed": all(checks.values()), "checks": checks, "retirements": evidence}
    except Exception as error:
        result = {"passed": False, "error": repr(error)}
    finally:
        from ddtrace import tracer
        from ddtrace.internal.telemetry import telemetry_writer

        tracer.shutdown(timeout=2)
        telemetry_writer.disable()
        server.shutdown()
        server.server_close()
    print(json.dumps(result, indent=2), flush=True)
    sys.exit(0 if result["passed"] else 1)
