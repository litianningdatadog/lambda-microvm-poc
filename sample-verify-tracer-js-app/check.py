"""Check clone features, automatic export, recovery, three resume cycles and final delivery.

Schema2 requires concrete acknowledged requests plus final CloudWatch/Datadog marker logs.
Preserved schema1 inputs retain only their historical three-sample feature coverage.
Use --guest-only for offline guest/CloudWatch evidence, or --cached-evaluation for captured
named backend responses. Datadog credentials must be injected, not saved with results.
"""

import argparse
from datetime import datetime, timezone
import json
from itertools import combinations
import math
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

SERVICE = "sample-verify-tracer-js-app"
SAMPLES = ("1", "2", "resumed-1", "resumed-2", "resumed-3")
HISTORICAL_SAMPLES = ("1", "2", "resumed")
AUTOMATIC_CHECKS = {
    "automatic_default_interval", "automatic_no_manual_flush", "automatic_trace_acknowledged",
    "automatic_identity", "automatic_deadline",
}
RECOVERY_CHECKS = {
    "recovery_both_failures_observed", "recovery_application_responsive",
    "recovery_encoder_bounded", "recovery_queue_drained", "recovery_fresh_delivery",
    "recovery_identity_stable", "recovery_no_manual_flush", "recovery_no_unexpected_errors",
}
TERMINATION_CHECKS = {
    "termination_no_manual_flush", "termination_trace_acknowledged",
    "termination_identity", "termination_deadline", "termination_hook_once",
}
IDENTITIES = ("runtimeId",)
ROOT_CASES = {"manual", "expectedError", "http", "async", "concurrent0", "concurrent1", "concurrent2", "concurrent3"}
REQUIRED_CHECKS = {
    "pinned_version", "microvm_serverless_detected", "runtime_identity_refreshed_before_handler",
    "identity_stable_after_run", "run_called_once", "fresh_runtime_metrics_identity",
    "fresh_custom_metric_identity", "telemetry_disabled_in_serverless",
    "remote_config_disabled_in_serverless", "no_disabled_pipeline_requests",
    "pino_log_correlation", "intake_delivery_succeeded", "feature_roots_exported",
    "feature_root_identity", "feature_root_service", "feature_root_isolation",
    "manual_hierarchy", "manual_tags_and_resources", "manual_scope_restoration",
    "callback_error_exported", "http_incoming_and_outgoing", "http_w3c_propagation",
    "http_runtime_identity", "http_500_and_error_metadata", "http_404_client_error",
    "http_scope_restoration", "async_overlapping_contexts", "async_parent_restoration",
    "concurrent_http_context_isolation", "log_correlation_span_exported",
}


def load(results, name):
    return json.loads((results / name).read_text())


def identities(value):
    return all(isinstance(value.get(name), str) and bool(value[name]) for name in IDENTITIES)


def id_sets(guest):
    traces, spans = set(), set()
    rows = guest.get("evidence", {}).get("ids", [])
    if not rows:
        raise ValueError("Missing generated trace/span IDs")
    for row in rows:
        # dd-trace's public toTraceId()/toSpanId() return decimal strings.
        trace, span = row.get("traceId"), row.get("spanId")
        if not isinstance(trace, str) or not trace.isdecimal() or not isinstance(span, str) or not span.isdecimal():
            raise ValueError("Expected decimal traceId/spanId strings")
        low64, span64 = int(trace) & ((1 << 64) - 1), int(span)
        if not low64 or not 0 < span64 < (1 << 64):
            raise ValueError("Trace low64 and span IDs must be nonzero 64-bit values")
        if low64 in traces:
            raise ValueError("Duplicate trace low64 within one verification sample")
        if span64 in spans:
            raise ValueError("Duplicate span ID within one verification sample")
        traces.add(low64)
        spans.add(span64)
    return traces, spans


