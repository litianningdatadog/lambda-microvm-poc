"""Evaluate guest evidence and Datadog arrival, with credentials supplied by dd-auth."""

import argparse
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from delivery_checks import DELIVERY_OPERATIONS
from delivery_checks import evaluate_cycles
from delivery_checks import evaluate_phase
from delivery_checks import evaluate_termination
from delivery_checks import log_values_equal
from feature_checks import FEATURE_OPERATIONS
from feature_checks import evaluate_features
from lifecycle_checks import required_telemetry_checks
from retirement_checks import evaluate_retirements


RSS_CHECK = "datadog_runtime_metrics_new_identity"
RSS_SKIP_REASON = (
    "Temporarily disabled: the standard RSS name query misses stored aggregates in org 447397. "
    "Guest RSS identity checks remain required; use --check-runtime-metrics to restore the backend assertion."
)


def dd(path, params=None, body=None):
    base = os.environ.get("DD_API_URL", "https://api." + os.environ.get("DD_SITE", "datadoghq.com"))
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("Datadog API requests require HTTPS")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "DD-API-KEY": os.environ["DD_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
        return json.load(response)


def _restore_backend_empty_errors(row, reference):
    """Restore only known error arrays omitted from otherwise matching backend logs."""
    for section, field in (("features", "errors"), ("evidence", "unexpected_errors")):
        actual = row.get(section)
        expected = reference.get(section)
        if (isinstance(actual, dict) and isinstance(expected, dict)
                and field not in actual and expected.get(field) == []):
            row = {**row, section: {**actual, field: []}}
    return row


def evaluate(results, output=None, check_runtime_metrics=False):
    deployment = json.loads((results / "deployment.json").read_text())
    vm = json.loads((results / "vm.json").read_text())
    guest = json.loads((results / "verification.json").read_text())
    checks = {"guest_" + name: value is True for name, value in guest["checks"].items()}
    checks["guest_passed"] = bool(guest["checks"]) and guest.get("passed") is True
    checks["wheel_matches_deployment"] = guest["wheel_sha256"] == deployment["wheel_sha256"]
    checks["version_matches_deployment"] = guest["verification_id"] == deployment["verification_id"]
    start, end = int(deployment["start"]) - 60, int(vm.get("end", vm["start"])) + 120
    # Datadog normalizes tag values, while the structured log's verification ID
    # retains the original case of the results directory name.
    tags = "service:sample-verify-tracer-app,version:" + deployment["verification_id"].lower()
    evidence = {}
    diagnostics = {}
    skipped = {}
    schema = deployment.get("verification_schema", 1)
    if schema >= 5:
        retirement_checks = evaluate_retirements(guest.get("retirements"))
        checks.update({"retirement_" + name: ok for name, ok in retirement_checks.items()})
        checks["guest_retirement_scenarios_present"] = (
            guest.get("verification_schema") == schema and retirement_checks.keys() <= guest["checks"].keys()
        )
    else:
        skipped["retirement_failure_scenarios"] = (
            "Historical schema predates #21429 cleanup-error controls; deploy again for schema 5 coverage."
        )
    samples = []
    armed = {}
    if schema >= 4:
        def load_phase(name):
            path = results / name
            if not path.exists():
                return {}
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}

        deliveries = {name: load_phase(f"delivery-{name}.json") for name in ("automatic", "recovery")}
        for name, value in deliveries.items():
            checks.update({f"{name}_{key}": ok for key, ok in evaluate_phase(value, guest["runtime_id"]).items()})
            checks[f"{name}_version_matches"] = value.get("verification_id") == deployment["verification_id"]
        checks["delivery_manual_counter_continuity"] = (
            deliveries["automatic"].get("evidence", {}).get("manual_flush_count_after")
            == deliveries["recovery"].get("evidence", {}).get("manual_flush_count_before")
        )
        for cycle in range(1, 4):
            sample = load_phase(f"verification-resumed-{cycle}.json")
            samples.append(sample)
            feature_checks = evaluate_features(sample.get("feature_spans", []), sample.get("features", {}), guest["runtime_id"])
            checks[f"resumed_{cycle}_features"] = all(feature_checks.values())
            checks[f"resumed_{cycle}_version_matches"] = sample.get("verification_id") == deployment["verification_id"]
            value = load_phase(f"automatic-resumed-{cycle}.json")
            checks.update({
                f"resumed_{cycle}_{key}": ok
                for key, ok in evaluate_phase(value, guest["runtime_id"]).items()
            })
            checks[f"resumed_{cycle}_automatic_version_matches"] = value.get("verification_id") == deployment["verification_id"]
        checks.update(evaluate_cycles(vm, samples, guest["runtime_id"]))
        root_ids = [
            (root.get("trace_id"), root.get("span_id"))
            for sample in [guest, *samples]
            for root in sample.get("features", {}).get("roots", {}).values()
        ]
        checks["feature_samples_have_distinct_roots"] = (
            len(root_ids) == 4 * len(FEATURE_OPERATIONS) and len(set(root_ids)) == len(root_ids)
        )
        checks["initial_feature_payload_valid"] = all(
            evaluate_features(guest.get("feature_spans", []), guest.get("features", {}), guest["runtime_id"]).values()
        )
        checks["microvm_terminated"] = vm.get("terminated") is True
        armed = load_phase("termination-armed.json")
        checks["termination_armed_version_matches"] = armed.get("verification_id") == deployment["verification_id"]
    else:
        skipped["delivery_scenarios"] = (
            "Historical schema predates automatic export, intake recovery, repeated suspend/resume, "
            "and final termination-batch verification; deploy again for schema 4 coverage."
        )
    if schema >= 2:
        checks["guest_telemetry_scenarios_present"] = guest.get(
            "verification_schema"
        ) == schema and required_telemetry_checks(schema).issubset(guest["checks"])
    if schema < 3:
        skipped["strengthened_telemetry_scenarios"] = (
            "This deployment predates schema 3 subscriber-membership and native exporter telemetry checks."
        )

    def query(name, expression):
        response = dd("/api/v1/query", {"from": start, "to": end, "query": expression})
        if response.get("error") or response.get("errors"):
            raise RuntimeError(f"Datadog rejected {name}: {response.get('error') or response.get('errors')}")
        evidence[name] = response
        return [
            value
            for series in response.get("series") or []
            for _, value in series.get("pointlist", [])
            if value is not None
        ]

    fresh = query("fresh_trace", f"sum:trace.verify.fresh.hits{{{tags}}}.as_count()")
    checks["datadog_fresh_trace_arrived"] = sum(fresh) >= 1
    for name in ("stale", "stale_child", "buffered"):
        values = query(name, f"sum:trace.verify.{name}.hits{{{tags}}}.as_count()")
        checks["datadog_" + name + "_absent"] = sum(values) == 0
    if deployment.get("verification_schema", 1) >= 2:
        expected_checks = set(evaluate_features([], {}, guest["runtime_id"]))
        if schema < 4:
            expected_checks.discard("feature_http_single_instrumentation")
        checks["guest_feature_scenarios_present"] = (
            guest.get("verification_schema") == deployment["verification_schema"]
            and expected_checks.issubset(guest["checks"])
            and set(FEATURE_OPERATIONS).issubset(guest.get("features", {}).get("roots", {}))
        )
        for case, operation in FEATURE_OPERATIONS.items():
            values = query("feature_trace_" + case, f"sum:trace.{operation}.hits{{{tags}}}.as_count()")
            checks["datadog_feature_" + case + "_arrived"] = sum(values) >= (4 if schema >= 4 else 1)
        errors = query("feature_errors", f"sum:trace.{FEATURE_OPERATIONS['error']}.errors{{{tags}}}.as_count()")
        checks["datadog_feature_error_reported"] = sum(errors) >= (4 if schema >= 4 else 1)
    else:
        skipped["common_feature_scenarios"] = (
            "This deployment predates the expanded feature suite; deploy again to run it."
        )
    if schema >= 4:
        for operation in DELIVERY_OPERATIONS:
            values = query(operation, f"sum:trace.{operation}.hits{{{tags}}}.as_count()")
            checks["datadog_" + operation] = sum(values) >= (4 if operation.endswith(".automatic") else 1)
    if check_runtime_metrics:
        metrics = query("runtime_metrics", f"avg:runtime.python.mem.rss{{{tags},runtime-id:{guest['runtime_id']}}}")
        checks[RSS_CHECK] = bool(metrics) and max(metrics) > 0
    else:
        skipped[RSS_CHECK] = RSS_SKIP_REASON
    # Diagnostic copies do not satisfy the standard metric assertion when it is enabled.
    alias = deployment.get("runtime_rss_alias")
    if alias:
        control = query("runtime_metrics_control", f"avg:{alias}{{{tags},runtime-id:{guest['runtime_id']}}}")
        diagnostics["runtime_rss_alias_arrived"] = bool(control) and max(control) > 0
    if not checks.get(RSS_CHECK, False):
        diagnostics["guest_rss_packets"] = guest.get("runtime_rss_packets", [])
        if check_runtime_metrics:
            try:
                indexed_tags = dd("/api/v2/metrics/runtime.python.mem.rss/all-tags")
                evidence["runtime_metric_tags"] = indexed_tags
                diagnostics["runtime_id_present_in_metric_tag_inventory"] = (
                    f"runtime-id:{guest['runtime_id']}"
                    in indexed_tags.get("data", {}).get("attributes", {}).get("tags", [])
                )
            except (OSError, ValueError) as error:
                diagnostics["runtime_metric_tag_inventory_error"] = str(error)
            if diagnostics.get("runtime_rss_alias_arrived"):
                diagnostics["runtime_metric_issue"] = "Diagnostic copy arrived; standard metric name is not queryable"
    log_response = dd(
        "/api/v2/logs/events/search",
        body={
            "filter": {
                "query": (
                    f"service:sample-verify-tracer-app @verification_id:{deployment['verification_id']} "
                    + (
                        '("tracer_verification" OR "tracer_resumed_features" OR '
                        '"tracer_termination_marker" OR "tracer_termination_result")'
                        if schema >= 4 else '"tracer_verification"'
                    )
                ),
                "from": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "to": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            },
            "page": {"limit": 1000},
        },
    )
    evidence["logs"] = log_response
    attrs = [row.get("attributes", {}).get("attributes", {}) for row in log_response.get("data", [])]
    checks["datadog_verification_log_arrived"] = any(
        a.get("runtime_id") == guest["runtime_id"] and a.get("checks") == guest["checks"] for a in attrs
    )
    cloudwatch = json.loads((results / "cloudwatch.json").read_text())
    printed = []
    for event in cloudwatch.get("events", []):
        if not event.get("logStreamName", "").endswith(vm["microvmId"]):
            continue
        try:
            printed.append(json.loads(event["message"]))
        except ValueError:
            continue
    checks["cloudwatch_verification_matches"] = any(
        row.get("verification_id") == deployment["verification_id"] and row.get("checks") == guest["checks"]
        for row in printed
    )
    if schema >= 4:
        backend_rows = [
            {"msg": row.get("attributes", {}).get("message"), **row.get("attributes", {}).get("attributes", {})}
            for row in log_response.get("data", [])
        ]
        for cycle, sample in enumerate(samples, 1):
            def matches_sample(row):
                return all(log_values_equal(row.get(key), sample.get(key)) for key in (
                    "verification_schema", "scenario", "cycle", "runtime_id", "verification_id",
                    "checks", "features", "lifecycle",
                ))

            checks[f"cloudwatch_resumed_{cycle}_matches"] = any(matches_sample(row) for row in printed)
            checks[f"datadog_resumed_{cycle}_matches"] = any(
                matches_sample(_restore_backend_empty_errors(row, sample)) for row in backend_rows
            )

        finals = [
            row for row in printed
            if row.get("scenario") == "termination" and row.get("marker") == armed.get("marker")
            and row.get("verification_id") == deployment["verification_id"]
        ]
        final = finals[0] if len(finals) == 1 else {}
        checks["cloudwatch_single_termination_result"] = len(finals) == 1
        checks.update({
            "final_" + key: ok
            for key, ok in evaluate_termination(armed, final, guest["runtime_id"]).items()
        })
        checks["datadog_termination_result_matches"] = bool(final) and any(
            all(evaluate_termination(armed, row, guest["runtime_id"]).values())
            and all(log_values_equal(row.get(key), final.get(key)) for key in (
                "verification_schema", "scenario", "marker", "runtime_id", "verification_id",
                "passed", "checks", "evidence", "lifecycle",
            ))
            for row in (_restore_backend_empty_errors(row, final) for row in backend_rows)
        )
        span = final.get("evidence", {}).get("span", {})
        for name, rows in (("cloudwatch", printed), ("datadog", backend_rows)):
            checks[name + "_termination_marker_matches"] = bool(final) and any(
                row.get("marker") == armed.get("marker")
                and row.get("verification_id") == deployment["verification_id"]
                and row.get("runtime_id") == guest["runtime_id"]
                and row.get("trace_id") == span.get("trace_id")
                and row.get("span_id") == span.get("span_id")
                for row in rows
            )
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "skipped": skipped,
        "evidence": evidence,
        "diagnostics": diagnostics,
    }
    (output or results / "evaluation.json").write_text(json.dumps(result, indent=2) + "\n")
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    for name, reason in skipped.items():
        print(f"SKIP {name}: {reason}")
    print(
        f"Datadog evaluation: {'PASS' if result['passed'] else 'FAIL'} "
        f"({sum(checks.values())}/{len(checks)}, {len(skipped)} skipped)"
    )
    if diagnostics:
        print("Runtime metric diagnostics: " + json.dumps(diagnostics))
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, help="Save re-evaluation without overwriting evaluation.json")
    parser.add_argument(
        "--check-runtime-metrics",
        action="store_true",
        help="Require the temporarily skipped standard RSS backend query",
    )
    args = parser.parse_args()
    try:
        return evaluate(args.results, args.output, check_runtime_metrics=args.check_runtime_metrics)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        (args.output or args.results / "evaluation.json").write_text(
            json.dumps({"passed": False, "error": str(error)}, indent=2) + "\n"
        )
        print(f"FAIL evaluation: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
