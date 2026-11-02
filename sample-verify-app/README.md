# sample-verify-app

Verifies how serverless-init behaves across MicroVM suspend / resume / terminate.
Only a ❌ result below is a reason to change serverless-init.

serverless-init comes from a **local arm64 build**:
- `./verify.sh deploy` copies `$SERVERLESS_INIT_BIN` into this directory as
  `serverless-init-linux-arm64`, and the `Dockerfile` does `COPY serverless-init-linux-arm64 /serverless-init`.
- The default is `/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64`, built
  with `cmd/serverless-init/build-linux-arm64.sh`.
- `events.log` records the binary path and sha256 for every deploy.
- To use the released image instead (as `../sample-flask-app` does), swap the commented
  `COPY --from=datadog/serverless-init:1.10.4 ...` line back in.

The 2026-10-27 00:14–01:55 UTC runs used **1.10.4**. Between tag `serverless-init-1.10.4` and
datadog-agent `main`, the suspend/resume/terminate and flush logic is unchanged. Two differences
matter:
- Lifecycle metric names: 1.10.4 emits `aws.lambda.enhanced.microvm.*`, `main` (and local builds
  from it) emits `aws.lambda.microvm.enhanced.*`.
- CPU enhanced metrics (`aws.lambda.microvm.enhanced.cpu.*`): `main` has a proc-stat fallback
  (#56291) that 1.10.4 lacks. 1.10.4 emitted no `aws.lambda.microvm.enhanced.*` metrics at all
  (no cgroups in the MicroVM guest).

The code references below point at `main`.

The app (`app.py`) emits telemetry continuously, independent of inbound traffic:

| Signal | Rate | Used for |
|---|---|---|
| JSON log `{"msg":"tick","seq":N}` | 10/s | Q2: lost log batches |
| DogStatsD gauge `mvmtest.seq` + span `mvmtest.tick` | 1/s | H1/H2: lost metrics / trace stats on terminate |
| DogStatsD distribution `mvmtest.distribution` | 1/s | Control for sketch delivery, compared with enhanced lifecycle metrics |
| JSON log `{"msg":"clock","wall","mono","uptime"}` | every 5s | Q4: guest clocks across suspend |
| Span `mvmtest.burst` + JSON log `{"msg":"spans","sec","n"}` | `SPANS_PER_SEC` (deploy-time, default 0 = off) | R2: trace stats bucket overwritten |
| JSON log `{"msg":"apphook","hook"}` + span `mvmtest.hook` + DogStatsD `mvmtest.hook` (tag `hook`) | once per forwarded hook, image `fwd` only | R3: forwarded hooks |

serverless-init answers all lifecycle hooks itself (`DD_AWS_MICROVM_USER_APP_PORT` is not set),
except on image `fwd`, where it forwards run/resume/suspend/terminate to `app.py` on port 8081.
It writes the app's stdout to both CloudWatch and the Datadog logs pipeline, so
**CloudWatch = what was printed, Datadog = what arrived.**

## Questions

| ID | Question | serverless-init code (main) | Run |
|---|---|---|---|
| Q1 | Is `/terminate` called when a **SUSPENDED** VM is terminated? | `cmd/serverless-init/lifecycle/server.go:735` | run1 |
| Q2 | Is a logs batch in flight at the freeze dropped? | `comp/logs-library/client/http/sync_destination.go:77-90` | run1, run3 |
| Q3 | Does the AWS default 1s hook timeout cut our 1.5s flush? | `cmd/serverless-init/main.go:100` | run3 vs run1 |
| Q4 | Wall / monotonic clock behavior across suspend | `cmd/serverless-init/enhanced-metrics/collector.go:290` | run1 |
| Q5 | Does AWS retry hooks (double-counted lifecycle metrics)? | `lifecycle/server.go:801` | all |
| H1 | `/terminate`: is the async metrics POST lost? | `comp/forwarder/defaultforwarder/impl/domain_forwarder.go:343` | run2 |
| H2 | `/terminate`: is the open APM stats bucket lost? | `pkg/trace/agent/agent.go:307-325` | run2 |
| R3 | With forwarding on, does `/suspend` keep the trace agent running and `/terminate` send what the app emitted while answering? | `lifecycle/server.go` `flushForSuspend` / `flushAndStop` / `handleWithForwarder` | run5, run6 |

## Steps

Requires `aws-vault` (profile `sso-serverless-sandbox-account-admin`), AWS CLI v2 and `jq`.
run4 also requires `uv`; it installs the runner's boto3 dependency in an isolated cached
environment and uses one AWS session and persistent client for the entire scenario.
Defaults (region, roles, base image) match `../deploy-microvm.sh`.

```bash
cd sample-verify-app
export DD_API_KEY=<test key>            # stored in the image env; use a key you can revoke
# optional: export SERVERLESS_INIT_BIN=/path/to/serverless-init-linux-arm64

./verify.sh deploy long                  # ~ a few minutes each
./verify.sh deploy default
./verify.sh deploy fwd

./verify.sh run1                         # ~6 min
./verify.sh run2                         # ~2 min
./verify.sh run3                         # ~3 min
./verify.sh run5                         # ~3 min
./verify.sh run6                         # ~2 min
```

Run them one at a time. Custom metrics and spans carry no `lambda_microvm_id`, so in Datadog
filter by time window (from `events.log`) plus `version:long` / `version:default`.

Collect evidence:

```bash
./verify.sh logs long 20m       # lifecycle, flush complete/timed out, Could not send payload
./verify.sh clock long 20m      # clock lines for Q4
./verify.sh logs default 15m
cat events.log                  # UTC time of every API call and observed state
```

Automated check (needs `dd-auth`; queries `dddev.datadoghq.com` unless `DD_AUTH_DOMAIN` is set):

The wrapper uses `dd-auth --force-app-key`. For the same VM and dddev org, the default
PAT returned no enhanced lifecycle timeseries while application-key authentication
returned the expected terminate count of 1. This authentication-context difference
caused a false-negative lifecycle check; the exact backend access rule remains unconfirmed.
`run-all.sh` inherits application-key authentication through `verify.sh check`.

```bash
./verify.sh check run2                  # latest run2 VM in events.log
./verify.sh check run1 microvm-…        # a specific VM
```

It compares Datadog against what the app printed to CloudWatch, per 10s bucket, inside the VM's
active intervals, and exits 1 on any FAIL:

| Check | Criterion |
|---|---|
| `hooks` | Every `suspend`/`terminate — flushing telemetry` reaches `flush complete`; no `flush timed out` |
| `stop` | run2/run3/run5: `/terminate` ran `Stop()` (`Exiting concentrator`), no `did not stop in time` |
| `nostop` | All runs: no `Exiting concentrator` before `/terminate`. A `/suspend` must never stop the trace agent (R3) |
| `fwd` | run5/run6: `app.py` logged an `apphook` line for every forwarded hook of the run (forwarding is really on) |
| `fwd-susp` / `fwd-term` | run5/run6: the `apphook` log and `mvmtest.hook` span emitted while answering `/suspend` / `/terminate` arrived. The `mvmtest.hook` counter is `WARN` only (see R3) |
| `trace` | `trace.mvmtest.tick.hits` = tick spans printed (±1), including the `/terminate` bucket (#58958) and the final `/suspend` bucket (#58960) |
| `burst` | With `SPANS_PER_SEC`: `trace.mvmtest.burst.hits` = `spans` logs (± one second's worth) (R2) |
| `metrics` | `mvmtest.count` = tick logs printed (±2). The final `/suspend` bucket of a VM terminated while suspended is `KNOWN` (aggregator follow-up) |
| `distribution` | Custom distribution sample count = printed tick spans (±1); the final suspend's open bucket is `KNOWN`. Skipped for older images that lack the control |
| `logs` | Every tick seq printed arrived in Datadog once |

Deploy-time expectations (including burst rate and distribution-control availability) come
from the latest deployment **of the run's image variant** before that VM started, not
the latest deployment of any image. `run5`/`run6` use `fwd`, `run3` uses `default`,
and the other runs use `long`.

The 2026-11-01 run5 VM `microvm-25accf87-90b5-304e-86bf-4d5429607caf` used `fwd` 5.0,
whose packaged app did not emit `mvmtest.distribution`. The checker previously borrowed
`distribution_control=true` from `long` 45.0 and falsely failed all distribution buckets.
That old image now reports the control as `INFO`, not a validated PASS. To validate
distribution delivery across resume, deploy the current app with `./verify.sh deploy fwd`,
run `./verify.sh run5`, wait 180s for ingestion, then run `./verify.sh check run5`.

Ticks printed before the `run` hook or after the final flush started are reported as `INFO unsent`
and not checked: no hook sends them. Lifecycle distribution counts are queried over complete
10s buckets and checked against the CloudWatch hooks. A final suspend's unsent count is
`KNOWN`; earlier suspends and running-VM terminations must arrive. Missing scoped counts
print unfiltered current/legacy metric-name queries and sample instance tags to distinguish
missing tags from absent series. Unfiltered counts never satisfy a VM's assertion. Confirm
`DD_AUTH_DOMAIN` matches the org receiving the image's telemetry if both queries are empty.
The checker prints the selected org and API URL. For the 2026-10-31 run, direct queries
confirmed custom metrics in `dddev.datadoghq.com`; `app.datadoghq.com` had neither the
custom control metric nor the lifecycle metrics. Keep using dddev for this test setup.
Missing required counts now also query the same org's indexed/ingested metric tags for this VM
over the last four hours. For diagnostic VM `microvm-bde1ae12-4eb0-34a8-9044-0373488af141`,
the dddev indexed-tags API returned its terminate-metric VM tag despite `/api/v1/query`
returning no timeseries. This is a visibility discrepancy, not proof that the sample was
lost. Standard `aws.lambda.*` metrics can follow a different query-access path from custom
`mvmtest.*` metrics. The same VM passed when checked with `--force-app-key`; this is now
the wrapper's default. If investigating the PAT-specific difference, share the paired
results with the metrics-query owners.
The checker retains FAIL until the expected count is visible; tag presence alone does not
validate the count, and missing tags outside the four-hour lookup window are inconclusive.

`./verify.sh logs` also captures sketch flushes, sketch HTTP responses, warnings and errors.
For the 2026-10-31 run2 VM, CloudWatch showed sketch flushes throughout the run (four at
termination) and an earlier sketch payload accepted with HTTP 202. This does not prove
that the terminate metric was indexed. A newly deployed image emits `mvmtest.distribution`
through the custom-metric path to distinguish a general sketch-delivery failure from an
enhanced-metric-specific problem. If the control passes but lifecycle counts fail, investigate
the enhanced metric's emission, origin metadata, backend processing and query visibility. If both fail, inspect
the shared sketch pipeline and HTTP delivery. A 202 response alone cannot distinguish them.

If the distribution control passes but lifecycle metrics remain missing, deploy with
`DD_LOG_PAYLOADS=true ./verify.sh deploy long` before running run2. The existing log command
will capture `Flushing Sketches` entries, including metric names, tags and sample values.
Check for this VM's `aws.lambda.microvm.enhanced.terminate` in the terminate flush. Its
presence narrows the failure to serialization/delivery/backend processing or query visibility; its absence
points back to lifecycle sample emission/aggregation. A regular deploy defaults payload
logging back to false. Payload logging includes the test telemetry and tags.

Redeploy just the long image and rerun the running-VM termination control to collect this evidence:

```bash
./verify.sh deploy long
./verify.sh run2
sleep 180
./verify.sh logs long 10m
./verify.sh check run2
```

Validated 2026-10-27: 1.10.4 run2 `microvm-512cb13a-…` fails `trace`/`stop`; the #58960-tip runs
`microvm-b86fb5c5-…`, `microvm-34e70669-…` (run2), `microvm-68bb99d5-…` (run1),
`microvm-ba06162c-…` (run4) pass.

Datadog queries (service `sample-verify-app`):

- Logs: `service:sample-verify-app version:long message:tick` (Datadog maps the JSON `msg` into `message`; `@seq` is an attribute but not a measure facet, so check gaps by fetching the logs)
- `max:mvmtest.seq{service:sample-verify-app,version:long}`
- `sum:trace.mvmtest.tick.hits{service:sample-verify-app,version:long}.as_count()` (if empty, find the name in Metrics Explorer)
- `sum:aws.lambda.microvm.enhanced.{run,suspend,resume,terminate}{lambda_microvm_id:<id>}.as_count()` (`main`/local-build names; 1.10.4 used `aws.lambda.enhanced.microvm.*`)
- `avg:aws.lambda.microvm.enhanced.cpu.usage{image_name:sample-verify-app-long}` (collector prefix is `aws.lambda.microvm.` + `enhanced.` in both 1.10.4 and main)

## What to look for

### Q1: run1, after the second suspend
- CloudWatch shows `MicroVM lifecycle: terminate — flushing telemetry` (maybe preceded by `MicroVM lifecycle: resume`) → **A: design holds** ✅
- Nothing after the second `MicroVM lifecycle: suspend — flushing telemetry` → **B** ❌: `/suspend` is the final flush for idle-terminated VMs
- Datadog `aws.lambda.enhanced.microvm.terminate` for that id: 1 (A) or 0 (B). In B, expect the last ~10s of `mvmtest.seq` before the suspend to be missing.

### Q2: around each suspend (run1 ×2, run3 ×1)
- From CloudWatch, take the last tick `seq` before the suspend and the first after the resume. In Datadog, check that every `seq` in that range is present.
- A contiguous block missing right before a suspend **and** `Could not send payload` after `MicroVM lifecycle: resume` → ❌ the batch in flight at the freeze was dropped.
- No gaps → ✅ for this run. Repeat, or raise `LOG_PAD_BYTES` in `verify.sh` (e.g. 8192) to make it more likely a send is in flight.

### Q3: run3 (1s) vs run1 (5s)
- `MicroVM lifecycle: flush timed out after 1.5s` in run3 logs, or `events.log` shows a state other than `SUSPENDED` after the suspend (e.g. `TERMINATING` with a hook-related `stateReason`) → ❌.
- Last ~10s before suspend and terminate are equally complete in run3 and run1/run2 → ✅.

### Q4: run1, last clock line before suspend #1 vs first after resume
Compute `Δwall`, `Δmono`, `Δuptime` and compare with the real elapsed time (from `events.log`):
- `Δwall ≈ real` → wall clock fine ✅. `Δwall` much smaller and catching up later → wall clock stale after resume ❌ (timestamps off until it syncs; note how long).
- `Δmono ≈ real` → monotonic counts suspended time; expect a near-zero dip in `aws.lambda.microvm.enhanced.cpu.usage` at the first point after resume ❌ (cosmetic). `Δmono ≈ 5s` → paused ✅.

### Q5: all runs
- More `suspend/terminate — flushing telemetry` lines than actual suspends/terminates → AWS retried the hook. Matching extra `aws.lambda.enhanced.microvm.*` counts → double count ❌.

### H1: run2
- Last `mvmtest.seq` value in Datadog ≈ last tick `seq` ÷ 10 printed in CloudWatch before `terminate — flushing telemetry` → ✅.
- Datadog stops 10s+ earlier although CloudWatch shows `flush complete` → ❌ the async metrics POST was cut off.

### H2: run2
- `trace.mvmtest.tick.hits` (10s rollup) stays at about 10 per bucket right up to the terminate → ✅.
- Spans for the final ~10s visible in APM Traces, but the hits series stops ~10s early → ❌ the open stats bucket was never flushed.

### R1: run4 (suspend right after resume)
Question: when `/suspend` arrives seconds after a resume, its forced trace flush may be the first
send since the platform killed the outbound connections. Does it still deliver within the 1.5s
flush budget? The VM is then terminated while suspended, so nothing can be sent later.

Run with serverless-init built from `tianning.li/microvm-suspend-flush-all` (both fixes).
`./verify.sh run4` uses one boto3 client, polls for `RUNNING` every 100ms after resume, and
immediately requests suspension. This removes repeated CLI/credential startup from the
critical window. The checker still requires the actual CloudWatch hook gap to be <=3s;
AWS control-plane delays can still leave that target unexercised.

Rerun only this scenario after the full suite (which restores the plain long image):

```bash
./verify.sh run4
sleep 180
./verify.sh logs long 10m
./verify.sh check run4
```

The lifecycle diagnostics also work on old runs without redeploying:

```bash
./verify.sh check run2 microvm-8bc61c74-826f-3c7c-8e85-a5d37eeaa7bd
```

Offline runner/checker checks: `python3 -m unittest discover -p test_runner.py -v`.

- CloudWatch (`./verify.sh logs long 10m`): note the gap between `MicroVM lifecycle: resume` and
  the second `suspend — flushing telemetry` (target ≤ ~3s). The second suspend must log
  `flush complete`, not `flush timed out after 1.5s`.
- Datadog: `sum:trace.mvmtest.tick.hits{service:sample-verify-app,version:long}.as_count()`
  (10s rollup). Expect about one hit per second for the short window between the resume and the
  second suspend. The last <1s may be missing (still in the tracer's own send buffer).
- Logs: every tick in that window arrived (`message:tick`, `lambda_microvm_id:<id>`).

| You see | Meaning |
|---|---|
| `flush complete` and the window's hits present | ✅ No connection gap after resume: the dead connections were already evicted, and the redial fit the budget |
| `flush timed out`, or hits for the window missing | ❌ The first send after resume didn't make it; investigate before deciding on a fix |

Note: the trace agent's 3s periodic flush likely fires right at the resume (the monotonic clock
jumped by the suspend duration), so it may redial before `/suspend` arrives. Either way, the
result shows whether stats from that window get out.

### R2: trace stats bucket overwritten by a second, partial point (`SPANS_PER_SEC=200`)
> Update 2026-10-29: #58958 now calls only `Stop()` on `/terminate` (no `FlushAll()` first), so
> the `/terminate` case below no longer applies; `FlushAll()` is used on `/suspend` only (#58960).
> At 200 spans/s, run2 with `Stop()` only (`microvm-53bd23e1-…`) got 970 of 974 burst spans in the
> `/terminate` bucket, flushed in 75 ms.

Question (CI review on #58958): `FlushAll()` forces the open stats bucket out while trace
payloads are still being processed. Spans processed after it re-create that bucket, and a later
flush sends a second, smaller point for the same bucket. Does the backend replace the first
point with it?
- `/terminate` (run2): spans processed between `FlushAll()` and the end of `Stop()`.
- `/suspend` (run1, first suspend, which is resumed): spans processed between `FlushAll()` and
  the freeze, sent after the resume.

At 1 span/s (earlier runs) a payload almost never lands in that window. At 200 spans/s, with the
tracer flushing every 100ms, one almost always does.

Run with the #58960 tip binary (both fixes), deployed with the burst on:
```bash
SPANS_PER_SEC=200 ./verify.sh deploy long
./verify.sh run2
./verify.sh spans long 10m      # ground truth: spans created per 10s bucket
./verify.sh run1
./verify.sh spans long 15m
```
Datadog: `sum:trace.mvmtest.burst.hits{service:sample-verify-app,version:long}.as_count()`
(10s rollup), compared bucket by bucket with `./verify.sh spans`.

| You see | Meaning |
|---|---|
| Hits ≈ spans (±1 second's worth at bucket edges) in every bucket, including the terminate bucket and the first suspend's bucket | ✅ No overwrite (no late spans, or the backend adds the points). Reply on the PR with the numbers |
| Hits far below spans in the terminate bucket (run2) | ❌ #58958: the second point replaced the first. Revert to `Stop()` only |
| Hits far below spans in the first suspend's bucket (run1) | ❌ #58960: same, after a resume |
| The last ~100ms of spans before the terminate missing | Expected: still in the tracer's buffer |

### R3: forwarded hooks (run5, run6, image `fwd`)
Question (#58958 review, `discussion_r4414873382`): `flushAll(ctx, bool)` was split into
`flushForSuspend` and `flushAndStop`, and each handler now passes its own flush function;
`flushParallel` / `flushSequential` only decide when it runs. run1–run4 cover the standalone path
(`dispatchHook`). run5 and run6 cover the forwarder path (`handleWithForwarder`), which no other run uses:
- run5: forwarded `/suspend` that is resumed, then forwarded `/terminate` while RUNNING (#58958 path).
- run6: forwarded `/suspend` as the last hook, VM terminated while SUSPENDED (#58960 path).

Run with a binary built from the refactored branch:
```bash
SERVERLESS_INIT_BIN=... ./verify.sh deploy fwd
./verify.sh run5 && ./verify.sh check run5
./verify.sh run6 && ./verify.sh check run6
```

| You see | Meaning |
|---|---|
| `fwd` PASS, `nostop` PASS, `trace` PASS in the buckets after the resume | ✅ `/suspend` (forwarded) flushed without stopping the trace agent |
| `stop` PASS, `fwd-term` PASS | ✅ `/terminate` (forwarded) flushed after the app answered and stopped the agent |
| run6: `trace` PASS on the final `/suspend` bucket | ✅ the forwarded `/suspend` force-flushed trace stats (#58960) |
| run6: `fwd-susp` KNOWN | The app's own `/suspend` span or log lost the race with the parallel flush; documented `flushParallel` limitation, not a regression |
| `fwd` FAIL | Forwarding is off (env vars missing from the image); the other R3 checks are skipped |
| `nostop` FAIL, or `trace` FAIL in every bucket after the resume | ❌ a `/suspend` stopped the trace agent |
| `fwd-term` FAIL (log or span lost) | ❌ `/terminate`'s flush ran before the app's telemetry reached the agents |
| `fwd-term` WARN (counter only) | Not a refactor regression. The counter was still queued in DogStatsD (listener buffer / `packetsIn`) when the flush started. This measures that ms-wide gap; only fix it if it shows up |

## Results

| ID | ✅ / ❌ / ? | Evidence |
|---|---|---|
| Q1 | | |
| Q2 | | |
| Q3 | | |
| Q4 | | Δwall= Δmono= Δuptime= real= |
| Q5 | | |
| H1 | | |
| H2 | | |
| R1 | ✅ traces/logs; ❌ metrics (known) | VM `microvm-ba06162c-…`, 2026-10-27. Resume→suspend gap = 6s (05:33:52 → 05:33:58; ≤3s not reachable through the API). Second suspend: `flush complete`, same second. Trace hits in window = 5 (~5.7s window, 1 span/s). Tick logs 58, 0 missing. Metrics: no `mvmtest.count` point for the 05:33:50 bucket and no `resume` or second `suspend` lifecycle metric; that's the known open-bucket gap (aggregator follow-up). |
| R2 | ✅ second point sent, but summed | 2026-10-27, image 7.0, binary `a60c66f1d4e2` (#58960 tip), 200 spans/s. Debug logs show a second point for the same bucket on both paths: run2 (`microvm-34e70669-…`) `/terminate` flushed bucket 14:25:10 in `FlushAll()` at 13.444 and again in `Stop()` at 13.581 (1 entry); run1 (`microvm-68bb99d5-…`) first `/suspend` flushed bucket 14:26:30 at 14:26:31.441 and again right after the resume at 14:27:11.923 (2 entries). Datadog: terminate bucket 697 burst hits for ~700 spans (588 logged + the ~0.6s before terminate, never logged), tick 4; suspend bucket 305 + 2 moved into 14:27:00 = 307 spans, tick 2. Every other bucket matches exactly. The backend adds the second point to the first, so no loss. Replied on the #58958 [P1] thread (`discussion_r4390158684`). Image-build VM buckets (14:23:30–14:23:50) have no hits: unrelated, not investigated. |
| R3 | | Not run yet. Needs a binary built from the refactored branch. |
