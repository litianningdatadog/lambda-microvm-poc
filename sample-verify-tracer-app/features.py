"""Exercise common tracing features after a MicroVM identity refresh."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading

from feature_checks import EXPECTED_ERROR
from feature_checks import FEATURE_OPERATIONS
import requests

from ddtrace import tracer


def span_identity(span):
    return {"trace_id": str(span.trace_id & ((1 << 64) - 1)), "span_id": str(span.span_id)}


class CaptureLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def emit(self, record):
        self.fields = {name: getattr(record, name, None) for name in ("dd.trace_id", "dd.span_id", "dd.service")}


def run_features():
    evidence = {"roots": {}, "errors": []}

    def manual(root):
        with tracer.trace("verify.feature.child") as child:
            child.set_tag("verify.case", "nested")
            child.set_metric("verify.count", 3)
            identity = span_identity(child)
        capture = CaptureLog()
        logger = logging.getLogger("verify.features")
        logger.addHandler(capture)
        try:
            expected = tracer.get_log_correlation_context()
            # Explicit level avoids changing the application's logger configuration.
            logger.warning("microvm feature log correlation")
        finally:
            logger.removeHandler(capture)
        return {
            "child": identity,
            "log": capture.fields,
            "expected_log": {"trace_id": expected["dd.trace_id"], "span_id": expected["dd.span_id"]},
        }

    def error(root):
        raise ValueError(EXPECTED_ERROR)

    def database(root):
        connection = sqlite3.connect(":memory:")
        try:
            return {"answer": connection.execute("SELECT 42 AS answer").fetchone()[0]}
        finally:
            connection.close()

    def http(root):
        # Keep all dependencies inside this guest, including HTTP propagation.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get("http://127.0.0.1:8080/feature/echo", timeout=3)
            response.raise_for_status()
            result = response.json()
            failure = session.get("http://127.0.0.1:8080/feature/error", timeout=3)
            result["error_status"] = failure.status_code
            return result

    def asynchronous(root):
        async def exercise():
            entered = asyncio.Event()
            count = 0

            async def task(index):
                nonlocal count
                with tracer.trace("verify.feature.async_task", resource=f"task-{index}") as child:
                    count += 1
                    if count == 2:
                        entered.set()
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    await asyncio.sleep(0)
                    return {**span_identity(child), "isolated": tracer.current_span() is child}

            return await asyncio.gather(task(0), task(1))

        tasks = asyncio.run(exercise())
        return {"tasks": tasks, "parent_restored": tracer.current_span() is root}

    def threads(root):
        entered = threading.Barrier(2, timeout=3)

        def task(index):
            with tracer.trace("verify.feature.thread_task", resource=f"task-{index}") as child:
                entered.wait()
                return {**span_identity(child), "isolated": tracer.current_span() is child}

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(task, index) for index in range(2)]
            tasks = [future.result(timeout=5) for future in futures]
        return {"tasks": tasks, "parent_restored": tracer.current_span() is root}

    cases = {
        "manual": manual,
        "error": error,
        "database": database,
        "http": http,
        "async": asynchronous,
        "threads": threads,
    }
    for case, operation in FEATURE_OPERATIONS.items():
        try:
            resource = "manual.after_refresh" if case == "manual" else case + ".after_refresh"
            with tracer.start_span(operation, child_of=None, activate=True, resource=resource) as root:
                evidence["roots"][case] = {**span_identity(root), "full_trace_id": str(root.trace_id)}
                evidence[case] = cases[case](root)
        except Exception as exception:
            if case != "error" or not isinstance(exception, ValueError) or str(exception) != EXPECTED_ERROR:
                evidence["errors"].append(f"{case}: {type(exception).__name__}: {exception}")
    return evidence
