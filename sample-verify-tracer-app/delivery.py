"""Bounded delivery scenarios driven only by the tracer's normal native timer."""

import copy
from http.client import HTTPConnection
import json
import os
import threading
import time
import uuid

from ddtrace import tracer
from ddtrace.internal.runtime import get_runtime_id

import intake
from lifecycle_checks import SCHEMA


AUTOMATIC_OPERATION = "verify.delivery.automatic"
TERMINATION_OPERATION = "verify.delivery.termination"
LOW_64 = (1 << 64) - 1


def _identity(span):
    return {"trace_id": str(span.trace_id & LOW_64), "span_id": str(span.span_id)}


def _errors_since(index):
    with intake.records_lock:
        return list(intake.records["errors"][index:])


def _last_sequence():
    with intake.records_lock:
        return intake.trace_requests[-1]["sequence"] if intake.trace_requests else 0


def _matches(record, identity, operation, marker, runtime_id):
    return any(
        span.get("trace_id") == identity.get("trace_id")
        and span.get("span_id") == identity.get("span_id")
        and span.get("name") == operation
        and span.get("parent_id", "0") == "0"
        and span.get("meta", {}).get("verify.marker") == marker
        and span.get("meta", {}).get("runtime-id") == runtime_id
        and span.get("metrics", {}).get("_dd.measured") == 1
        for span in record["spans"]
    )


def _acknowledgements(after, identity, operation, marker, runtime_id, started, deadline):
    return [
        record
        for record in intake.snapshot_requests(after)
        if record["outcome"] == "accepted"
        and isinstance(record["status"], int)
        and 200 <= record["status"] < 300
        and record["completed_at"] is not None
        and started <= record["received_at"] <= record["completed_at"] <= deadline
        and record["forwarded"] == intake.FORWARD
        and record["bytes"] > 0
        and _matches(record, identity, operation, marker, runtime_id)
    ]


def _wait_for(read_evidence, deadline, description):
    while True:
        evidence = read_evidence()
        if evidence:
            return evidence
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(description)
        time.sleep(min(0.025, remaining))


