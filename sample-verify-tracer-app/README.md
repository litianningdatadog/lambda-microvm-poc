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
   the script runs the original `/verify`, native automatic export, and intake
   failure/recovery checks. It never synthesizes `/run` on the running VM.
3. Performs three actual AWS suspend/resume cycles, waiting for `SUSPENDED` and
   `RUNNING`. Each resume reruns the common feature suite and automatic export.
4. Arms the final termination trace/log, terminates the VM, and waits for termination.
5. Waits 180 seconds for ingestion, captures CloudWatch logs, and evaluates
   Datadog trace metrics and logs under `dd-auth`. The known failing standard RSS
   backend assertion is explicitly skipped by default.
6. Writes numbered logs, deployment/VM/core verification JSON, delivery evidence,
   `verification-resumed-{1,2,3}.json`, `automatic-resumed-{1,2,3}.json`,
   `termination-armed.json`, CloudWatch/backend evidence, and a summary under
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

Run the regression tests with `make test`; they need no credentials. The wire
tests require `msgpack==1.1.2` in the host Python environment (use a virtualenv).
With uv, run `uv run --no-project --with msgpack==1.1.2 make test`.
The pinned native wheel is exercised separately inside ARM64 Docker.

New deployments use schema 5. Every #21429 cleanup-error, #20956, common-feature, and delivery phase
is required; a missing phase fails even if the guest reports `passed=true`.
Historical schema 3 still requires its subscriber/native telemetry checks;
schema 2 requires its original five telemetry checks. Results below schema 4
explicitly lack the four new delivery scenarios; re-evaluation cannot add coverage.
Results below schema 5 explicitly skip the cleanup-error controls described below.

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
subscriptions stay active. The retired trace writer's periodic thread is also
paused with its pinned fork-stop primitive, without flushing/shutting down its
exporter. Its replacement keeps the native one-second interval. This preserves
the seeded buffered trace without a process-wide 3600-second interval override.
A repeated `/ready` is idempotent.

After restore, Flask instrumentation must refresh identity before the forwarded
`/run` handler executes. An outer WSGI wrapper measures that request. `/verify`
checks the refreshed state, does span work while retirement is held, releases
retirement, finishes the stale span and a retained-context child, then emits a
fresh trace, telemetry marker, runtime metrics, and a structured result log.
The app never calls `refresh_identity()` directly.

