"""PASS/FAIL checks for one sample-verify-app run. Invoked by `./verify.sh check`.

Ground truth is CloudWatch (what the app printed); Datadog is what arrived.
Expects DD_API_KEY / DD_APP_KEY in the environment (verify.sh runs this under
dd-auth) and AWS_VAULT_PROFILE / REGION for the CloudWatch calls.

Checks, all limited to the VM's active intervals (run/resume -> suspend/terminate):
  hooks      every suspend/terminate flush logs `flush complete`, never `timed out`
  stop       /terminate ran Stop (`Exiting concentrator`) and it didn't time out
  nostop     no Stop before /terminate: /suspend keeps the trace agent running (R3)
  fwd        run5/run6: every hook reached app.py, and its /suspend and /terminate
             log + span arrived in Datadog (R3); the counter is WARN only
  trace      trace.mvmtest.tick.hits per 10s bucket vs tick spans printed (#58958, #58960)
  burst      trace.mvmtest.burst.hits per bucket vs `spans` logs, if SPANS_PER_SEC > 0 (R2)
  metrics    mvmtest.count per bucket vs tick logs printed (regression)
  distribution  custom distribution samples vs tick spans printed (sketch path control)
  logs       every printed tick seq arrived in Datadog exactly once (regression)
  lifecycle  hook counts, allowing a final suspend's known unsent bucket;
             missing counts trigger unfiltered metric/tag diagnostics
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE = "sample-verify-app"
API = os.environ.get("DD_API_URL", "https://api.datadoghq.com")
REGION = os.environ.get("REGION", "us-east-2")
PROFILE = os.environ.get("AWS_VAULT_PROFILE", "sso-serverless-sandbox-account-admin")
BUCKET = 10

results = []  # (status, name, detail)


def report(status, name, detail):
    results.append((status, name, detail))
    print(f"  {status:<5} {name:<10} {detail}")


def hms(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def bucket(ts):
    return int(ts) // BUCKET * BUCKET


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


# --- inputs -----------------------------------------------------------------

def load_events(run, vm_id):
    """Returns (vm_id, first event ts, last event ts, latest deploy line before the run)."""
    lines = open(os.path.join(SCRIPT_DIR, "events.log")).read().splitlines()
    if not vm_id:
        ids = [l.split()[2] for l in lines if len(l.split()) > 2 and l.split()[1] == run]
        if not ids:
            sys.exit(f"no {run} entries in events.log")
        vm_id = ids[-1]
    mine = [l for l in lines if f" {vm_id} " in l]
    if not mine:
        sys.exit(f"{vm_id} not in events.log")
    t0, t1 = parse_ts(mine[0].split()[0]), parse_ts(mine[-1].split()[0])
    deploy = next((l for l in reversed(lines) if " deploy " in l and parse_ts(l.split()[0]) < t0), "")
    return vm_id, t0, t1, deploy


def cloudwatch(log_group, start, end, pattern, vm_id):
    """CloudWatch events from vm_id's stream as [(ts seconds, message)]."""
    out = subprocess.run(
        ["aws-vault", "exec", PROFILE, "--", "aws", "logs", "filter-log-events",
         "--region", REGION, "--log-group-name", log_group,
         "--start-time", str(int(start * 1000)), "--end-time", str(int(end * 1000)),
         "--filter-pattern", pattern, "--output", "json",
         "--query", "events[].[logStreamName,timestamp,message]"],
        check=True, capture_output=True, text=True, env={**os.environ, "AWS_PAGER": ""}).stdout
    return [(ts / 1000, msg.rstrip("\n")) for stream, ts, msg in json.loads(out or "[]")
            if stream.endswith(vm_id)]