def feature_roots(guest):
    features = guest.get("evidence", {}).get("features", {})
    roots = features.get("roots", {})
    if not isinstance(roots, dict) or set(roots) != ROOT_CASES or len(set(roots.values())) != 8:
        raise ValueError("Expected all eight distinct feature root operations")
    if any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]*", name) for name in roots.values()):
        raise ValueError("Invalid feature root operation names")
    if features.get("errorOperation") not in roots.values():
        raise ValueError("Missing exported feature error operation")
    return roots, features["errorOperation"]


def sample_names(deployment):
    schema = deployment.get("schema", 1)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("Unsupported deployment schema")
    return SAMPLES if schema == 2 else HISTORICAL_SAMPLES


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_span_id(value):
    if not isinstance(value, dict):
        return False
    trace, span = value.get("traceId"), value.get("spanId")
    return (isinstance(trace, str) and trace.isdecimal() and 0 < int(trace) < 1 << 128
            and int(trace) & ((1 << 64) - 1) != 0
            and isinstance(span, str) and span.isdecimal() and 0 < int(span) < 1 << 64)


def span_field(attributes, name):
    """Read native dotted keys or their nested Datadog log representation."""
    if isinstance(attributes, dict) and name in attributes:
        return attributes[name]
    for key in name.split("."):
        if not isinstance(attributes, dict):
            return None
        attributes = attributes.get(key)
    return attributes


def exported_span_matches(span, expected, operation, runtime, marker):
    return (valid_span_id(expected) and isinstance(span, dict)
            and str(span.get("trace_id")) == str(int(expected["traceId"]) & ((1 << 64) - 1))
            and str(span.get("span_id")) == expected["spanId"]
            and str(span.get("parent_id")) == "0" and span.get("name") == operation
            and span.get("service") == SERVICE
            and span.get("meta", {}).get("runtime-id") == runtime
            and span_field(span.get("meta"), "verify.delivery.marker") == marker
            and span_field(span.get("metrics"), "_dd.measured") == 1)


def request_matches(request, expected, operation, runtime, marker, accepted=True):
    if not isinstance(request, dict):
        return False
    received, completed = request.get("receivedAt"), request.get("completedAt")
    valid = (type(request.get("sequence")) is int and request["sequence"] > 0
             and finite_number(received) and finite_number(completed) and completed >= received
             and type(request.get("bytes")) is int and request["bytes"] > 0)
    if accepted:
        valid = (valid and request.get("outcome") == "accepted" and request.get("forwarded") is True
                 and type(request.get("status")) is int and 200 <= request["status"] < 300)
    return valid and any(exported_span_matches(span, expected, operation, runtime, marker)
                         for span in request.get("spans", []))


def named_checks(result, names):
    checks = result.get("checks", {})
    return (result.get("schema") == 2 and result.get("passed") is True
            and isinstance(checks, dict) and names.issubset(checks)
            and all(value is True for value in checks.values()))


def manual_unchanged(evidence):
    before, after = evidence.get("manualFlushCountBefore"), evidence.get("manualFlushCountAfter")
    return type(before) is int and before >= 0 and type(after) is int and before == after


def within_deadline(evidence, maximum):
    elapsed, deadline = evidence.get("elapsedMs"), evidence.get("deadlineMs")
    return (finite_number(elapsed) and finite_number(deadline)
            and 0 <= elapsed <= deadline <= maximum and deadline > 0)


def single_delivery_checks(result, scenario, runtime, marker=None):
    evidence = result.get("evidence", {})
    names = AUTOMATIC_CHECKS if scenario == "automatic" else TERMINATION_CHECKS
    expected_marker = evidence.get("marker") if scenario == "automatic" else result.get("marker")
    operation = "verify.js." + scenario
    checks = {
        scenario + "_required_checks": named_checks(result, names) and result.get("scenario") == scenario,
        scenario + "_default_interval": evidence.get("flushIntervalMs") == 2000,
        scenario + "_no_manual_flush": manual_unchanged(evidence),
        scenario + "_identity": evidence.get("runtimeId") == runtime and bool(runtime),
        scenario + "_marker": isinstance(expected_marker, str) and bool(expected_marker)
                             and (marker is None or expected_marker == marker),
        scenario + "_deadline": within_deadline(evidence, 10000),
        scenario + "_trace_acknowledged": evidence.get("forwardToAgent") is True and any(
            request_matches(request, evidence.get("span"), operation, runtime, expected_marker)
            for request in evidence.get("successfulRequests", [])),
        scenario + "_operation": evidence.get("operation") == operation,
    }
    return checks


