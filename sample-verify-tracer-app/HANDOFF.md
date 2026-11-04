# Tracer verification: RSS query investigation handoff

## November 4 cleanup-error coverage update

The sample now emits schema 5 and requires the #21429 `f7e1792cdd` cleanup-error
probes during initial `/verify`. See the README's cleanup-error section for the
cases and fault-injection boundaries. Reconfiguration must finish after a retired
writer flush/stop failure; retired-only failures must not prevent tracer shutdown
cleanup; active-writer and replacement-construction errors must still propagate.
The local non-MicroVM control preserves its previous exception behavior.

Host regressions passed 73 tests. The ARM64 local workflow passed 125/125 checks
in `results/20261104T175602Z-31610`; replacing only the processor with the prior
`522078478d` module failed the intended new probes. Evidence and wheel hashes are
in that directory. The wheel's processor source matches `f7e1792cdd` exactly.
The schema-5 AWS run `results/20261104T180028Z-34388` passed 177/177 active checks
on image version 8.0, including the cleanup-error probes, three real suspend/resume
cycles, and final termination. The deployed archive matches the local build inputs;
replaying the current evaluator against captured backend responses reproduces the
saved evaluation. The standard RSS backend assertion remains explicitly skipped;
guest RSS emission/identity checks passed. Historical schemas explicitly report
that they lack the new cleanup-error coverage.

## Current state

The expanded suite passed on real AWS in
[`results/20261101T023314Z-31844`](results/20261101T023314Z-31844/inspection-summary.json),
image version 4.0: **56/56 active checks, 39/39 guest checks, one intentional RSS
backend skip**. All six common-feature traces and the expected error arrived in
Datadog. Recomputing the 18 feature assertions over 35 exported feature spans
also passed. The run hook took 0.263 seconds, the workflow took 6 minutes 40
seconds, and the VM was terminated.

The six feature traces cover manual spans and log correlation, exceptions, HTTP
propagation, SQLite, async tasks, and thread-pool context. New deployments use
`verification_schema=2` and require their guest assertions and Datadog trace/error
metrics. See [README.md](README.md).

At the user's request, `datadog_runtime_metrics_new_identity` is now **explicitly
skipped by default**, pending this backend investigation. It can be restored with
`./run-all.sh --check-runtime-metrics` or `check.py --check-runtime-metrics`.
Guest RSS emission/identity checks remain active. The historical results below
have not been rewritten.

The earlier AWS MicroVM run `results/20261101T000340Z-71289` passed all 21 guest checks for the combined
scenario covering dd-trace-py PRs #20955, #21429, #20956, and #20957. Its formal
evaluation remains **30/31, FAIL**. The remaining check is
`datadog_runtime_metrics_new_identity`: querying `runtime.python.mem.rss` by name
returns no data for the refreshed runtime ID.

The standard RSS sample reached Datadog and is stored in its distribution
aggregates. Direct queries of those aggregates return the expected value and
runtime ID. The failure is in the query path for the standard metric name; the
exact backend cause and correction are still unconfirmed.

No tracer fix, backend configuration change, or cache clearing was performed in
this investigation. No escalation message or ticket has been sent.

## Application and run

The application uses **dd-trace-py**, installed from a local Python wheel. The
original request's reference to dd-trace-js was a naming mistake.

| Item | Value |
| --- | --- |
| Application directory | `/Users/tianning.li/DataDog/lambda-microvm-poc/sample-verify-tracer-app` |
| RSS investigation evidence directory | `results/20261101T000340Z-71289` |
| Datadog organization | Datadog Serverless, **447397** |
| Datadog site | `us1.prod.dog` / `datadoghq.com` |
| Service | `sample-verify-tracer-app` |
| Verification/version ID | `20261101T000340Z-71289` |
| Version tag in metric queries | `20261101t000340z-71289` |
| Old runtime ID | `078b5b7c0e2f48dc96d61a213150919e` |
| New runtime ID | `573be720fac04e4eb32a97e3f48ff793` |
| Image version | `3.0` |
| Run hook duration | `0.288882462` seconds |
| Query window | `2026-11-01T00:03:06Z` to `2026-11-01T00:08:37Z` |
| Sample timestamp | `2026-11-01T00:06:30Z` (`1793491590000` milliseconds) |
| RSS value | `63414272` bytes |

Wheel source:
`/Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl`.
Its SHA-256 is
`89b3701fe78aedf631b4a2d5bd768d5e0cd37608d0ffa7a0a4c0f62d4a07c74d`.

The application copies serverless-init from
`/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64`.
The binary's SHA-256 is
`459e0c16ba1bced7a9266fcff40f1f105a3b7b8744ab40501dc36e4fa4ad4a89`.

