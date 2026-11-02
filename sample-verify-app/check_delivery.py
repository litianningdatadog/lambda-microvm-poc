"""Offline delivery evidence checks; no credentials or network access required."""
import argparse
import json
import math
from pathlib import Path
import uuid


SCENARIOS = ("initial", "recovery-http503", "recovery-disconnect",
             "resumed-1", "resumed-2", "resumed-3")
FIELDS = ("msg", "schema", "scenario", "marker", "runId", "wall", "value", "counters")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def counters(resumes, terminated=0):
    return {"runCount": 1, "resumeCount": resumes, "suspendCount": resumes,
            "terminateCount": terminated}


def valid_marker(marker):
    if not isinstance(marker, dict):
        return False
    try:
        valid_id = str(uuid.UUID(marker.get("marker", ""))) == marker["marker"]
    except (ValueError, TypeError, AttributeError):
        return False
    counts = marker.get("counters")
    return (valid_id and marker.get("msg") == "mvmtest_delivery"
            and type(marker.get("schema")) is int and marker["schema"] == 1
            and marker.get("scenario") in (*SCENARIOS, "termination")
            and isinstance(marker.get("runId"), str) and bool(marker["runId"])
            and number(marker.get("wall")) and marker["wall"] > 0
            and number(marker.get("value")) and marker["value"] == 42
            and isinstance(counts, dict) and set(counts) == set(counters(0))
            and all(type(v) is int and v >= 0 for v in counts.values()))


def clean_response(response):
    if not isinstance(response, dict) or any(response.get(k) for k in ("error", "errors", "warnings")):
        return False
    meta = response.get("meta", {})
    if not isinstance(meta, dict) or meta.get("warnings"):
        return False
    page = meta.get("page", {})
    return isinstance(page, dict) and not page.get("after") and meta.get("status", "done") == "done"