def recovery_checks(result, runtime):
    evidence = result.get("evidence", {})
    cases = evidence.get("cases", [])
    checks = {
        "recovery_required_checks": named_checks(result, RECOVERY_CHECKS) and result.get("scenario") == "recovery",
        "recovery_identity": evidence.get("runtimeId") == runtime and bool(runtime),
        "recovery_default_interval": evidence.get("flushIntervalMs") == 2000,
        "recovery_no_manual_flush": manual_unchanged(evidence),
        "recovery_forward_to_agent": evidence.get("forwardToAgent") is True,
        "recovery_cases_complete": len(cases) == 2 and {case.get("mode") for case in cases} == {"http503", "disconnect"},
        "recovery_no_unexpected_errors": evidence.get("unexpectedErrors") == [],
    }
    for mode in ("http503", "disconnect"):
        case = next((value for value in cases if value.get("mode") == mode), {})
        prefix, operation = "recovery_" + mode + "_", "verify.js.recovery." + mode
        marker, fresh = case.get("marker"), case.get("freshSpan")
        offered, attempts = case.get("offeredSpans", []), case.get("failureAttempts", [])
        generated = case.get("generated")
        fault_at, restored_at = case.get("faultActiveAt"), case.get("restoredAt")
        timeline = finite_number(fault_at) and finite_number(restored_at) and restored_at > fault_at
        checks[prefix + "workload"] = (
            type(generated) is int and 0 < generated <= 1024 and len(offered) == generated
            and all(valid_span_id(value) for value in offered) and valid_span_id(fresh)
            and len({(value["traceId"], value["spanId"]) for value in offered if valid_span_id(value)}) == generated
            and fresh not in offered)
        checks[prefix + "marker"] = isinstance(marker, str) and bool(marker)
        fresh_marker = case.get("freshMarker")
        checks[prefix + "fresh_marker"] = isinstance(fresh_marker, str) and bool(fresh_marker) and fresh_marker != marker
        checks[prefix + "no_unexpected_errors"] = case.get("unexpectedErrors") == []
        checks[prefix + "operation"] = case.get("operation") == operation
        checks[prefix + "failure_observed"] = timeline and any(
            request.get("outcome") == "injected-" + ("http503" if mode == "http503" else "disconnect")
            and request.get("forwarded") is False
            and request.get("status") == (503 if mode == "http503" else None)
            and fault_at <= request.get("receivedAt", -1) <= request.get("completedAt", -1) <= restored_at
            and any(request_matches(request, value, operation, runtime, marker, accepted=False) for value in offered)
            for request in attempts)
        probes = case.get("probes", [])
        checks[prefix + "outage_health"] = timeline and len(probes) >= 3 and all(
            probe.get("status") == 200 and probe.get("runtimeId") == runtime
            and finite_number(probe.get("elapsedMs")) and 0 <= probe["elapsedMs"] < 1000
            and finite_number(probe.get("at")) and fault_at <= probe["at"]
            and probe["at"] + probe["elapsedMs"] <= restored_at for probe in probes)
        counts = case.get("encoderCounts", [])
        valid_counts = bool(counts) and all(
            isinstance(row, dict) and finite_number(row.get("at"))
            and type(row.get("count")) is int and row["count"] >= 0 for row in counts)
        checks[prefix + "encoder_bounded"] = (
            valid_counts and type(generated) is int and generated > 0
            and case.get("baselineEncoderCount") == 0
            and type(case.get("encoderPeak")) is int
            and case["encoderPeak"] == max(row["count"] for row in counts)
            and case["encoderPeak"] <= generated + 1
            and counts[0]["count"] == 0 and timeline and counts[0]["at"] <= fault_at
            and all(a["at"] <= b["at"] for a, b in zip(counts, counts[1:])))
        checks[prefix + "queue_drained"] = (valid_counts and case.get("queueDrained") is True
                                           and counts[-1]["count"] == 0 and timeline
                                           and counts[-1]["at"] >= restored_at)
        checks[prefix + "fresh_acknowledged"] = timeline and any(
            request_matches(request, fresh, operation, runtime, fresh_marker)
            and finite_number(case.get("freshCreatedAt")) and case["freshCreatedAt"] >= restored_at
            and request["receivedAt"] >= case["freshCreatedAt"] for request in case.get("freshAcknowledgements", []))
        checks[prefix + "deadline"] = within_deadline(case, 20000)
    markers = [case.get("marker") for case in cases]
    checks["recovery_distinct_markers"] = len(markers) == 2 and len(set(markers)) == 2
    checks["recovery_total_deadline"] = all(
        finite_number(case.get("elapsedMs")) for case in cases) and sum(case.get("elapsedMs", 0) for case in cases) < 90000
    return checks