The latest run used:

```sh
VERIFY_RUNTIME_RSS_ALIAS=mvmtest.runtime_rss_control ./run-all.sh
```

The UDP relay forwards the original packet unchanged and sends a diagnostic copy
with only the metric name changed. The copy preserves the value, distribution
type, and tags. Alias success does not satisfy the required standard-name check.

## Confirmed evidence

Both metrics were queried with this filter:

```text
service:sample-verify-tracer-app,runtime-id:573be720fac04e4eb32a97e3f48ff793
```

| Query/result | Standard RSS | Control metric |
| --- | --- | --- |
| Ordinary `avg:` name query | No series | `63414272` |
| Internal distribution `.sum` | `63414272` | `63414272` |
| Internal distribution `.cnt` | `1` | `1` |
| Internal distribution `.min` / `.max` | `63414272` | `63414272` |

For example, this query returns the standard RSS sample:

```text
sum:n_o_i_n_d_e_x.summary_aggr.1001720055.sum{service:sample-verify-tracer-app,runtime-id:573be720fac04e4eb32a97e3f48ff793}
```

This query returns no series over the same window:

```text
avg:runtime.python.mem.rss{service:sample-verify-tracer-app,runtime-id:573be720fac04e4eb32a97e3f48ff793}
```

Standard-name `min:`, `max:`, `sum:`, and `count:` queries also returned no data.
Explicit rollups of 10 and 60 seconds and grouping by `runtime-id` did not restore
the sample. These query variations are not an explicit cache bypass.

Fresh trace arrival, stale trace absence, the verification log, and the matching
CloudWatch record passed. The tracer captured the original RSS distribution
packet with the new runtime ID before forwarding it to serverless-init.

## Configuration comparison

| Field | Standard RSS | Control |
| --- | --- | --- |
| Metric name | `runtime.python.mem.rss` | `mvmtest.runtime_rss_control` |
| Metric key | `3224180373` | `66031691924` |
| Org summary key | `1001720055` | `2085643555` |
| Aggregation mode | `all_groups` | `all_groups` |
| Summary type | `default` | `default` |
| Source | `intake_serverless` | `intake_serverless` |
| Groups | Empty | Empty |
| Groups negated | `false` | `false` |
| Percentiles enabled | `false` | `false` |
| Deleted timestamp | Absent | Absent |

An authenticated `GetSummaryAggrByOrgMetrics` request for **org 0**, using both
metric keys, returned no rows. No global override was found through that lookup.

RSS `FullMetadataByKeys` returned `metric_type=gauge`, `interval=10`,
`integration=python`, and **`org_id=-1`**. This appears to be catalog/global
fallback metadata, not an org 447397 gauge override. The control returned no
metadata row. The public metadata API reports `distribution` for both names.
These differences do not establish a metric-type conflict.

An earlier collector incorrectly used org **1381**, inferred from the documented
`dd-auth` domain mapping. That export is retained for audit and must not be used
to diagnose org 447397. The active API organization and the Metrics Admin UI were
subsequently verified as 447397. Confirm credential scope rather than inferring
the organization from `DD_AUTH_DOMAIN`.

## Backend code lead and access gap

The inspected local backend checkout is `/Users/tianning.li/dd/dd-go`, commit
`16265fe9f2a4b3e668fd4c2012b2321898524eae`, dated **2025-07-29**. It is historical
source; its behavior has not been verified against the deployed version.

In that checkout:

- `model/summary_aggr_service.go`, `GetSummaryAggrMetrics`, selects rows where
  `enabled IS TRUE` and `deleted_at IS NULL`, checking the requested organization
  and org 0. Org 0 takes precedence if both exist.
- `GetByOrgMetrics`, used by the inspected HMS/admin lookup path, omits `enabled`
  from both the selected fields and the predicates. The collected admin response
  also does not expose it.
- `apps/metrics-querier/internal/planner/distributions.go` rewrites distribution
  queries to the synthetic summary series, including sum/count for averages.
- `.github/CODEOWNERS` assigns `/apps/metrics-querier/` to
  `@DataDog/metrics-query`.

**An incorrect `enabled` value is a candidate, not a confirmed cause.** The next
read must inspect that flag and the actual query plan/cache behavior.

The authenticated Metrics Admin browser session supported read-only RPC exports.
The current SupportDog UI did not expose the documented Query Metrics page in
this session, and the linked EXPLAIN prototype returned HTTP 404. No connected
tool exposes the live planner; local `kubectl` and `dogq` executables were absent.
The next steps require an existing authorized metrics-db replica and
metrics-querier diagnostic environment in us1 production.

## Next actions