| PR | Guest evidence |
| --- | --- |
| [#20955](https://github.com/DataDog/dd-trace-py/pull/20955) | Old writer discarded; buffered, open, and retained-context spans absent from captured payloads; fresh trace uses the new runtime ID. |
| [#21429](https://github.com/DataDog/dd-trace-py/pull/21429) | `/run` and span work finish in under one second while retirement is held; retirement finishes after release; cleanup errors allow configuration/shutdown to complete, with active-writer errors preserved. |
| [#20956](https://github.com/DataDog/dd-trace-py/pull/20956) | Worker and identity replaced; both obsolete writer callbacks absent; current callback registered exactly once; telemetry marker and native exporter request/response metrics emitted under the new ID. |
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

The drain pause verifies Python lock ownership. Separate local controls below
exercise a native HTTP send held by a controlled intake. Private probes intentionally match this wheel. Old-ID telemetry
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

For schema 4, `check.py` independently evaluates exported feature payloads from
the initial restore and all three resumes. Roots must be distinct between
samples. It requires at least four APM hits for each of the six operations,
four errors for `verify.feature.error`, and matching structured feature logs in
both CloudWatch and Datadog. A missing sample, feature, error count, or log fails.
HTTP checks count distinct span IDs: retransmitting the same span is not duplicate
instrumentation, but additional client/server spans for one request fail. This
new assertion is required for schema 4, not retroactively for schema 2/3 evidence.

Dependencies are local to the guest; the HTTP test does not call a public
service. The suite covers one restored VM and its repeated lifecycle. It does
not cover cross-VM isolation, load testing, or every tracer integration. See
[HANDOFF.md](HANDOFF.md) for the separate RSS backend investigation and preserved
historical evidence.

## Delivery and repeated lifecycle scenarios

| Scenario | Required evidence |
| --- | --- |
| Automatic export | A new measured root reaches the intake using the real `NativeWriter` default **one-second** timer. Harness explicit-flush count does not change. Runs initially and after each of three resumes. |
| Intake outage/recovery | Independently inject trace-only HTTP 503 and connection disconnects; offer 24 independent roots per case; observe an actual failed attempt and three successful `/health` probes while the fault is active; restore the intake and require a new root's completed acknowledgement. |
| Repeated suspend/resume | Three ordered AWS `SUSPENDED` → `RUNNING` transitions; the same runtime ID; exact run/suspend/resume counters; fresh exported feature spans and a working automatic exporter after every cycle. |
| Final termination batch | Arming creates no span. The actual terminate hook creates a final measured root and correlated marker log, waits for native export without an explicit flush, and returns so serverless-init can flush its final batch. Require one hook, one marker/root, and matching CloudWatch/Datadog result and marker logs. |

`POST /delivery/automatic`, `/delivery/recovery`, and `/delivery/termination`
drive the phases; the last endpoint only arms termination. `POST /verify/features`
reruns common features without replaying the one-shot identity/drain setup.
Automatic/final phases have 10-second deadlines; each outage/recovery case has a
20-second deadline. The platform terminate hook allows 30 seconds and the
serverless-init app-forwarding timeout is 15 seconds. The terminate hook is not
a promise that the platform sends SIGTERM to the application.

Delivery evidence records request IDs, operation/marker/runtime tags, timing,
HTTP outcome, response completion, and whether original bytes were forwarded to
the Agent. A decoded request, 503, disconnect, partial upstream body, or failed
trace response write is never an acknowledgement. AWS evaluation requires
actual Agent forwarding, plus backend hits for all four delivery operations
(at least four for automatic export) and the final correlated logs.

Following the AWS-tested JS evaluator, dotted fields such as `verify.marker`,
`_dd.measured`, and `dd.trace_id` may also use Datadog's nested object form.
Resumed feature logs and final results compare those representations without
dropping nulls or unrelated values. Final backend results are independently
validated for the exact marker, measured root, service, span IDs, and correlation.
Explicit-flush counters must also remain continuous between automatic export
and recovery. JavaScript-only absent Remote Config IDs are not a Python rule.
For backend logs only, the evaluator restores omitted `features.errors` and
`evidence.unexpected_errors` arrays when the corresponding guest/CloudWatch
reference explicitly contains `[]`. Explicit nulls, non-empty errors, missing
parent objects, and other missing fields still fail; guest and CloudWatch
validation is unchanged.

Recovery verifies **bounded offered work**, not global memory/backpressure or
native queue-depth limits. It does not promise lossless delivery during an outage.
Expected native exporter error messages during fault injection are retained.
Non-trace response cancellations during teardown (observed for `/info`) are
retained under `cancelled_responses`; trace response and Agent upstream failures
remain failures. Native integer IDs/timestamps in wire log evidence are serialized
as strings when needed to avoid precision loss in JSON log pipelines.

## Optional local smoke test

```sh
./run-local.sh
```

The credential-free Docker workflow builds the same ARM64 image and overrides
serverless-init with `ddtrace-run`. It exercises the native lifecycle controls,
initial refresh, automatic export, both outages, three synthetic hook cycles
with fresh features/automatic export, and the final hook. It saves every phase
plus `vm-local.json` and cleans up. Its default port is 18080; `PORT` and `IMAGE`
override the local port and container/image name.

Forwarding is disabled. These results prove local native-tracer behavior, not
Firecracker freeze/thaw, real Agent final flushing, or Datadog ingestion.

Individual local steps remain `make build`, `make start PORT=18080`,
`make check PORT=18080`, and `make stop`. `make package` copies both artifacts
and produces an archive for manual deployment.

## Subscription and blocked-send controls

After rebuilding the wheel from the reviewed tracer revision:

```sh
make build
make check-lifecycle
```

`run-local.sh` runs these controls before the full local restore smoke test.
Each control starts a fresh process and uses real native workers/exporters with
an in-process HTTP intake. Nothing is forwarded to Datadog.

- `regular` runs without MicroVM detection, concurrently shuts down two writers,
  rebuilds telemetry through the test-session API, and checks that the surviving
  exporter receives the replacement native handle. Stopped writers retain their
  weak subscriptions until collection; pending-worker locks must be absent.
- `blocked-send` enables MicroVM detection, holds a real native trace HTTP send
  at the intake, and explicitly refreshes identity while it is blocked. Refresh
  must return in under one second, leaving the replacement worker pending. After
  release, the next send must apply that handle and emit native `trace_api.*`
  request/response metrics under the new runtime ID. This is a local lifecycle
  control; the AWS scenario continues to rely exclusively on platform hooks.
- `retirement_control.py` runs once with MicroVM detection enabled and once
  without it. These controls cover the cleanup-error fixes in
  [`f7e1792cdd`](https://github.com/DataDog/dd-trace-py/commit/f7e1792cdd034e788c9e84765600d7056acd9cf0).

### Cleanup-error controls for #21429

During the initial `/verify`, dedicated tracer instances exercise the following
cases without shutting down the application tracer or calling `refresh_identity()`.
Native writers, services, processors, and HTTP exports are real; only the named
failure operation is injected. Expected exception warnings are retained. The two
cleanup warning messages have rate limiting disabled only while captured, so
repeated probes can check each warning and its original exception.

| Case | Required evidence |
| --- | --- |
| Retired `flush_queue()` fails | Configuration returns; writer, span processors, context provider, and user processor are updated; a tagged trace reaches the intake with the current runtime ID; the old writer is cleaned up. |
| Retired `stop()` fails | The same configuration/export assertions pass; the failed retirement stays reachable and shutdown retries it successfully. |
| Two retired writers keep failing | Shutdown attempts both and the active writer, logs both original errors, then disables the tracer, clears its executor/context, replaces `start_span`, and unregisters its exit/fork hooks. A subsequent retry cleans up both workers. |
| Active writer also fails | Both retirements are still attempted/logged, but the original active-writer exception propagates. Removing the injected faults permits complete cleanup. |
| Replacement construction fails | The original error propagates and the installed writer, processors, context provider, and retirement registry remain unchanged. |

The local non-MicroVM control requires flush/stop errors during configuration
and active-writer errors during shutdown to continue propagating. It is run in
a separate process because MicroVM detection happens at tracer initialization.

Schema 5 stores per-case `retirements` evidence in `verification.json` and the
structured result log. Both the local driver and AWS evaluator recompute the
five required case verdicts; a missing case/field or a false result cannot be
hidden by `passed=true`. Old result files remain readable and explicitly report
their missing coverage. Existing real snapshot, suspend/resume, and final-batch
scenarios remain part of the workflow. These failure probes do not cover every
native shutdown/downgrade race or measure throughput.

The restored guest inspects weak callback membership directly, including the
writer retired by `configure()` and the writer active immediately before `/run`.
Exporter telemetry checks require the `src_library:libdatadog` tag and parse
both direct metric messages and message batches. A Python marker, an old runtime
ID, or a bookkeeping flag alone cannot satisfy them.

Wheel hashes identify the tested artifact; they do not prove its source revision.
Rebuild the wheel after tracer edits and retain the source commit alongside the
run's wheel hash. An older wheel with the same version string is insufficient.

## Verification of schema 5 changes

- Host regressions: **73 passed**, using the isolated uv command above.
- ARM64 AL2023 local workflow: **125/125** checks passed in
  `results/20261104T175602Z-31610`, including all four native lifecycle controls,
  initial restore probes, three synthetic hook cycles, and final termination.
- The tested wheel's processor source exactly matches `f7e1792cdd`; its SHA256
  is recorded in that run's `artifacts.txt`.
- Replacing only that processor module with its previous `522078478d` version
  makes both configuration failure cases and the retired-only shutdown case
  fail. The active-writer propagation and construction-failure guards still pass.
  This negative control is saved as `retirement-before-fix.json` and `.log` in
  the same run directory.
- Real AWS schema-5 run `results/20261104T180028Z-34388`, image version **8.0**:
  **177/177** active checks passed, including cleanup-error probes, three actual
  suspend/resume cycles, and final termination. The standard RSS backend assertion
  remains explicitly skipped; guest RSS emission/identity checks passed.
- The deployed archive matches all 14 local build inputs byte-for-byte. Replaying
  the current evaluator against the captured Datadog responses reproduces the
  saved evaluation exactly; this replay does not issue fresh backend queries.

## Verification of schema 4 changes

- ARM64 AL2023 `./run-local.sh` passed **119/119** checks in
  `results/20261102T052131Z-13534`, including both native lifecycle controls.
  Automatic export used the one-second native timer and the final hook exported
  with explicit-flush count unchanged. Final counters were
  run=1, suspend=3, resume=3, terminate=1.
- Regression suites passed, including real HTTP partial/rejected/disconnected
  responses, missing/malformed evidence, cancelled negotiation, optional native
  root `parent_id`, and cleanup after a failed second cycle.
  The 43 affected evaluator/feature/delivery regression tests passed after
  porting the JS log-normalization and duplicate-instrumentation checks.
- A controlled backend-fixture smoke exercised the full schema 4 evaluator with
  captured native payload shapes and Datadog dotted-key/numeric remapping.
  Capture-only evidence failed the Agent-forwarding gates. Explicit synthetic
  acknowledgement fixtures passed; wrong nested markers, measurements,
  services, IDs, and counter discontinuities still failed. These fixtures are
  not an AWS or Datadog ingestion test.
- AWS run `results/20261102T052643Z-18896` recorded **162/166** passing checks
  with one RSS backend check skipped. All four failures were omitted empty error
  arrays in the captured Datadog logs. After the backend-only normalization fix,
  offline evaluation of the unmodified saved responses passed **166/166**, with
  the same RSS check skipped. Null/non-empty errors, changed span IDs, and rejected
  acknowledgements still failed in negative controls. No new deployment or live
  Datadog query was performed for this re-evaluation.
- All **65** Python regression tests passed using Python 3.10 and `msgpack==1.1.2`,
  including backend empty-array omissions and rejection of malformed error fields.