class Delivery:
    def __init__(self, state):
        # /ready treats an empty app state as the signal to prepare the image.
        self.state = state
        self.lock = threading.Lock()
        self._termination_called = False

    def _begin(self, scenario, deadline_ms):
        started = time.monotonic()
        with intake.records_lock:
            error_index = len(intake.records["errors"])
        runtime_id = get_runtime_id()
        phase = {
            "verification_schema": SCHEMA,
            "scenario": scenario,
            "verification_id": os.environ.get("DD_VERSION", "local"),
            "runtime_id": runtime_id,
            "passed": False,
            "checks": {},
            "evidence": {
                "runtime_id_before": runtime_id,
                "runtime_id_after": runtime_id,
                "forward_to_agent": intake.FORWARD,
                "manual_flush_count_before": self.state.get("manual_flush_count", 0),
                "manual_flush_count_after": self.state.get("manual_flush_count", 0),
                "started_at": started,
                "elapsed_ms": 0,
                "deadline_ms": deadline_ms,
                "unexpected_errors": [],
            },
        }
        return phase, error_index

    def _finish(self, phase, error_index):
        evidence = phase["evidence"]
        evidence.update(
            runtime_id_after=get_runtime_id(),
            manual_flush_count_after=self.state.get("manual_flush_count", 0),
            elapsed_ms=(time.monotonic() - evidence["started_at"]) * 1000,
        )
        evidence["unexpected_errors"].extend(_errors_since(error_index))
        phase["passed"] = bool(phase["checks"]) and all(phase["checks"].values()) and not evidence["unexpected_errors"]
        return phase

    def _unavailable(self, scenario, message, deadline_ms):
        phase, error_index = self._begin(scenario, deadline_ms)
        phase["error"] = message
        phase["checks"] = {"initial_verification_completed": bool(self.state.get("result")), "scenario_available": False}
        return self._finish(phase, error_index)

    def _root(self, operation, marker, runtime_id):
        root = tracer.start_span(operation, child_of=None, activate=True, service="sample-verify-tracer-app")
        root.set_metric("_dd.measured", 1)
        root.set_tag("verify.marker", marker)
        root.set_tag("runtime-id", runtime_id)
        return root

    def automatic(self):
        if not self.state.get("result"):
            return self._unavailable("automatic", "Initial /verify must complete before delivery verification", 10000)
        if not self.lock.acquire(blocking=False):
            return self._unavailable("automatic", "Another delivery or feature scenario is active", 10000)
        try:
            return self._single("automatic", AUTOMATIC_OPERATION)
        finally:
            self.lock.release()

    def _single(self, scenario, operation, armed=None):
        phase, error_index = self._begin(scenario, 10000)
        evidence = phase["evidence"]
        deadline = evidence["started_at"] + 10
        runtime_id = phase["runtime_id"]
        marker = armed["marker"] if armed else str(uuid.uuid4())
        evidence.update(operation=operation, marker=marker, span={}, successful_requests=[])
        if scenario == "termination":
            phase["marker"] = marker
            phase["lifecycle"] = dict(self.state.get("lifecycle", {}))
        interval_ok = False
        try:
            if scenario == "automatic":
                from ddtrace.internal.writer.writer import NativeWriter

                writer = tracer._span_aggregator.writer
                evidence["flush_interval_ms"] = writer.interval * 1000
                evidence["writer_type"] = type(writer).__name__
                evidence["writer_sync_mode"] = writer._sync_mode
                interval_ok = isinstance(writer, NativeWriter) and writer.interval == 1.0 and not writer._sync_mode
            after = _last_sequence()
            with self._root(operation, marker, runtime_id) as root:
                evidence["span"] = _identity(root)
                if scenario == "termination":
                    correlation = tracer.get_log_correlation_context()
                    evidence["log_correlation"] = correlation
                    marker_log = {
                        "msg": "tracer_termination_marker",
                        "verification_id": phase["verification_id"],
                        "runtime_id": runtime_id,
                        "marker": marker,
                        **evidence["span"],
                        "full_trace_id": str(root.trace_id),
                        "log_correlation": correlation,
                        **correlation,
                    }
                    evidence["marker_log"] = marker_log
                    print(json.dumps(marker_log), flush=True)
            evidence["successful_requests"] = _wait_for(
                lambda: _acknowledgements(after, evidence["span"], operation, marker, runtime_id, evidence["started_at"], deadline),
                deadline,
                f"No completed native response acknowledged {operation} within 10s",
            )
        except Exception as error:
            evidence["unexpected_errors"].append(f"{type(error).__name__}: {error}")
        self._finish(phase, error_index)
        checks = {
            f"{scenario}_no_manual_flush": evidence["manual_flush_count_before"] == evidence["manual_flush_count_after"],
            f"{scenario}_trace_acknowledged": bool(evidence["successful_requests"]),
            f"{scenario}_identity": evidence["runtime_id_before"] == evidence["runtime_id_after"],
            f"{scenario}_deadline": 0 < evidence["elapsed_ms"] <= evidence["deadline_ms"],
        }
        if scenario == "automatic":
            checks["automatic_default_interval"] = interval_ok
        else:
            checks["termination_hook_once"] = self.state.get("lifecycle", {}).get("terminate") == 1
            checks["termination_identity"] = checks["termination_identity"] and armed["runtime_id"] == runtime_id
        phase["checks"] = checks
        phase["passed"] = all(checks.values()) and not evidence["unexpected_errors"]
        return phase

    def recovery(self):
        if not self.state.get("result"):
            return self._unavailable("recovery", "Initial /verify must complete before recovery verification", 40000)
        if not self.lock.acquire(blocking=False):
            return self._unavailable("recovery", "Another delivery or feature scenario is active", 40000)
        try:
            phase, error_index = self._begin("recovery", 40000)
            evidence = phase["evidence"]
            evidence["cases"] = []
            for mode in ("http503", "disconnect"):
                evidence["cases"].append(self._recovery_case(mode))
            self._finish(phase, error_index)
            cases = evidence["cases"]
            checks = {
                "recovery_both_failures_observed": len(cases) == 2 and all(case["failure_attempts"] for case in cases),
                "recovery_application_responsive": all(
                    len(case["probes"]) == 3
                    and all(probe["status"] == 200 and probe["runtime_id"] == phase["runtime_id"] for probe in case["probes"])
                    for case in cases
                ),
                "recovery_workload_bounded": all(
                    case["offered_count"] == 24
                    and len({(span["trace_id"], span["span_id"]) for span in case["offered_spans"]}) == 24
                    and 0 < case["elapsed_ms"] <= case["deadline_ms"]
                    for case in cases
                ) and evidence["elapsed_ms"] <= evidence["deadline_ms"],
                "recovery_fresh_delivery": all(
                    case["fresh_acknowledgements"]
                    and case["fresh_created_at"] >= case["restored_at"]
                    and case["fresh_span"] not in case["offered_spans"]
                    for case in cases
                ),
                "recovery_identity_stable": evidence["runtime_id_before"] == evidence["runtime_id_after"]
                and all(case["runtime_id_before"] == case["runtime_id_after"] == phase["runtime_id"] for case in cases),
                "recovery_no_manual_flush": evidence["manual_flush_count_before"] == evidence["manual_flush_count_after"],
                "recovery_no_unexpected_errors": not evidence["unexpected_errors"] and all(not case["unexpected_errors"] for case in cases),
            }
            phase["checks"] = checks
            phase["passed"] = all(checks.values())
            return phase
        finally:
            intake.set_fault()
            self.lock.release()

    def _probe(self, deadline):
        started = time.monotonic()
        probe = {"at": started, "status": None, "runtime_id": None, "elapsed_ms": 0}
        connection = HTTPConnection("127.0.0.1", 8080, timeout=max(0.001, min(1, deadline - started)))
        try:
            if started >= deadline:
                raise TimeoutError("Recovery health probe exceeded case deadline")
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read()
            probe.update(status=response.status, runtime_id=json.loads(body).get("runtime_id"))
        except Exception as error:
            probe["error"] = f"{type(error).__name__}: {error}"
        finally:
            connection.close()
            probe["elapsed_ms"] = (time.monotonic() - started) * 1000
        return probe

    def _recovery_case(self, mode):
        started = time.monotonic()
        deadline = started + 20
        runtime_id = get_runtime_id()
        with intake.records_lock:
            error_index = len(intake.records["errors"])
        case = {
            "mode": mode,
            "operation": f"verify.delivery.recovery.{mode}",
            "offered_spans": [],
            "offered_count": 0,
            "fresh_span": {},
            "fresh_marker": str(uuid.uuid4()),
            "failure_attempts": [],
            "fresh_acknowledgements": [],
            "probes": [],
            "fault_active_at": started,
            "restored_at": None,
            "fresh_created_at": None,
            "elapsed_ms": 0,
            "deadline_ms": 20000,
            "runtime_id_before": runtime_id,
            "runtime_id_after": runtime_id,
            "unexpected_errors": [],
        }
        after = _last_sequence()
        intake.set_fault(mode)
        try:
            for _ in range(24):
                with self._root(f"verify.delivery.outage.{mode}", case["fresh_marker"], runtime_id) as root:
                    case["offered_spans"].append(_identity(root))
            case["offered_count"] = len(case["offered_spans"])
            offered = {(span["trace_id"], span["span_id"]) for span in case["offered_spans"]}

            def failed_attempts():
                return [
                    record
                    for record in intake.snapshot_requests(after)
                    if record["outcome"] == mode
                    and record["status"] == (503 if mode == "http503" else None)
                    and any((span["trace_id"], span["span_id"]) in offered for span in record["spans"])
                ]

            _wait_for(failed_attempts, min(deadline, started + 10), f"Native exporter never encountered controlled {mode}")
            for _ in range(3):
                probe = self._probe(deadline)
                case["probes"].append(probe)
                if "error" in probe:
                    case["unexpected_errors"].append(probe["error"])
            case["failure_attempts"] = failed_attempts()
        except Exception as error:
            case["unexpected_errors"].append(f"{type(error).__name__}: {error}")
        finally:
            intake.set_fault()
            case["restored_at"] = time.monotonic()
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("No time remains for fresh native delivery after recovery")
            fresh_after = _last_sequence()
            case["fresh_created_at"] = time.monotonic()
            with self._root(case["operation"], case["fresh_marker"], runtime_id) as root:
                case["fresh_span"] = _identity(root)
            case["fresh_acknowledgements"] = _wait_for(
                lambda: _acknowledgements(
                    fresh_after, case["fresh_span"], case["operation"], case["fresh_marker"], runtime_id, case["fresh_created_at"], deadline
                ),
                deadline,
                f"Fresh native root was not acknowledged after {mode} restoration",
            )
        except Exception as error:
            case["unexpected_errors"].append(f"{type(error).__name__}: {error}")
        case["runtime_id_after"] = get_runtime_id()
        case["elapsed_ms"] = (time.monotonic() - started) * 1000
        case["unexpected_errors"].extend(_errors_since(error_index))
        return case

    def arm(self):
        if not self.state.get("result"):
            return self._unavailable("termination-armed", "Initial /verify must complete before termination is armed", 10000)
        if not self.lock.acquire(blocking=False):
            return self._unavailable("termination-armed", "Another delivery or feature scenario is active", 10000)
        try:
            if "delivery_termination_arm" not in self.state:
                self.state["delivery_termination_arm"] = {
                    "verification_schema": SCHEMA,
                    "scenario": "termination-armed",
                    "armed": True,
                    "marker": str(uuid.uuid4()),
                    "runtime_id": get_runtime_id(),
                    "operation": TERMINATION_OPERATION,
                    "verification_id": os.environ.get("DD_VERSION", "local"),
                }
            return dict(self.state["delivery_termination_arm"])
        finally:
            self.lock.release()

    def terminate(self):
        armed = self.state.get("delivery_termination_arm")
        if not armed:
            return {"status": "ok"}
        if not self.lock.acquire(blocking=False):
            result = self._unavailable("termination", "Another delivery or feature scenario is active during termination", 10000)
        else:
            try:
                if self._termination_called:
                    result = copy.deepcopy(self.state["delivery_termination_result"])
                    result.update(passed=False, lifecycle=dict(self.state.get("lifecycle", {})), error="Termination hook invoked more than once")
                    result["checks"]["termination_hook_once"] = False
                else:
                    self._termination_called = True
                    result = self._single("termination", TERMINATION_OPERATION, armed)
                    self.state["delivery_termination_result"] = result
            finally:
                self.lock.release()
        print(json.dumps({"msg": "tracer_termination_result", **result}), flush=True)
        return result