1. Run [inspect-summary-aggr.sql](results/20261101T000340Z-71289/inspect-summary-aggr.sql)
   against a **metrics-db read replica**. It uses a read-only transaction and
   inspects both metric keys in orgs 447397 and 0, including `enabled` and
   `deleted_at`. Save the returned rows.
2. Copy [collect-backend-query-evidence.sh](results/20261101T000340Z-71289/collect-backend-query-evidence.sh)
   into an existing authorized us1 production metrics-querier diagnostic
   environment and run:

   ```sh
   bash collect-backend-query-evidence.sh rss-query-diagnostics
   ```

   The script checks the current CLI's help before issuing six bounded queries:
   RSS and control, each with normal caches, result cache bypassed, and both
   result and planner caches bypassed. It saves debug output and trace links.
   `METRICS_QUERIER_BIN` can select an existing binary. Shell syntax and mocked
   execution passed; live backend execution is still pending.
3. Compare resolved metric/summary keys, synthetic aggregate keys, time bounds,
   query-source handling, and metadata rewrites. If bypassing a cache restores
   data, identify which cache contains the incorrect state. If `enabled=false`,
   confirm how the current deployed planner handles that row and why it is set.
4. Have the responsible operator apply the specific correction after the cause
   is established. No production correction has been selected yet. Restore the
   standard-name assertion for validation with `--check-runtime-metrics`; the
   alias and internal aggregate queries are diagnostic evidence.
5. Re-evaluate the existing AWS evidence after the correction, preserving the
   original evaluation:

   ```sh
   cd /Users/tianning.li/DataDog/lambda-microvm-poc/sample-verify-tracer-app
   dd-auth --domain "${DD_AUTH_DOMAIN:-dddev.datadoghq.com}" -- \
     python3 check.py results/20261101T000340Z-71289 --check-runtime-metrics \
       --output results/20261101T000340Z-71289/evaluation-after-backend-fix.json
   ```

   The credentials must query org 447397. Once the standard RSS query passes,
   run a fresh `run-all.sh` for final end-to-end validation. Deployment requires
   exported `DD_API_KEY`; evaluation obtains API/app credentials through
   `dd-auth`. See [README.md](README.md) for the full AWS workflow.

## Evidence and routing

All evidence below is under `results/20261101T000340Z-71289/`:

| File | Purpose |
| --- | --- |
| [evaluation.json](results/20261101T000340Z-71289/evaluation.json) | Original formal 30/31 result; preserved |
| [metrics-admin-inspection-org447397.json](results/20261101T000340Z-71289/metrics-admin-inspection-org447397.json) | Decoded configuration and stored-point comparison |
| [metrics-admin-rss-config-447397.json](results/20261101T000340Z-71289/metrics-admin-rss-config-447397.json) | Original org 447397 export |
| [metrics-admin-global-summary.json](results/20261101T000340Z-71289/metrics-admin-global-summary.json) | Empty global-row lookup |
| [summary-aggregate-queries.json](results/20261101T000340Z-71289/summary-aggregate-queries.json) | Initial direct aggregate queries |
| [summary-aggregate-query-followup.json](results/20261101T000340Z-71289/summary-aggregate-query-followup.json) | Correct `.cnt` suffix and public-name comparisons |
| [query-shape-diagnostics.json](results/20261101T000340Z-71289/query-shape-diagnostics.json) | Rollup/grouping variations and working control |
| [backend-planner-followup.json](results/20261101T000340Z-71289/backend-planner-followup.json) | Additional findings, source version, and access gap |
| [query-summary-aggregates.py](results/20261101T000340Z-71289/query-summary-aggregates.py) | Reproduce the stored-aggregate comparison with API/app credentials |
| [metrics-query-escalation.txt](results/20261101T000340Z-71289/metrics-query-escalation.txt) | Unsent escalation draft |

The documented support entry point is `#support-metrics`; the local codeowner is
`@DataDog/metrics-query`. No individual owner was verified through the available
responsibility lookup.

Relevant internal references:

- [Investigate a Distribution Metric Configuration](https://datadoghq.atlassian.net/wiki/spaces/METRICSIN/pages/3107487966/Investigate+a+Distribution+Metric+Configuration)
- [Metrics metadata](https://datadoghq.atlassian.net/wiki/spaces/metricssto/pages/5435623139/Metrics+metadata)
- [Query Planner](https://datadoghq.atlassian.net/wiki/spaces/ME/pages/1601801295/Query+Planner)
- [Debugging Metric Caching Issues](https://datadoghq.atlassian.net/wiki/spaces/TS/pages/348620184/How-To+Troubleshoot+Debugging+Metric+Caching+Issues)

The sample application and results are local work products; the results directory
is ignored by Git. Preserve the evidence directory alongside this document when
moving the investigation to another machine or session.
