# Tracer verification sample

Verifies the Python tracer from the supplied CPython 3.12 / Linux ARM64 wheel
through a real AWS Lambda MicroVM snapshot restore and Datadog evaluation.
The wheel and four PRs concern dd-trace-py.

## Run on AWS

From this directory:

```sh
# Set DD_API_KEY in your environment for the target Datadog organization.
./run-all.sh
```

Requires `DD_API_KEY`, `aws`, `aws-vault`, `dd-auth`, `make`, `python3`, and `zip`.
Authenticate to the AWS profile and dd-auth organization before starting. Like
`sample-verify-app/run-all.sh`, the deployment key is supplied to serverless-init;
`dd-auth` injects `DD_API_KEY` and `DD_APP_KEY` into the evaluation process.
You do not need to export `DD_APP_KEY` yourself. The deployment key and the
`DD_AUTH_DOMAIN` credentials must belong to the same Datadog organization.

The script:

1. Copies the wheel and local serverless-init binary into the build context,
   packages them, uploads to S3, and creates or updates the ARM64 MicroVM image.
2. Waits for the build and runs a snapshot clone. The platform calls `/run` once;
   the script calls only `/verify`, never a synthetic `/run` on the running VM.
3. Saves guest assertions, terminates the VM, and waits for termination.
4. Waits 180 seconds for ingestion, captures CloudWatch logs, and evaluates
   Datadog trace metrics and logs under `dd-auth`. The known failing standard RSS
   backend assertion is explicitly skipped by default.
5. Writes numbered logs, `deployment.json`, `vm.json`, `verification.json`,
   `cloudwatch.json`, `evaluation.json`, and `summary.txt` under
   `results/<UTC timestamp>-<pid>/`. A failed step or assertion returns nonzero.

The exit trap terminates a created VM after failures too. Each VM also has a
600-second maximum lifetime. The image versions and S3 archives remain available
for inspection; those AWS resources incur storage costs until removed.
Deployment credentials are passed in an owner-only temporary file, and proxy
auth tokens stay in memory. `DD_APP_KEY` is never packaged into the guest.

To re-evaluate an existing run without deploying again or replacing its original
evaluation, run:

```sh
dd-auth --domain dddev.datadoghq.com -- python3 check.py \
  results/<run> --output results/<run>/evaluation-recheck.json
```

Metric queries use the lowercased version tag recorded by Datadog. Log queries
keep the original verification ID and match the result message, since the log
pipeline remaps `msg` into `message`.
`datadog_runtime_metrics_new_identity` is temporarily listed under `skipped`,
with a reason, in `evaluation.json` and the console summary. Its standard RSS
query is not issued by default. Guest RSS emission/identity checks remain required.
To restore the backend assertion for a new AWS run or an existing result:

```sh
./run-all.sh --check-runtime-metrics
dd-auth --domain dddev.datadoghq.com -- python3 check.py \
  results/<run> --check-runtime-metrics --output results/<run>/evaluation-strict.json
```

Run the evaluator regression tests with `make test`; they need no credentials.

### Environment overrides

| Variable | Default / purpose |
| --- | --- |
| `DD_API_KEY` | Required: deployment/ingestion key. |
| `DD_AUTH_DOMAIN` | `dddev.datadoghq.com`, used by dd-auth for evaluation. |
| `DD_SITE` | `datadoghq.com`, the ingestion site and default query site. |
| `DD_API_URL` | Optional explicit Datadog query API base URL. |
| `AWS_VAULT_PROFILE` | `sso-serverless-sandbox-account-admin`. |
| `REGION` | `us-east-2`. |
| `BUILD_ROLE_ARN` | `arn:aws:iam::425362996713:role/microvm-build-role`. |
| `EXECUTION_ROLE_ARN` | Same as the build role unless overridden. |
| `BASE_IMAGE_ARN` | Regional managed `al2023-1` image. |
| `IMAGE_NAME` | `sample-verify-tracer-app`. |
| `S3_BUCKET` | Same as `IMAGE_NAME`. Created if absent. |
| `WHEEL` | `/Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl`. |
| `SERVERLESS_INIT_BIN` | `/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64`. |
| `CHECK_DELAY` | `180` seconds for Datadog ingestion. |
| `VERIFY_RUNTIME_RSS_ALIAS` | Optional diagnostic metric name for a copy of the RSS packet. The original is still submitted; the standard-name backend assertion is opt-in for now. |
| `BUILD_TIMEOUT` | `1800` seconds for the image build. |
| `RESULTS_DIR` | Optional new results directory; existing directories are rejected. |

```sh
DD_AUTH_DOMAIN=dddev.datadoghq.com CHECK_DELAY=240 ./run-all.sh
```

The results directory's basename becomes `DD_VERSION` and the verification ID,
so checks are isolated from other runs. Use a unique basename when overriding it.
The script uses the same default AWS account and region as `sample-verify-app`.

## Scenario: identity refresh while a retired writer drains

The forwarded `/ready` hook opens a stale span, retains its context, buffers a
completed trace, accumulates CPU/GC activity, and holds a retiring writer's
`flush_queue()` at entry with a threading event. AWS snapshots that state. The
periodic runtime-metric thread is paused while its collectors and refresh
subscriptions stay active. A repeated `/ready` is idempotent.

After restore, Flask instrumentation must refresh identity before the forwarded
`/run` handler executes. An outer WSGI wrapper measures that request. `/verify`
checks the refreshed state, does span work while retirement is held, releases
retirement, finishes the stale span and a retained-context child, then emits a
fresh trace, telemetry marker, runtime metrics, and a structured result log.
The app never calls `refresh_identity()` directly.