def logs_match(response, marker):
    if not clean_response(response) or not isinstance(response.get("data"), list):
        return False
    found = False
    for entry in response["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("attributes"), dict):
            return False
        outer = entry["attributes"]
        row = outer.get("attributes")
        if not isinstance(row, dict):
            return False
        row = dict(row)
        if "msg" not in row and outer.get("message") == "mvmtest_delivery":
            row["msg"] = "mvmtest_delivery"
        if not valid_marker(row) or not all(row[key] == marker[key] for key in FIELDS):
            return False
        found = True
    return found


def metrics_match(response, marker):
    if not clean_response(response) or response.get("status") != "ok":
        return False
    series = response.get("series")
    if not isinstance(series, list) or not series:
        return False
    expected = {"service:sample-verify-app", "marker:" + marker["marker"],
                "run_id:" + marker["runId"]}
    found = False
    for item in series:
        if not isinstance(item, dict) or item.get("metric") != "mvmtest.delivery":
            return False
        scope = item.get("scope", "")
        tags = item.get("tag_set")
        if tags is None or tags == []:
            tags = scope.split(",") if isinstance(scope, str) else None
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return False
        if not expected.issubset(tags):
            return False
        points = item.get("pointlist")
        if not isinstance(points, list) or not points:
            return False
        for point in points:
            if (not isinstance(point, list) or len(point) != 2 or not number(point[0])
                    or (point[1] is not None and not number(point[1]))):
                return False
            found |= point[1] == 42
    return found


def marker_delivered(sample, require_metric=True):
    """True only for matching backend evidence, never a local relay acknowledgement."""
    if not isinstance(sample, dict) or not valid_marker(sample.get("marker")):
        return False
    marker = sample["marker"]
    return (logs_match(sample.get("logs"), marker)
            and (not require_metric or metrics_match(sample.get("metrics"), marker)))


def healthy(probes, count, start, end):
    return (number(start) and number(end)
            and isinstance(probes, list) and len(probes) == count
            and all(isinstance(p, dict) and type(p.get("status")) is int
                    and p["status"] == 200 and number(p.get("elapsedMs"))
                    and 0 <= p["elapsedMs"] < 1000 and number(p.get("at"))
                    and start <= p["at"] <= p["at"] + p["elapsedMs"] / 1000 <= end
                    for p in probes))


def within(observed, emitted, deadline):
    return number(observed) and number(emitted) and 0 <= observed - emitted <= deadline


def evaluate(evidence):
    """Return named strict checks and the original evidence without modifying it."""
    source = evidence
    evidence = evidence if isinstance(evidence, dict) else {}
    checks = {}
    start, end = evidence.get("startedAt"), evidence.get("endedAt")
    vm = evidence.get("vm")
    vm = vm if isinstance(vm, dict) else {}
    checks["run"] = (type(evidence.get("schema")) is int and evidence["schema"] == 1
                     and evidence.get("deadlineSeconds") == 180
                     and number(start) and number(end) and 0 < start <= end
                     and all(isinstance(vm.get(k), str) and vm[k]
                             for k in ("microvmId", "imageArn", "imageVersion")))
    checks["no_runner_error"] = not evidence.get("error")
    samples = evidence.get("samples")
    samples = samples if isinstance(samples, list) else []
    by_scenario = {s["marker"]["scenario"]: s for s in samples
                   if isinstance(s, dict) and valid_marker(s.get("marker"))}
    checks["samples"] = (len(samples) == 6 and len(by_scenario) == 6
                         and set(by_scenario) == set(SCENARIOS))
    markers = [s["marker"] for s in by_scenario.values()]
    checks["identity"] = (len(markers) == 6
                          and len({m["marker"] for m in markers}) == 6
                          and len({m["runId"] for m in markers}) == 1)
    for scenario in SCENARIOS:
        sample = by_scenario.get(scenario, {})
        marker = sample.get("marker", {})
        n = int(scenario[-1]) if scenario.startswith("resumed-") else 0
        checks[scenario + ".backend"] = marker_delivered(sample)
        checks[scenario + ".deadline"] = (within(sample.get("observedAt"), marker.get("wall"), 180)
                                          and number(start) and number(end)
                                          and start <= marker["wall"] <= sample["observedAt"] <= end)
        checks[scenario + ".counters"] = marker.get("counters") == counters(n)

    # Each transition must be observed, and each delivery must finish before the next suspend.
    cycles = evidence.get("cycles")
    order = isinstance(cycles, list) and len(cycles) == 3
    prior = by_scenario.get("recovery-disconnect", {}).get("observedAt")
    for n in range(1, 4):
        cycle = cycles[n - 1] if order else {}
        if not isinstance(cycle, dict):
            cycle = {}
        suspended, resumed = cycle.get("suspended", {}), cycle.get("resumed", {})
        suspended = suspended if isinstance(suspended, dict) else {}
        resumed = resumed if isinstance(resumed, dict) else {}
        sample = by_scenario.get(f"resumed-{n}", {})
        wall, observed = sample.get("marker", {}).get("wall"), sample.get("observedAt")
        times = (prior, suspended.get("at"), resumed.get("at"), wall, observed)
        ok = (cycle.get("cycle") == n and suspended.get("state") == "SUSPENDED"
              and resumed.get("state") == "RUNNING" and all(number(t) for t in times)
              and list(times) == sorted(times))
        checks[f"cycle-{n}.ordering"] = bool(order and ok)
        checks[f"resumed-{n}.health"] = healthy(sample.get("health"), 1, resumed.get("at"), wall)
        prior = observed

    recoveries = evidence.get("recoveries")
    recoveries = recoveries if isinstance(recoveries, list) else []
    checks["recoveries"] = (len(recoveries) == 2
                            and [r.get("mode") if isinstance(r, dict) else None
                                 for r in recoveries] == ["http503", "disconnect"])
    previous = by_scenario.get("initial", {}).get("observedAt")
    for mode in ("http503", "disconnect"):
        matching = [r for r in recoveries if isinstance(r, dict) and r.get("mode") == mode]
        recovery = matching[0] if len(matching) == 1 else {}
        scenario = "recovery-" + mode
        sample = by_scenario.get(scenario, {})
        times = (previous, recovery.get("startedAt"), recovery.get("restoredAt"),
                 sample.get("marker", {}).get("wall"), sample.get("observedAt"))
        checks[scenario + ".restored"] = (recovery.get("freshScenario") == scenario
                                            and all(number(t) for t in times)
                                            and list(times) == sorted(times)
                                            and times[2] < times[3])
        checks[scenario + ".health"] = healthy(recovery.get("health"), 3, times[1], times[2])
        requests = recovery.get("requests")
        valid = isinstance(requests, list) and bool(requests)
        pipelines = set()
        for request in requests if isinstance(requests, list) else []:
            if not isinstance(request, dict):
                valid = False
                continue
            received, completed = request.get("receivedAt"), request.get("completedAt")
            injected = "injected-" + mode
            status = 503 if mode == "http503" else None
            timed = (request.get("pipeline") in ("logs", "metrics")
                     and all(number(t) for t in (times[1], received, completed, times[2]))
                     and times[1] <= received <= completed <= times[2])
            fault = (request.get("mode") == mode and request.get("outcome") == injected
                     and "status" in request and request["status"] == status)
            accepted = (request.get("mode") == "normal" and request.get("outcome") == "accepted"
                        and type(request.get("status")) is int and 200 <= request["status"] < 300)
            valid &= timed and (fault or accepted)
            if timed and fault:
                pipelines.add(request["pipeline"])
        checks[scenario + ".fault"] = bool(valid and pipelines == {"logs", "metrics"})
        previous = sample.get("observedAt")

    termination = evidence.get("termination")
    termination = termination if isinstance(termination, dict) else {}
    arm = termination.get("arm")
    arm = arm if isinstance(arm, dict) else {}
    # Arming returns the marker template. Only the hook's CloudWatch row proves emission.
    checks["termination.arm"] = (valid_marker(arm) and arm.get("scenario") == "termination"
                                 and arm.get("armed") is True
                                 and arm.get("counters") == counters(3)
                                 and bool(markers) and arm.get("runId") == markers[0]["runId"]
                                 and arm.get("marker") not in {m["marker"] for m in markers}
                                 and number(prior) and number(arm.get("wall"))
                                 and prior <= arm["wall"])
    rows = evidence.get("cloudwatch")
    rows = rows if isinstance(rows, list) else []
    finals = [r for r in rows if isinstance(r, dict) and isinstance(r.get("row"), dict)
              and r["row"].get("marker") == arm.get("marker")]
    final = finals[0] if len(finals) == 1 else {}
    row = final.get("row", {})
    checks["termination.cloudwatch"] = (valid_marker(row) and row.get("scenario") == "termination"
                                         and all(row.get(k) == arm.get(k) for k in
                                                 ("marker", "runId", "schema", "value", "msg"))
                                         and row.get("counters") == counters(3, 1)
                                         and number(arm.get("wall")) and number(row.get("wall"))
                                         and number(final.get("at")) and number(start) and number(end)
                                         and start <= final["at"] <= end
                                         and arm["wall"] <= row["wall"])
    checks["termination.backend"] = bool(valid_marker(row) and logs_match(termination.get("logs"), row))
    checks["termination.deadline"] = within(termination.get("observedAt"), row.get("wall"), 180)
    checks["termination.state"] = (vm.get("terminated") is True and number(vm.get("terminatedAt"))
                                    and number(row.get("wall")) and number(end)
                                    and row["wall"] <= vm["terminatedAt"] <= end
                                    and number(termination.get("observedAt"))
                                    and termination["observedAt"] <= end)
    return {"schema": 1, "passed": all(checks.values()), "checks": checks, "evidence": source}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.source.resolve() == args.output.resolve()
            or (args.output.exists() and args.source.samefile(args.output))):
        parser.error("output must not overwrite source evidence")
    with args.source.open() as handle:
        source = json.load(handle)
    # Re-evaluation may consume a previous result without nesting evidence forever.
    if isinstance(source, dict) and "evidence" in source and "checks" in source:
        source = source["evidence"]
    result = evaluate(source)
    # Exclusive creation also protects existing artifacts and symlinks.
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    for name, passed in result["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