def evaluate_guest(results):
    deployment = load(results, "deployment.json")
    samples = sample_names(deployment)
    schema = 2 if samples == SAMPLES else 1
    guests = {sample: load(results, f"verification-{sample}.json") for sample in samples}
    vms = {number: load(results, f"vm{number}.json") for number in (1, 2)}
    checks = {"different_microvms": bool(vms[1].get("microvmId")) and vms[1]["microvmId"] != vms[2].get("microvmId")}
    for number, vm in vms.items():
        checks[f"clone{number}_same_image_version"] = (
            vm.get("imageArn") == deployment["imageArn"] and vm.get("imageVersion") == deployment["imageVersion"]
        )
    for sample, guest in guests.items():
        if not isinstance(guest, dict):
            raise ValueError(f"Missing verification result for sample {sample}")
        prefix = f"guest_{sample}_"
        evidence, state = guest.get("evidence", {}), guest.get("state", {})
        before, run, after = (evidence.get(name, {}) for name in ("before", "run", "after"))
        checks[prefix + "schema_version"] = guest.get("schema") == schema and guest.get("version") == "6.15.0"
        checks[prefix + "passed"] = (guest.get("passed") is True and bool(guest.get("checks"))
                                      and all(value is True for value in guest["checks"].values()))
        checks[prefix + "required_checks_present"] = REQUIRED_CHECKS.issubset(guest.get("checks", {}))
        for required in REQUIRED_CHECKS:
            checks[prefix + required] = guest.get("checks", {}).get(required) is True
        for name, value in guest.get("checks", {}).items():
            checks[prefix + name] = value is True
        if schema == 2:
            checks[prefix + "integer_lifecycle_counters"] = all(
                type(state.get(name)) is int and state[name] >= 0 for name in (
                    "runCount", "suspendCount", "resumeCount", "terminateCount", "verificationCount"))
            checks[prefix + "not_terminated_before_cleanup"] = state.get("terminateCount") == 0
        checks[prefix + "identities_present"] = all(identities(row) for row in (before, run, after, state))
        checks[prefix + "native_run_refreshed_identities"] = all(
            isinstance(before.get(name), str) and isinstance(run.get(name), str) and before[name] != run[name]
            for name in IDENTITIES
        )
        checks[prefix + "identity_stable_after_run"] = all(
            run.get(name) == after.get(name) == state.get(name) for name in IDENTITIES
        )
        # Stock 6.15.0 forces RC and telemetry off for serverless/MicroVM runtimes.
        checks[prefix + "rc_client_absent"] = all(
            row.get("rcClientId") is None for row in (before, run, after, state)
        )
        for disabled_check in (
            "telemetry_disabled_in_serverless", "remote_config_disabled_in_serverless",
            "no_disabled_pipeline_requests",
        ):
            checks[prefix + disabled_check] = guest.get("checks", {}).get(disabled_check) is True
        checks[prefix + "run_exactly_once"] = state.get("runCount") == 1
        checks[prefix + "prepared"] = state.get("prepared") is True
        checks[prefix + "system_openssl"] = state.get("bundledOpenSSL") is False
        roots, error_operation = feature_roots(guest)
        root_spans = evidence.get("features", {}).get("rootSpans", [])
        checks[prefix + "eight_feature_root_spans"] = (
            len(root_spans) == 8 and {root.get("name") for root in root_spans} == set(roots.values())
        )
        checks[prefix + "generated_id_sample_complete"] = len(evidence.get("ids", [])) == (8 if sample.startswith("resumed") else 136)
        spans = evidence.get("spans", [])
        checks[prefix + "exported_features_present"] = bool(spans) and all(
            any(span.get("name") == name and span.get("service") == SERVICE for span in spans)
            for name in roots.values()
        )
        checks[prefix + "exported_error_present"] = any(
            span.get("name") == error_operation and span.get("error") == 1 for span in spans
        )
    first, second = guests["1"], guests["2"]
    resumed_samples = samples[2:]
    checks["shared_snapshot_before_identity"] = all(
        first["evidence"]["before"].get(name) == second["evidence"]["before"].get(name)
        for name in IDENTITIES
    )
    for name in IDENTITIES:
        checks["clone_unique_" + name] = first["evidence"]["run"].get(name) != second["evidence"]["run"].get(name)
        checks["resume_stable_" + name] = all(
            first["evidence"]["after"].get(name) == guests[sample]["evidence"]["run"].get(name)
            == guests[sample]["evidence"]["after"].get(name) for sample in resumed_samples
        )
    sets = {sample: id_sets(guest) for sample, guest in guests.items()}
    for a, b in combinations(samples, 2):
        checks[f"unique_trace_low64_{a}_{b}"] = sets[a][0].isdisjoint(sets[b][0])
        checks[f"unique_span_ids_{a}_{b}"] = sets[a][1].isdisjoint(sets[b][1])
    checks["consistent_feature_roots"] = all(feature_roots(first) == feature_roots(guest) for guest in guests.values())
    if schema == 1:
        checks["aws_suspended_state_observed"] = vms[1].get("suspended", {}).get("state") == "SUSPENDED"
        checks["aws_resumed_state_observed"] = vms[1].get("resumed", {}).get("state") == "RUNNING"
    else:
        cycles = vms[1].get("cycles", [])
        checks["aws_three_cycles_complete"] = (
            len(cycles) == 3 and [cycle.get("cycle") for cycle in cycles] == [1, 2, 3]
            and not vms[2].get("cycles"))
        previous = None
        for number in range(1, 4):
            cycle = next((value for value in cycles if value.get("cycle") == number), {})
            suspended, resumed_state = cycle.get("suspended", {}), cycle.get("resumed", {})
            times = suspended.get("time"), resumed_state.get("time")
            checks[f"aws_cycle_{number}_states_observed"] = (
                suspended.get("state") == "SUSPENDED" and resumed_state.get("state") == "RUNNING"
                and all(finite_number(value) for value in times) and times[0] <= times[1]
                and (previous is None or previous <= times[0]))
            previous = times[1]
        automatic = load(results, "delivery-automatic.json")
        recovery = load(results, "delivery-recovery.json")
        checks.update(single_delivery_checks(automatic, "automatic", first["state"]["runtimeId"]))
        checks.update(recovery_checks(recovery, first["state"]["runtimeId"]))
        checks["delivery_manual_counter_continuity"] = (
            automatic.get("evidence", {}).get("manualFlushCountAfter")
            == recovery.get("evidence", {}).get("manualFlushCountBefore"))
    checks["no_suspend_before_initial_verification"] = all(
        guests[sample]["state"].get(name) == 0 for sample in ("1", "2") for name in ("suspendCount", "resumeCount")
    )
    checks["resume_hooks_observed"] = all(
        guests[sample]["state"].get(name) == number
        for number, sample in enumerate(resumed_samples, 1) for name in ("suspendCount", "resumeCount"))
    if schema == 2:
        checks["verification_counts_exact"] = all(
            guests[sample]["state"].get("verificationCount") == number + 1
            for number, sample in enumerate(resumed_samples, 1)
        ) and all(guests[sample]["state"].get("verificationCount") == 1 for sample in ("1", "2"))
    checks["resume_preserved_snapshot_identity"] = all(
        first["evidence"]["before"] == guests[sample]["evidence"]["before"] for sample in resumed_samples)
    return checks


