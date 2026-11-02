"""Pure, fail-closed assertions over schema-4 delivery evidence.

Decoded receipt is not delivery: a matching request must have finished its
successful response, and AWS evidence must show actual Agent forwarding.
"""

import math


DELIVERY_OPERATIONS = (
    "verify.delivery.automatic",
    "verify.delivery.recovery.http503",
    "verify.delivery.recovery.disconnect",
    "verify.delivery.termination",
)

_PHASE_CHECKS = {
    "automatic": (
        "automatic_default_interval", "automatic_no_manual_flush",
        "automatic_trace_acknowledged", "automatic_identity", "automatic_deadline",
    ),
    "recovery": (
        "recovery_both_failures_observed", "recovery_application_responsive",
        "recovery_workload_bounded", "recovery_fresh_delivery",
        "recovery_identity_stable", "recovery_no_manual_flush",
        "recovery_no_unexpected_errors",
    ),
    "termination": (
        "termination_no_manual_flush", "termination_trace_acknowledged",
        "termination_identity", "termination_deadline", "termination_hook_once",
    ),
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _count(value, positive=False):
    return type(value) is int and (value > 0 if positive else value >= 0)


def _id(value):
    return (
        isinstance(value, str) and 0 < len(value) <= 20
        and value.isascii() and value.isdigit()
        and 0 < int(value) < 2 ** 64
        and str(int(value)) == value
    )


def _identity(value):
    value = _dict(value)
    return _id(value.get("trace_id")) and _id(value.get("span_id"))


def _pair(value):
    value = _dict(value)
    return value.get("trace_id"), value.get("span_id")


def _wire_id(value):
    if type(value) is int and 0 < value < 2 ** 64:
        return str(value)
    return value if _id(value) else None


def _summary(value, required=()):
    checks = _dict(value.get("checks"))
    return (
        value.get("passed") is True and bool(checks)
        and all(checks.get(key) is True for key in required)
        and all(check is True for check in checks.values())
    )


def _lifecycle(value, suspend, resume, terminate):
    value = _dict(value)
    expected = {"run": 1, "suspend": suspend, "resume": resume, "terminate": terminate}
    return all(type(value.get(key)) is int and value[key] == count for key, count in expected.items())


def _bounds(start, elapsed, deadline, expected):
    return (
        _number(start) and _number(elapsed, positive=True)
        and _number(deadline, positive=True) and deadline == expected
        and elapsed <= deadline
    )


def _between(value, start, end):
    return _number(value) and start <= value <= end


def log_field(attributes, name, default=None):
    """Read a dotted wire key or its nested Datadog log representation."""
    attributes = _dict(attributes)
    if name in attributes:
        return attributes[name]
    for key in name.split("."):
        if not isinstance(attributes, dict) or key not in attributes:
            return default
        attributes = attributes[key]
    return attributes


def _log_form(value):
    if isinstance(value, list):
        return [_log_form(item) for item in value]
    if not isinstance(value, dict):
        return value
    fields = {}

    def collect(mapping, prefix):
        for key, item in mapping.items():
            path = prefix + tuple(key.split("."))
            if isinstance(item, dict) and item:
                collect(item, path)
            else:
                if path in fields:
                    raise ValueError("Conflicting dotted and nested log fields")
                fields[path] = _log_form(item)

    collect(value, ())
    return fields


def log_values_equal(actual, expected):
    """Allow dotted-key nesting only; retain nulls, list order and all values."""
    try:
        return _log_form(actual) == _log_form(expected)
    except ValueError:
        return False


def _span_matches(span, identity, operation, runtime_id, marker=None):
    span = _dict(span)
    meta = _dict(span.get("meta"))
    measured = log_field(span.get("metrics"), "_dd.measured")
    parent_id = span.get("parent_id", 0)
    return (
        _identity(identity)
        and (_wire_id(span.get("trace_id")), _wire_id(span.get("span_id"))) == _pair(identity)
        and span.get("name") == operation
        and span.get("service") == "sample-verify-tracer-app"
        and type(parent_id) in (int, str) and parent_id in (0, "0")
        and _number(measured, positive=True) and measured == 1
        and meta.get("runtime-id") == runtime_id
        and (marker is None or log_field(meta, "verify.marker") == marker)
    )


def _request(record):
    return (
        _count(record.get("sequence"), positive=True)
        and _count(record.get("bytes"), positive=True)
        and isinstance(record.get("spans"), list)
        and type(record.get("forwarded")) is bool
    )


def _acknowledged(records, identity, operation, runtime_id, marker, start, end, forward):
    if not (_identity(identity) and _text(marker) and _text(runtime_id)):
        return False
    for item in _list(records):
        record = _dict(item)
        received, completed = record.get("received_at"), record.get("completed_at")
        if not (
            _request(record) and record.get("outcome") == "accepted"
            and type(record.get("status")) is int and 200 <= record["status"] < 300
            and record.get("forwarded") is forward
            and _between(received, start, end) and _between(completed, received, end)
        ):
            continue
        if any(_span_matches(span, identity, operation, runtime_id, marker) for span in record["spans"]):
            return True
    return False


def _failure(case, runtime_id, identities, start, restored):
    mode = case.get("mode")
    for item in _list(case.get("failure_attempts")):
        record = _dict(item)
        if not (
            _request(record) and record.get("forwarded") is False
            and record.get("outcome") == mode
            and _between(record.get("received_at"), start, restored)
        ):
            continue
        if mode == "http503":
            response = type(record.get("status")) is int and record["status"] == 503 and _between(
                record.get("completed_at"), record["received_at"], restored
            )
        else:
            response = record.get("status") is None and record.get("completed_at") is None
        if response and any(
            _span_matches(span, identity, f"verify.delivery.outage.{mode}", runtime_id)
            for span in record["spans"] for identity in identities
        ):
            return True
    return False


def _recovery_cases(evidence, runtime_id, forward, start, end):
    cases = _list(evidence.get("cases"))
    result = {
        "recovery_both_failures_observed": len(cases) == 2,
        "recovery_application_responsive": len(cases) == 2,
        "recovery_workload_bounded": len(cases) == 2,
        "recovery_fresh_delivery": len(cases) == 2,
        "recovery_identity_stable": len(cases) == 2,
        "recovery_case_deadlines": len(cases) == 2,
    }
    previous_end = start
    all_roots = []
    for index, item in enumerate(cases):
        case = _dict(item)
        mode = ("http503", "disconnect")[index] if index < 2 else None
        offered = _list(case.get("offered_spans"))
        valid_offered = len(offered) == 24 and all(_identity(span) for span in offered)
        unique = valid_offered and all(
            len({_dict(span)[key] for span in offered}) == 24 for key in ("trace_id", "span_id")
        )
        fresh = case.get("fresh_span")
        identities = [*offered, fresh]
        if valid_offered and _identity(fresh):
            all_roots.extend(_pair(identity) for identity in identities)
        else:
            result["recovery_workload_bounded"] = False
        active, restored, created = (case.get(key) for key in ("fault_active_at", "restored_at", "fresh_created_at"))
        elapsed, deadline = case.get("elapsed_ms"), case.get("deadline_ms")
        timing = _bounds(active, elapsed, deadline, 20000)
        case_end = active + elapsed / 1000 if timing else start
        timing = (
            timing and _between(active, previous_end, end)
            and _between(case_end, active, end)
            and _between(restored, active, case_end)
            and _between(created, restored, case_end) and created > restored
        )
        result["recovery_case_deadlines"] &= timing
        if timing:
            previous_end = case_end
        ordered = mode is not None and case.get("mode") == mode
        result["recovery_workload_bounded"] &= unique and case.get("offered_count") == 24 and type(case.get("offered_count")) is int
        result["recovery_identity_stable"] &= (
            case.get("runtime_id_before") == runtime_id == case.get("runtime_id_after")
            and case.get("unexpected_errors") == []
        )
        result["recovery_both_failures_observed"] &= (
            ordered and timing and valid_offered and _failure(case, runtime_id, offered, active, restored)
        )
        probes = _list(case.get("probes"))
        probe_times = [_dict(probe).get("at") for probe in probes]
        responsive = len(probes) == 3 and timing
        for probe in probes:
            probe = _dict(probe)
            duration = probe.get("elapsed_ms")
            responsive = responsive and (
                type(probe.get("status")) is int and probe["status"] == 200
                and probe.get("runtime_id") == runtime_id
                and _number(duration, positive=True) and duration <= 20000
                and _between(probe.get("at"), active, restored)
                and probe["at"] + duration / 1000 <= restored
            )
        result["recovery_application_responsive"] &= responsive and all(
            _number(before) and _number(after) and before < after
            for before, after in zip(probe_times, probe_times[1:])
        )
        result["recovery_fresh_delivery"] &= (
            ordered and timing and unique and _identity(fresh)
            and _dict(fresh).get("trace_id") not in {_dict(span).get("trace_id") for span in offered if _identity(span)}
            and case.get("operation") == f"verify.delivery.recovery.{mode}"
            and _acknowledged(
                case.get("fresh_acknowledgements"), fresh, case.get("operation"), runtime_id,
                case.get("fresh_marker"), created, case_end, forward,
            )
        )
    result["recovery_workload_bounded"] &= len(all_roots) == 50 and all(
        len({root[index] for root in all_roots}) == 50 for index in (0, 1)
    )
    return result


def evaluate_phase(value, runtime_id, require_forward=True):
    """Validate wire evidence independently of producer summary booleans."""
    value = _dict(value)
    evidence = _dict(value.get("evidence"))
    scenario = value.get("scenario")
    known = isinstance(scenario, str) and scenario in _PHASE_CHECKS
    start, elapsed, deadline = (evidence.get(key) for key in ("started_at", "elapsed_ms", "deadline_ms"))
    timing = _bounds(start, elapsed, deadline, 40000 if scenario == "recovery" else 10000)
    end = start + elapsed / 1000 if timing else 0
    forward = evidence.get("forward_to_agent")
    before, after = evidence.get("manual_flush_count_before"), evidence.get("manual_flush_count_after")
    checks = {
        "delivery_schema": type(value.get("verification_schema")) is int and value["verification_schema"] == 4,
        "delivery_summary": known and _summary(value, _PHASE_CHECKS[scenario]),
        "delivery_verification_id": _text(value.get("verification_id")),
        "delivery_identity": _text(runtime_id) and value.get("runtime_id") == runtime_id
        and evidence.get("runtime_id_before") == runtime_id == evidence.get("runtime_id_after"),
        "delivery_forwarding": type(forward) is bool and (not require_forward or forward is True),
        "delivery_no_manual_flush": _count(before) and _count(after) and before == after,
        "delivery_no_unexpected_errors": evidence.get("unexpected_errors") == [],
        "delivery_deadline": timing,
    }
    if scenario in ("automatic", "termination"):
        operation = DELIVERY_OPERATIONS[0 if scenario == "automatic" else 3]
        checks[f"{scenario}_trace_acknowledged"] = (
            timing and evidence.get("operation") == operation
            and _acknowledged(evidence.get("successful_requests"), evidence.get("span"), operation,
                              runtime_id, evidence.get("marker"), start, end, forward)
        )
        if scenario == "automatic":
            checks["automatic_default_interval"] = type(evidence.get("flush_interval_ms")) in (int, float) and evidence["flush_interval_ms"] == 1000
        else:
            checks["termination_hook_once"] = _lifecycle(value.get("lifecycle"), 3, 3, 1)
            checks["termination_marker"] = _text(value.get("marker")) and value["marker"] == evidence.get("marker")
    elif scenario == "recovery":
        checks.update(_recovery_cases(evidence, runtime_id, forward, start if timing else 0, end))
    return {key: bool(check) for key, check in checks.items()}


def evaluate_cycles(vm, samples, runtime_id):
    """Require three real, ordered platform transitions and fresh feature samples."""
    vm = _dict(vm)
    cycles, samples = _list(vm.get("cycles")), _list(samples)
    checks = {
        "cycles_count": len(cycles) == 3 and len(samples) == 3,
        "cycles_transitions": len(cycles) == 3,
        "cycles_identity": _text(runtime_id),
        "cycles_features": len(samples) == 3,
        "cycles_lifecycle": len(samples) == 3,
    }
    previous = 0
    verification_id = None
    vm_id = vm.get("microvmId")
    for index, item in enumerate(cycles, 1):
        cycle = _dict(item)
        suspended, resumed = _dict(cycle.get("suspended")), _dict(cycle.get("resumed"))
        pause, resume = suspended.get("time"), resumed.get("time")
        valid = (
            type(cycle.get("cycle")) is int and cycle["cycle"] == index
            and suspended.get("state") == "SUSPENDED" and resumed.get("state") == "RUNNING"
            and _number(pause, positive=True) and _number(resume, positive=True)
            and previous < pause < resume
        )
        checks["cycles_transitions"] &= valid
        if valid:
            previous = resume
        for record in (cycle, suspended, resumed):
            if "microvmId" in record:
                checks["cycles_identity"] &= _text(vm_id) and record["microvmId"] == vm_id
    for index, item in enumerate(samples, 1):
        sample = _dict(item)
        if verification_id is None:
            verification_id = sample.get("verification_id")
        checks["cycles_identity"] &= (
            sample.get("runtime_id") == runtime_id and _text(verification_id)
            and sample.get("verification_id") == verification_id
        )
        checks["cycles_features"] &= (
            type(sample.get("verification_schema")) is int and sample["verification_schema"] == 4
            and sample.get("scenario") == "resumed-features"
            and type(sample.get("cycle")) is int and sample["cycle"] == index
            and _summary(sample) and bool(_dict(sample.get("features")))
            and bool(_list(sample.get("feature_spans")))
            and all(isinstance(span, dict) for span in _list(sample.get("feature_spans")))
        )
        checks["cycles_lifecycle"] &= _lifecycle(sample.get("lifecycle"), index, index, 0)
    return {key: bool(check) for key, check in checks.items()}


def evaluate_termination(armed, value, runtime_id, require_forward=True):
    """Bind the final acknowledged root and correlated marker log to its arm."""
    armed, value = _dict(armed), _dict(value)
    checks = evaluate_phase(value, runtime_id, require_forward)
    evidence = _dict(value.get("evidence"))
    marker_log = _dict(evidence.get("marker_log"))
    span = _dict(evidence.get("span"))
    correlation = _dict(evidence.get("log_correlation"))
    checks["termination_armed"] = (
        type(armed.get("verification_schema")) is int and armed["verification_schema"] == 4
        and armed.get("scenario") == "termination-armed" and armed.get("armed") is True
        and armed.get("runtime_id") == runtime_id and _text(runtime_id)
        and armed.get("operation") == DELIVERY_OPERATIONS[3]
        and _text(armed.get("marker")) and armed["marker"] == value.get("marker") == evidence.get("marker")
        and _text(armed.get("verification_id")) and armed["verification_id"] == value.get("verification_id")
        and value.get("scenario") == "termination"
    )
    checks["termination_marker_log"] = (
        marker_log.get("msg") == "tracer_termination_marker"
        and marker_log.get("verification_id") == value.get("verification_id")
        and marker_log.get("runtime_id") == runtime_id
        and marker_log.get("marker") == armed.get("marker")
        and _identity(span) and _pair(marker_log) == _pair(span)
    )
    # The pinned tracer may expose a full 128-bit hex trace ID in log context;
    # its low 64 bits must still identify the v0.4 wire root.
    trace = log_field(correlation, "dd.trace_id", correlation.get("trace_id"))
    log_span = log_field(correlation, "dd.span_id", correlation.get("span_id"))
    trace_matches = False
    if isinstance(trace, str) and len(trace) <= 32 and _identity(span):
        if len(trace) == 32 and all(char in "0123456789abcdefABCDEF" for char in trace):
            trace_matches = str(int(trace, 16) & (2 ** 64 - 1)) == span["trace_id"]
        elif _id(trace):
            trace_matches = trace == span["trace_id"]
    checks["termination_log_correlation"] = trace_matches and log_span == span.get("span_id")
    return {key: bool(check) for key, check in checks.items()}
