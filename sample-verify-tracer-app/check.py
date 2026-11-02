"""Evaluate guest evidence and Datadog arrival, with credentials supplied by dd-auth."""

import argparse
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from feature_checks import FEATURE_OPERATIONS
from feature_checks import evaluate_features
from lifecycle_checks import SCHEMA
from lifecycle_checks import required_telemetry_checks


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
    if schema >= 2:
        checks["guest_telemetry_scenarios_present"] = guest.get(
            "verification_schema"
        ) == schema and required_telemetry_checks(schema).issubset(guest["checks"])
    if schema < SCHEMA:
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
        checks["guest_feature_scenarios_present"] = (
            guest.get("verification_schema") == deployment["verification_schema"]
            and expected_checks.issubset(guest["checks"])
            and set(FEATURE_OPERATIONS).issubset(guest.get("features", {}).get("roots", {}))
        )
        for case, operation in FEATURE_OPERATIONS.items():
            values = query("feature_trace_" + case, f"sum:trace.{operation}.hits{{{tags}}}.as_count()")
            checks["datadog_feature_" + case + "_arrived"] = sum(values) >= 1
        errors = query("feature_errors", f"sum:trace.{FEATURE_OPERATIONS['error']}.errors{{{tags}}}.as_count()")
        checks["datadog_feature_error_reported"] = sum(errors) >= 1
    else:
        skipped["common_feature_scenarios"] = (
            "This deployment predates the expanded feature suite; deploy again to run it."
        )
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
                    '"tracer_verification"'
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
