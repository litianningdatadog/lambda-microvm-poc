"""Check real snapshot clones/resume, CloudWatch logs, and Datadog APM/log arrival.

Use --guest-only for offline guest evidence, or --cached-evaluation for captured backend evidence.
Datadog credentials should be injected with dd-auth, not saved in the results directory.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

SERVICE = "sample-verify-tracer-js-app"
SAMPLES = ("1", "2", "resumed")
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


def evaluate_guest(results):
    deployment = load(results, "deployment.json")
    guests = {sample: load(results, f"verification-{sample}.json") for sample in SAMPLES}
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
        checks[prefix + "schema_version"] = guest.get("schema") == 1 and guest.get("version") == "6.15.0"
        checks[prefix + "passed"] = (guest.get("passed") is True and bool(guest.get("checks"))
                                      and all(value is True for value in guest["checks"].values()))
        checks[prefix + "required_checks_present"] = REQUIRED_CHECKS.issubset(guest.get("checks", {}))
        for required in REQUIRED_CHECKS:
            checks[prefix + required] = guest.get("checks", {}).get(required) is True
        for name, value in guest.get("checks", {}).items():
            checks[prefix + name] = value is True
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
        checks[prefix + "generated_id_sample_complete"] = len(evidence.get("ids", [])) == (8 if sample == "resumed" else 136)
        spans = evidence.get("spans", [])
        checks[prefix + "exported_features_present"] = bool(spans) and all(
            any(span.get("name") == name and span.get("service") == SERVICE for span in spans)
            for name in roots.values()
        )
        checks[prefix + "exported_error_present"] = any(
            span.get("name") == error_operation and span.get("error") == 1 for span in spans
        )
    first, second, resumed = (guests[sample] for sample in SAMPLES)
    checks["shared_snapshot_before_identity"] = all(
        first["evidence"]["before"].get(name) == second["evidence"]["before"].get(name)
        for name in IDENTITIES
    )
    for name in IDENTITIES:
        checks["clone_unique_" + name] = first["evidence"]["run"].get(name) != second["evidence"]["run"].get(name)
        checks["resume_stable_" + name] = (
            first["evidence"]["after"].get(name) == resumed["evidence"]["run"].get(name)
            == resumed["evidence"]["after"].get(name)
        )
    sets = {sample: id_sets(guest) for sample, guest in guests.items()}
    for a, b in (("1", "2"), ("1", "resumed"), ("2", "resumed")):
        checks[f"unique_trace_low64_{a}_{b}"] = sets[a][0].isdisjoint(sets[b][0])
        checks[f"unique_span_ids_{a}_{b}"] = sets[a][1].isdisjoint(sets[b][1])
    checks["consistent_feature_roots"] = feature_roots(first) == feature_roots(second) == feature_roots(resumed)
    checks["aws_suspended_state_observed"] = vms[1].get("suspended", {}).get("state") == "SUSPENDED"
    checks["aws_resumed_state_observed"] = vms[1].get("resumed", {}).get("state") == "RUNNING"
    checks["no_suspend_before_initial_verification"] = all(
        guests[sample]["state"].get(name) == 0 for sample in ("1", "2") for name in ("suspendCount", "resumeCount")
    )
    checks["resume_hooks_observed"] = all(resumed["state"].get(name) == 1 for name in ("suspendCount", "resumeCount"))
    checks["resume_preserved_snapshot_identity"] = first["evidence"]["before"] == resumed["evidence"]["before"]
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
                and result.get("passed") is True
                and state(result.get("state")) == state(guest.get("state"))
                and all(identity(result.get("evidence", {}).get(phase)) == identity(guest["evidence"][phase])
                        for phase in ("before", "run", "after")))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


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
    guests = {sample: load(results, f"verification-{sample}.json") for sample in SAMPLES}
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
            checks["datadog_feature_" + case + "_hits"] = bool(values) and sum(values) >= len(SAMPLES)
        errors = metric("feature_errors", f"sum:trace.{error_operation}.errors{{{tags}}}.as_count()")
        checks["datadog_feature_errors"] = bool(errors) and sum(errors) >= len(SAMPLES)
        query = f'service:{SERVICE} version:{deployment["version"].lower()} "microvm_js_verification"'
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
    mode = "guest-and-cloudwatch-only" if guest_only else "aws-and-datadog"
    if cached is not None:
        mode = "aws-and-cached-datadog"
        evidence = cached["evidence"]
    result = {"passed": all(checks.values()), "mode": mode, "checks": checks, "evidence": evidence}
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