def dd(path, params=None, body=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={
        "DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def dd_buckets(query, start, end):
    """10s-bucket start -> summed value for a metric query."""
    res = dd("/api/v1/query", {"from": int(start), "to": int(end), "query": query})
    out = Counter()
    for series in res.get("series", []):
        for ts_ms, v in series["pointlist"]:
            if v is not None:
                out[bucket(ts_ms / 1000)] += v
    return out


def dd_totals_by(query, tag, start, end):
    """Value of `tag` -> summed value, for a query grouped `by {tag}`."""
    res = dd("/api/v1/query", {"from": int(start), "to": int(end), "query": query})
    out = Counter()
    for series in res.get("series", []):
        tags = series.get("tag_set") or series.get("scope", "").split(",")
        value = next((t.split(":", 1)[1] for t in tags if t.startswith(f"{tag}:")), "?")
        out[value] += sum(v or 0 for _, v in series["pointlist"])
    return out


def dd_log_attrs(vm_id, start, end, message, attr):
    """`attr` of every `message:<message>` log of vm_id."""
    vals, cursor = [], None
    while True:
        body = {"filter": {"query": f"service:{SERVICE} lambda_microvm_id:{vm_id} message:{message}",
                           "from": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                           "to": datetime.fromtimestamp(end, timezone.utc).isoformat()},
                "page": {"limit": 1000}}
        if cursor:
            body["page"]["cursor"] = cursor
        res = dd("/api/v2/logs/events/search", body=body)
        vals += [d["attributes"]["attributes"].get(attr) for d in res.get("data", [])]
        cursor = res.get("meta", {}).get("page", {}).get("after")
        if not cursor:
            return [v for v in vals if v is not None]


# --- derived ----------------------------------------------------------------

def active_intervals(lifecycle, t1):
    """[(start, end, end_kind)] from run/resume to the next suspend/terminate."""
    out, start = [], None
    for ts, msg in lifecycle:
        if re.search(r"lifecycle: (run|resume)\b", msg):
            start = ts
        elif m := re.search(r"lifecycle: (suspend|terminate) — flushing", msg):
            if start is not None:
                out.append((start, ts, m.group(1)))
            start = None
    if start is not None:
        out.append((start, t1, "none"))
    return out


def in_intervals(b, intervals):
    return any(bucket(s) <= b <= bucket(e) for s, e, _ in intervals)


def compare(name, got, want, intervals, tolerance, known=(), labels=None):
    """FAILs a bucket if got < want - tolerance or got > want + tolerance."""
    labels = labels or {}
    bad, lines = 0, []
    for b in sorted(set(want) | set(got)):
        if not in_intervals(b, intervals):
            continue
        g, w = got.get(b, 0), want.get(b, 0)
        ok = abs(g - w) <= tolerance
        tag = labels.get(b, "")
        if b in known and not ok:
            status = "KNOWN"
        elif not ok:
            status, bad = "FAIL", bad + 1
        else:
            status = "ok"
        if status != "ok" or tag:
            lines.append(f"{hms(b)} got={g:g} want={w:g} {status} {tag}".rstrip())
    report("FAIL" if bad else "PASS", name, f"{bad} bad bucket(s)")
    for l in lines:
        print(f"{'':18}{l}")


def check_forwarded(lifecycle, vm_id, start, end, tags, want):
    """fwd: each hook in `want` reached app.py, and what app.py emitted while answering arrived.

    /terminate flushes after the app answered (flushSequential), so its log and
    span must arrive. /suspend flushes while the app answers (flushParallel);
    whatever that flush misses is sent after the resume, so it must arrive too.
    With no resume (run6) the miss is never sent: that's the documented
    flushParallel limitation, so it is KNOWN, not FAIL.
    The DogStatsD counter is sent last-moment over UDP and can sit unparsed in
    DogStatsD's queue when the flush starts, so it is WARN, not FAIL.
    """
    # serverless-init forwards on the platform path (/aws/lambda-microvms/runtime/v1/<hook>);
    # older app.py builds report that full path as the hook name, so key on its last segment.
    def name(h):
        return h.rstrip("/").rsplit("/", 1)[-1]

    def by_name(counts):
        out = Counter()
        for h, n in counts.items():
            out[name(h)] += n
        return out

    reached = Counter(name(json.loads(m)["hook"]) for _, m in lifecycle
                      if m.startswith("{") and '"apphook"' in m)
    not_reached = [h for h in want if not reached[h]]
    report("FAIL" if not_reached else "PASS", "fwd",
           f"hooks reached app.py: {dict(reached)}; missing {not_reached or 'none'}")
    if not_reached:
        return  # forwarding is off: the arrival checks below would test nothing

    logs = by_name(Counter(dd_log_attrs(vm_id, start, end, "apphook", "hook")))
    spans = by_name(dd_totals_by(f"sum:trace.mvmtest.hook.hits{{{tags}}} by {{resource_name}}.as_count()",
                                 "resource_name", start, end))
    metric = by_name(dd_totals_by(f"sum:mvmtest.hook{{{tags}}} by {{hook}}.as_count()", "hook", start, end))
    for hook in [h for h in ("suspend", "terminate") if h in want]:
        lost = [kind for kind, got in (("log", logs), ("span", spans)) if got[hook] < reached[hook]]
        status = "PASS" if not lost else "KNOWN" if hook == "suspend" and "resume" not in want else "FAIL"
        report(status, f"fwd-{hook[:4]}",
               f"/{hook} app telemetry: log={logs[hook]} span={spans[hook]:g} "
               f"(want {reached[hook]} each); lost {lost or 'none'}")
        if metric[hook] < reached[hook]:
            report("WARN", f"fwd-{hook[:4]}", f"/{hook} mvmtest.hook counter={metric[hook]:g} "
                   f"(want {reached[hook]}): DogStatsD packets queued at the flush")


def check_resume_suspend_gap(lifecycle):
    """Verify that run4 actually exercises the short post-resume window."""
    resume = next((ts for ts, msg in lifecycle if "lifecycle: resume" in msg), None)
    suspend = next((ts for ts, msg in lifecycle
                    if resume is not None and ts > resume and "lifecycle: suspend — flushing" in msg), None)
    if resume is None or suspend is None:
        report("FAIL", "resume-gap", "missing a resume followed by /suspend")
        return

    gap = suspend - resume
    report("PASS" if gap <= 3 else "FAIL", "resume-gap",
           f"resume → next /suspend={gap:.1f}s (target <=3s)")


def check_distribution_control(deploy, tags, start, end, want, intervals, known, labels):
    if "distribution_control=true" not in deploy.split():
        report("INFO", "distribution", "not emitted by this older image; redeploy to test the sketch path")
        return
    got = dd_buckets(f"count:mvmtest.distribution{{{tags}}}.rollup({BUCKET})", start, end)
    compare("distribution", got, want, intervals, tolerance=1, known=known, labels=labels)


def probe_indexed_metric(metric, vm_id):
    """Distinguish absent timeseries from a metric whose VM tag is indexed."""
    try:
        response = dd(f"/api/v2/metrics/{metric}/all-tags", {
            "window[seconds]": 14400,
            "filter[match]": vm_id,
            "filter[include_tag_values]": "true",
            "page[limit]": 1000,
        })
        if response.get("error") or response.get("errors"):
            raise RuntimeError("metric tags API returned an error")
        attributes = (response.get("data") or {}).get("attributes") or {}
        target = f"lambda_microvm_id:{vm_id}"
        if target in (attributes.get("tags") or []):
            detail = ("this VM tag is indexed for the metric in this org (last 4h), "
                      "but the timeseries query returned no data; investigate query access/filtering. "
                      "Indexed tags do not validate the expected point count")
        elif target in (attributes.get("ingested_tags") or []):
            detail = ("this VM tag is ingested but not indexed (last 4h); "
                      "check metric tag configuration and query access")
        else:
            detail = "this VM tag was not returned by the last-4h index lookup; this alone does not prove data loss"
        report("INFO", "lifecycle-index", detail)
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        # This secondary diagnostic must not replace the failed count assertion.
        report("INFO", "lifecycle-index", f"index lookup unavailable: {exc}")


def check_lifecycle_metrics(lifecycle, vm_id, start, end):
    # Distribution metrics are timestamped at the bucket start. Query complete
    # surrounding buckets, rather than dropping the bucket containing a hook.
    start, end = bucket(start) - BUCKET, bucket(end) + 2 * BUCKET
    events = [m.group(1) for _, msg in lifecycle
              if (m := re.search(r"lifecycle: (suspend|terminate) — flushing", msg))]
    final_suspend = bool(events) and events[-1] == "suspend"

    def query(metric, scope, grouped=False):
        expression = f"count:{metric}{{{scope}}}"
        if grouped:
            expression += " by {lambda_microvm_id}"
        # Distributions aggregate sketches over time; their rollup takes an
        # interval, without the sum/avg aggregator used for gauge metrics.
        expression += ".rollup(10)"
        response = dd("/api/v1/query", {"from": int(start), "to": int(end), "query": expression})
        if response.get("error") or response.get("errors"):
            raise RuntimeError(f"metric query failed: {response}")
        return response.get("series", [])

    def total(series):
        return sum(v or 0 for s in series for _, v in s["pointlist"])

    for hook in ("suspend", "terminate"):
        expected = events.count(hook)
        if not expected:
            continue
        metric = f"aws.lambda.microvm.enhanced.{hook}"
        count = total(query(metric, f"lambda_microvm_id:{vm_id}"))
        minimum = expected - int(hook == "suspend" and final_suspend)
        status = "FAIL" if count < minimum or count > expected else "PASS"
        if count < expected and status == "PASS":
            status = "KNOWN"
        detail = f"count={count:g}, expected={minimum}..{expected}"
        if hook == "suspend":
            detail += " (final suspend may remain in the open bucket)"
        report(status, f"lifecycle-{hook[:4]}", detail)
        if count == 0:
            # These broad results are diagnostics, never evidence for this VM.
            for name in (metric, f"aws.lambda.enhanced.microvm.{hook}"):
                series = query(name, "*")
                report("INFO", "lifecycle-query",
                       f"{name} without VM filter: {len(series)} series, count={total(series):g}, "
                       f"API={API}")
                if series:
                    # Separate from the ungrouped lookup: grouping by a missing
                    # tag must not hide otherwise-existing metric data.
                    grouped = query(name, "*", grouped=True)
                    tags = [s.get("tag_set") or s.get("scope", "").split(",") for s in grouped[:5]]
                    report("INFO", "lifecycle-tags",
                           f"{name} grouped by lambda_microvm_id: {len(grouped)} series, "
                           f"sample tags={tags}")
            report("INFO", "lifecycle-query",
                   "unfiltered results are not attributed to this VM; check the ID tag, "
                   "metric name and DD_AUTH_DOMAIN if the scoped count is missing")
            if status == "FAIL":
                probe_indexed_metric(metric, vm_id)


# --- main -------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("run1", "run2", "run3", "run4", "run5", "run6"):
        sys.exit("usage: check.py <run1|run2|run3|run4|run5|run6> [vm-id]")
    run = sys.argv[1]
    variant = {"run3": "default", "run5": "fwd", "run6": "fwd"}.get(run, "long")
    vm_id, t0, t1, deploy = load_events(run, sys.argv[2] if len(sys.argv) > 2 else "")
    spans_per_sec = int(m.group(1)) if (m := re.search(r"spans_per_sec=(\d+)", deploy)) else 0
    log_group = f"/aws/lambda-microvms/{SERVICE}-{variant}"
    start, end = t0 - 60, t1 + 120
    tags = f"service:{SERVICE},version:{variant}"

    print(f"{run} {vm_id} ({hms(t0)}–{hms(t1)} UTC, image {variant}, spans_per_sec={spans_per_sec})")
    if m := re.search(r"sha256=(\w+)", deploy):
        print(f"  serverless-init sha256 {m.group(1)}")

    lifecycle = cloudwatch(log_group, start, end,
                           '?"MicroVM lifecycle" ?"did not stop in time" ?"Exiting concentrator" ?apphook', vm_id)
    printed = cloudwatch(log_group, start, end, '{ $.msg = "tick" || $.msg = "spans" }', vm_id)
    intervals = active_intervals(lifecycle, t1)
    if not intervals:
        sys.exit("no run/resume lifecycle lines found in CloudWatch for this VM")
    final_end, final_kind = intervals[-1][1], intervals[-1][2]

    # hooks: each flushing line is followed by `flush complete` before the next hook.
    hooks = [(ts, msg) for ts, msg in lifecycle
             if re.search(r"flushing telemetry|flush complete|flush timed out", msg)]
    flushing = sum("flushing telemetry" in m for _, m in hooks)
    complete = sum("flush complete" in m for _, m in hooks)
    timed_out = [hms(ts) for ts, m in hooks if "flush timed out" in m]
    report("FAIL" if timed_out or complete < flushing else "PASS", "hooks",
           f"{flushing} flush(es), {complete} complete, timed out at {timed_out or 'none'}")

    # stop: /terminate must run Stop, which logs `Exiting concentrator`.
    term = [ts for ts, m in lifecycle if "lifecycle: terminate — flushing" in m]
    stuck = [hms(ts) for ts, m in lifecycle if "did not stop in time" in m]
    if run in ("run2", "run3", "run5"):
        stopped = term and any("Exiting concentrator" in m and ts >= term[0] for ts, m in lifecycle)
        report("PASS" if stopped and not stuck else "FAIL", "stop",
               f"/terminate at {hms(term[0]) if term else 'MISSING'}, Stop ran={bool(stopped)}, "
               f"did-not-stop-in-time={stuck or 'none'}")
    else:
        report("INFO", "stop", f"/terminate {'arrived' if term else 'not called'} "
               f"(terminated while SUSPENDED; expected not called)")

    # nostop: only /terminate may stop the trace agent. A Stop at a /suspend leaves
    # tracing dead after the resume (and its buckets FAIL `trace` below).
    early = [hms(ts) for ts, m in lifecycle
             if "Exiting concentrator" in m and (not term or ts < term[0])]
    report("FAIL" if early else "PASS", "nostop",
           f"trace agent stopped before /terminate at {early or 'none'}")

    if run == "run4":
        check_resume_suspend_gap(lifecycle)

    # Ground truth per bucket from what the app printed between the run hook and the
    # final flush. Lines printed before the run hook (snapshot restore) or after the
    # final flush started are never sent by any hook, so they aren't checked.
    first_start = intervals[0][0]
    ticks, outside = [], []
    want_spans, want_burst, want_count = Counter(), Counter(), Counter()
    for ts, msg in printed:
        try:
            ev = json.loads(msg)
        except ValueError:
            continue
        if ev.get("msg") == "tick" and not first_start <= ts <= final_end:
            outside.append(ev["seq"])
        elif ev.get("msg") == "tick":
            ticks.append(ev["seq"])
            want_count[bucket(ts)] += 1
            if ev["seq"] % 10 == 0:
                want_spans[bucket(ts)] += 1
        elif ev.get("msg") == "spans":
            want_burst[bucket(ev["sec"])] += ev["n"]

    labels = {}
    for i, (_, e, kind) in enumerate(intervals):
        final = i == len(intervals) - 1
        if kind == "terminate":
            labels[bucket(e)] = "<- /terminate bucket (#58958)"
        elif kind == "suspend":
            labels[bucket(e)] = ("<- final /suspend bucket (#58960)" if final
                                 else "<- resumed /suspend bucket (#58960 overwrite check)")

    compare("trace", dd_buckets(f"sum:trace.mvmtest.tick.hits{{{tags}}}.as_count().rollup(sum,{BUCKET})",
                                start, end), want_spans, intervals, tolerance=1, labels=labels)

    if spans_per_sec:
        got = dd_buckets(f"sum:trace.mvmtest.burst.hits{{{tags}}}.as_count().rollup(sum,{BUCKET})", start, end)
        # The terminate bucket also holds the <1s of spans never logged, so only a shortfall fails there.
        if final_kind == "terminate":
            b = bucket(final_end)
            got[b] = min(got.get(b, 0), want_burst.get(b, 0) + spans_per_sec)
        compare("burst", got, want_burst, intervals, tolerance=spans_per_sec, labels=labels)

    # The final suspend's metrics bucket is never sent when the VM is terminated while
    # suspended (aggregator follow-up, docs/dev/aws-microvm-aggregator-late-sample-followup.md).
    known = {bucket(final_end)} if final_kind == "suspend" else set()
    compare("metrics", dd_buckets(f"sum:mvmtest.count{{{tags}}}.as_count().rollup(sum,{BUCKET})", start, end),
            want_count, intervals, tolerance=2, known=known, labels=labels)
    check_distribution_control(deploy, tags, start, end, want_spans, intervals, known, labels)

    got = dd_log_attrs(vm_id, start, end, "tick", "seq")
    missing = sorted(set(ticks) - set(got))
    dups = sum(c - 1 for c in Counter(got).values() if c > 1)
    report("FAIL" if missing or dups else "PASS", "logs",
           f"{len(ticks)} printed, {len(set(got))} arrived, {len(missing)} missing"
           f"{' ' + str(missing[:10]) if missing else ''}, {dups} duplicate(s)")
    if outside:
        report("INFO", "unsent", f"{len(outside)} tick(s) printed before the run hook or after the final "
               f"flush started, not checked: {sorted(outside)[:10]}")

    if run == "run5":
        check_forwarded(lifecycle, vm_id, start, end, tags, ("run", "suspend", "resume", "terminate"))
    elif run == "run6":
        check_forwarded(lifecycle, vm_id, start, end, tags, ("run", "suspend"))

    check_lifecycle_metrics(lifecycle, vm_id, start, end)

    failed = [n for s, n, _ in results if s == "FAIL"]
    print(f"\n{run}: {'FAIL (' + ', '.join(failed) + ')' if failed else 'PASS'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