| PR | Guest evidence |
| --- | --- |
| [#20955](https://github.com/DataDog/dd-trace-py/pull/20955) | Old writer discarded; buffered, open, and retained-context spans absent from captured payloads; fresh trace uses the new runtime ID. |
| [#21429](https://github.com/DataDog/dd-trace-py/pull/21429) | `/run` and span work finish in under one second while retirement is held; retirement finishes after release. |
| [#20956](https://github.com/DataDog/dd-trace-py/pull/20956) | Worker and identity replaced, old exporter unsubscribed, new exporter subscribed, telemetry marker emitted under the new ID. |
| [#20957](https://github.com/DataDog/dd-trace-py/pull/20957) | CPU, wall-clock, and GC baselines reseeded; collector identity updated; runtime metrics emitted with the new ID. |

The HTTP intake on 18126 records real v0.4 trace and telemetry payloads and
forwards their original bytes and headers to serverless-init on 8126. The UDP
intake on 18125 records runtime metrics and forwards packets to 8125. The AWS
workflow enables this with `VERIFY_FORWARD_TO_AGENT=true`.

Datadog evaluation requires the fresh trace's APM hit metric, no hit metrics for
the three seeded stale operations, feature trace hit/error metrics described
below, and a structured verification log matching the guest's assertions.
With `--check-runtime-metrics`, it also requires a positive standard RSS sample
tagged with the new runtime ID.
CloudWatch must contain the matching result in the tested VM's log stream.
Missing required fresh data, authorization errors, or missing logs fail evaluation.
Telemetry identity is checked in the captured native payload and guest evidence;
there is no direct query of Datadog's internal telemetry backend.

When runtime RSS is missing, `verification.json` now retains its exact DogStatsD
lines for the new runtime ID. The guest check requires the RSS metric itself,
not just any runtime metric. `evaluation.json` records whether Datadog's metric
tag inventory knows that runtime ID when the backend assertion is enabled;
tag presence alone does not pass delivery.

For a diagnostic run that distinguishes the standard metric name from the rest
of the delivery path:

```sh
VERIFY_RUNTIME_RSS_ALIAS=mvmtest.runtime_rss_control ./run-all.sh
```

The UDP relay sends the original RSS line unchanged, plus a copy with only its
metric name changed. Type, value, and tags (including runtime ID) are preserved.
The evaluator queries the diagnostic name when configured. With
`--check-runtime-metrics`, it also queries the standard name, and a successful
copy cannot satisfy that assertion. The alias is disabled by default.

Investigation of the first two AWS runs found the submitted runtime IDs in
Datadog's RSS tag inventory but no RSS time series. An isolated run with the same
serverless-init binary captured a valid outbound RSS sketch. A direct API control
also accepted RSS without returning a time series, while separately named
distribution and gauge controls were queryable. This isolates the current gap to
handling of the standard metric name; it does not establish a tracer refresh bug
or prove a particular backend cause. No organization metric configuration is changed.

The drain pause verifies Python lock ownership, not a native exporter stuck in
network retries. Private probes intentionally match this wheel. Old-ID telemetry
can still flush through the documented native `stop()` fallback. Native stats
worker cleanup, downgrade/shutdown races, blocked DogStatsD sends, suspend/resume,
and detailed GC pause/context-switch assertions remain future scenarios.
The one-second check is a liveness assertion, not a performance benchmark.

## Common features after restore

After the original refresh/drain scenario, `/verify` runs six independent traces
through the refreshed writer:

| Scenario | Required guest evidence |
| --- | --- |
| Manual spans and logs | Nested parent/child IDs, resource name, custom string tag and numeric metric, automatic logging trace/span IDs and service. |
| Exceptions | An expected `ValueError` exported with the error flag, type, message, and stack. |
| HTTP | Instrumented `requests` calls to the guest's own Flask endpoints; outgoing/incoming parent linkage; W3C `traceparent` propagation; HTTP 500 marked as an error on both sides; runtime identity stays unchanged across requests. |
| SQLite | Automatically instrumented `SELECT 42 AS answer` returning 42, attached to the correct parent trace. |
| Async tasks | Two overlapping tasks produce distinct child spans under the same root, retain their active spans across awaits, and restore the caller's active span. |
| Thread pool | Two overlapping workers inherit the caller's trace, use distinct active spans, and leave the caller's context intact. |

All six roots must be exported as independent traces with the new runtime ID and
the expected service. `verification.json` includes `features` and `feature_spans`
so parent IDs, errors, tags, and context assertions can be inspected directly.
Expected exception and HTTP 500 messages in the guest log are part of the tests.

For new deployments (`verification_schema=2`), `check.py` requires every feature
assertion to be present, queries APM hit metrics for all six operations, and
requires an error metric for `verify.feature.error`. A missing feature trace or
error metric fails evaluation. Re-evaluating older results reports the expanded
suite as skipped; it does not claim those new scenarios ran.

All dependencies are local to the guest; the HTTP test does not call a public
service. These scenarios cover one restored VM. They do not cover suspend/resume,
cross-VM isolation, load testing, or every tracer integration. See
[HANDOFF.md](HANDOFF.md) for the separate RSS backend investigation and preserved
historical evidence.

## Optional local smoke test

```sh
./run-local.sh
```

This preserves the earlier Docker workflow without credentials. It builds the
same image, overrides the serverless-init entrypoint with `ddtrace-run`, prepares
state, synthesizes `/run`, evaluates local payloads, and cleans up. Its default
port is 18080; `PORT` and `IMAGE` override the local port and container/image name.
Forwarding to Datadog is disabled in local mode. Results are local evidence only.

Individual local steps remain `make build`, `make start PORT=18080`,
`make check PORT=18080`, and `make stop`. `make package` copies both artifacts
and produces an archive for manual deployment.