def dd(path, params=None, body=None):
    base = os.environ.get("DD_API_URL", "https://api." + os.environ.get("DD_SITE", "datadoghq.com"))
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("Datadog API requests require HTTPS")
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers={
        "DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if result.get("error") or result.get("errors"):
        raise RuntimeError(f"Datadog rejected request: {result.get('error') or result.get('errors')}")
    return result


def log_matches(row, guest, version):
    result = row.get("result", {})
    if not isinstance(result, dict):
        return False
    guest_checks = guest.get("checks", {})
    if (not isinstance(guest_checks, dict) or not REQUIRED_CHECKS.issubset(guest_checks)
            or not all(value is True for value in guest_checks.values())
            or result.get("checks") != guest_checks):
        return False
    disabled_rc = guest_checks.get("remote_config_disabled_in_serverless") is True

    def identity(value):
        if not isinstance(value, dict) or not identities(value) or value.get("rcClientId") is not None:
            raise ValueError("Missing identity or unexpected RC client")
        # Only these known identity objects may lose their null RC field in the log pipeline.
        return {key: item for key, item in value.items()
                if not (disabled_rc and key == "rcClientId" and item is None)}

    def state(value):
        normalized = identity(value)
        for counter in ("runCount", "resumeCount", "suspendCount", "terminateCount", "verificationCount"):
            if type(value.get(counter)) is not int:
                raise ValueError("Missing verification counter")
        for phase in ("before", "run"):
            normalized[phase] = identity(value.get(phase))
        return normalized

    try:
        return (row.get("msg") == "microvm_js_verification" and row.get("version") == version
                and result.get("schema") == guest.get("schema")
                and result.get("passed") is True
                and state(result.get("state")) == state(guest.get("state"))
                and all(identity(result.get("evidence", {}).get(phase)) == identity(guest["evidence"][phase])
                        for phase in ("before", "run", "after")))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def normalized_termination_state(value):
    if not isinstance(value, dict) or not identities(value) or value.get("rcClientId") is not None:
        raise ValueError("Invalid termination identity")
    for counter in ("runCount", "resumeCount", "suspendCount", "terminateCount", "verificationCount"):
        if type(value.get(counter)) is not int:
            raise ValueError("Invalid termination counter")
    normalized = {key: item for key, item in value.items() if key != "rcClientId"}
    for phase in ("before", "run"):
        identity = value.get(phase)
        if not isinstance(identity, dict) or not identities(identity) or identity.get("rcClientId") is not None:
            raise ValueError("Invalid termination phase identity")
        normalized[phase] = {key: item for key, item in identity.items() if key != "rcClientId"}
    return normalized


def termination_result_matches(row, arm, guest, version):
    try:
        result = row["result"]
        checks = single_delivery_checks(result, "termination", arm["runtimeId"], arm["marker"])
        expected = {**guest["state"], "terminateCount": 1}
        return (row.get("msg") == "microvm_js_termination_result" and row.get("version") == version
                and guest["checks"].get("remote_config_disabled_in_serverless") is True
                and all(checks.values())
                and normalized_termination_state(result["state"]) == normalized_termination_state(expected))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def termination_log_matches(row, result, arm, version):
    span = result.get("evidence", {}).get("span", {})
    return (row.get("msg") == "microvm_js_termination_log" and row.get("version") == version
            and row.get("marker") == arm.get("marker") and row.get("runtimeId") == arm.get("runtimeId")
            and valid_span_id(span) and row.get("traceId") == span["traceId"]
            and row.get("spanId") == span["spanId"])


def final_delivery_checks(rows, arm, guest, version, prefix):
    checks = {
        prefix + "termination_armed": (
            arm.get("schema") == 2 and arm.get("scenario") == "termination-armed"
            and arm.get("armed") is True and isinstance(arm.get("marker"), str) and bool(arm["marker"])
            and arm.get("runtimeId") == guest["state"]["runtimeId"]
            and arm.get("operation") == "verify.js.termination"),
    }
    matching = [row["result"] for row in rows if termination_result_matches(row, arm, guest, version)]
    checks[prefix + "termination_result_matches"] = bool(matching)
    checks[prefix + "termination_marker_log_matches"] = any(
        termination_log_matches(row, result, arm, version) for row in rows for result in matching)
    return checks



def cached_output(results, output, source, guest_only=False):
    if guest_only:
        raise ValueError("--cached-evaluation cannot be combined with --guest-only")
    if output is None:
        raise ValueError("--cached-evaluation requires an explicit --output")
    if output.resolve() in (source.resolve(), (results / "evaluation.json").resolve()):
        raise ValueError("Cached evaluation output must not overwrite its source or original evaluation.json")
    if output.exists() and (output.samefile(source) if source.exists() else False):
        raise ValueError("Cached evaluation output must not overwrite its source")
    original = results / "evaluation.json"
    if output.exists() and original.exists() and output.samefile(original):
        raise ValueError("Cached evaluation output must not overwrite original evaluation.json")


def evaluate(results, output=None, guest_only=False, cached_evaluation=None):
    cached = None
    if cached_evaluation is not None:
        cached_output(results, output, cached_evaluation, guest_only)
        cached = json.loads(cached_evaluation.read_text())
        if not isinstance(cached, dict) or not isinstance(cached.get("evidence"), dict):
            raise ValueError("Cached evaluation is missing backend evidence")
    checks = evaluate_guest(results)
    deployment = load(results, "deployment.json")
    samples = sample_names(deployment)
    schema = 2 if samples == SAMPLES else 1
    guests = {sample: load(results, f"verification-{sample}.json") for sample in samples}
    cloudwatch = load(results, "cloudwatch.json")
    rows = []
    for event in cloudwatch.get("events", []):
        try:
            row = json.loads(event["message"])
            if isinstance(row, dict):
                rows.append((event.get("logStreamName", ""), row))
        except (ValueError, KeyError):
            continue
    for sample, guest in guests.items():
        vm = load(results, "vm2.json" if sample == "2" else "vm1.json")
        checks[f"cloudwatch_{sample}_verification_matches"] = any(
            vm["microvmId"] in stream and log_matches(row, guest, deployment["version"]) for stream, row in rows
        )
        checks[f"clone_{sample}_terminated"] = vm.get("terminated") is True
    if schema == 2:
        arm = load(results, "termination-armed.json")
        first_vm = load(results, "vm1.json")
        checks.update(final_delivery_checks(
            [row for stream, row in rows if first_vm["microvmId"] in stream],
            arm, guests["resumed-3"], deployment["version"], "cloudwatch_"))
        cloudwatch_termination_results = [
            row["result"] for stream, row in rows if first_vm["microvmId"] in stream
            and termination_result_matches(row, arm, guests["resumed-3"], deployment["version"])]
    evidence = {}
    if not guest_only:
        vms = [load(results, f"vm{number}.json") for number in (1, 2)]
        start = int(deployment["start"]) - 60
        end = int(max(vm.get("terminatedAt", vm.get("end", vm["start"])) for vm in vms)) + 120
        tags = f"service:{SERVICE},env:devmicrovm,version:{deployment['version'].lower()}"

        def backend(name, path, params=None, body=None):
            if cached is None:
                response = dd(path, params, body)
            else:
                response = cached["evidence"].get(name)
                if not isinstance(response, dict):
                    raise ValueError(f"Cached evaluation is missing backend payload: {name}")
                required = "data" if name == "logs" else "series"
                if required not in response:
                    raise ValueError(f"Cached evaluation is missing {required} in backend payload: {name}")
            evidence[name] = response
            return response

        def metric(name, expression):
            response = backend(name, "/api/v1/query", {"from": start, "to": end, "query": expression})
            return [value for series in response.get("series") or []
                    for _, value in series.get("pointlist", []) if value is not None]

        roots, error_operation = feature_roots(guests["1"])
        # Derive metric operation names from the evidence actually exported to the agent.
        for case, operation in roots.items():
            values = metric("feature_" + case, f"sum:trace.{operation}.hits{{{tags}}}.as_count()")
            checks["datadog_feature_" + case + "_hits"] = bool(values) and sum(values) >= len(samples)
        errors = metric("feature_errors", f"sum:trace.{error_operation}.errors{{{tags}}}.as_count()")
        checks["datadog_feature_errors"] = bool(errors) and sum(errors) >= len(samples)
        if schema == 2:
            for name, operation in (
                ("automatic", "verify.js.automatic"), ("recovery_http503", "verify.js.recovery.http503"),
                ("recovery_disconnect", "verify.js.recovery.disconnect"), ("termination", "verify.js.termination"),
            ):
                values = metric("delivery_" + name, f"sum:trace.{operation}.hits{{{tags}}}.as_count()")
                checks["datadog_delivery_" + name + "_hits"] = bool(values) and sum(values) >= 1
        messages = ('("microvm_js_verification" OR "microvm_js_termination_log" OR "microvm_js_termination_result")'
                    if schema == 2 else '"microvm_js_verification"')
        query = f'service:{SERVICE} version:{deployment["version"].lower()} {messages}'
        body = {"filter": {"query": query,
                           "from": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                           "to": datetime.fromtimestamp(end, timezone.utc).isoformat()}, "page": {"limit": 1000}}
        logs = backend("logs", "/api/v2/logs/events/search", body=body)
        attrs = []
        for row in logs.get("data", []):
            outer = row.get("attributes", {})
            structured = dict(outer.get("attributes", {}))
            # Datadog's standard log pipeline may remap Pino msg to message.
            structured.setdefault("msg", outer.get("message"))
            attrs.append(structured)
        for sample, guest in guests.items():
            checks[f"datadog_{sample}_verification_log_matches"] = any(
                log_matches(row, guest, deployment["version"]) for row in attrs
            )
        if schema == 2:
            checks.update(final_delivery_checks(attrs, arm, guests["resumed-3"], deployment["version"], "datadog_"))
            checks["datadog_termination_span_matches_cloudwatch"] = any(
                termination_result_matches(row, arm, guests["resumed-3"], deployment["version"])
                and row["result"]["evidence"]["span"] == result["evidence"]["span"]
                for row in attrs for result in cloudwatch_termination_results)
    mode = "guest-and-cloudwatch-only" if guest_only else "aws-and-datadog"
    if cached is not None:
        mode = "aws-and-cached-datadog"
        evidence = cached["evidence"]
    result = {"schema": schema, "passed": all(checks.values()), "mode": mode, "checks": checks, "evidence": evidence,
              "coverage": {"schema": schema, "samples": list(samples), "historical": schema == 1,
                           "notCovered": (["automatic_export", "final_termination_batch",
                                           "repeated_suspend_resume", "intake_failure_recovery"] if schema == 1 else [])}}
    if cached is not None:
        result["provenance"] = {"cachedEvaluation": str(cached_evaluation.resolve()),
                                "sourceMode": cached.get("mode")}
    (output or results / "evaluation.json").write_text(json.dumps(result, indent=2) + "\n")
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(f"Evaluation ({result['mode']}): {'PASS' if result['passed'] else 'FAIL'} ({sum(checks.values())}/{len(checks)})")
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, help="Save re-evaluation separately")
    parser.add_argument("--guest-only", action="store_true", help="Offline real-AWS guest/CloudWatch evidence only; no backend claim")
    parser.add_argument("--cached-evaluation", type=Path, help="Reuse captured Datadog responses without network access")
    args = parser.parse_args()
    if args.cached_evaluation is not None:
        try:
            cached_output(args.results, args.output, args.cached_evaluation, args.guest_only)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    try:
        return evaluate(args.results, args.output, args.guest_only, args.cached_evaluation)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        (args.output or args.results / "evaluation.json").write_text(json.dumps({"passed": False, "error": str(error)}, indent=2) + "\n")
        print(f"FAIL evaluation: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
