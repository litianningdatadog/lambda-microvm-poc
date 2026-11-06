# dd-trace-js MicroVM verification

An executable verifier for **dd-trace 6.15.0**, using the same Express/Pino stack
as `../sample-nodejs-app`. The Dockerfile copies `/datadog-init` from its pinned
`datadog/serverless-init` image to `/serverless-init`, as in
`../sample-verify-tracer-app`. No host init binary is required.

The scenarios target [dd-trace-js PR #9709](https://github.com/DataDog/dd-trace-js/pull/9709):
a Firecracker snapshot includes the tracer's cached random IDs and process
identity. Each clone needs fresh IDs, but resuming the *same* VM must preserve
its identity. Initializing two ordinary Node processes does not test that bug.

## Scenarios and required evidence

`POST /verify` returns 30 named feature assertions plus decoded native trace
payloads, metric lines, expected span relationships, and lifecycle state. New
deployments use schema 2: all five feature samples, automatic/recovery evidence,
and final termination evidence are required. Any false or missing assertion fails.

| Scenario | Assertion / evidence |
| --- | --- |
| Version and environment | Exactly 6.15.0; the tracer detects `AWS_LAMBDA_MICROVM_IMAGE_ARN` as a serverless MicroVM. The app never sets that variable itself. |
| Snapshot preparation | Forwarded `/ready` warms the ID cache, records the build-time runtime ID, exercises tracing/custom metrics, and flushes before returning. Repeated ready calls are idempotent. |
| Native `/run` refresh | The real tracer HTTP subscriber refreshes identity before the app's handler executes. The app only reads identity; it never calls refresh/reseed functions or publishes identity channels. |
| Two clones of one snapshot | Launch up to six candidates of the same image version and select the first pair with the same prepared build-time runtime ID. Require different runtime IDs after run. Compare 128 early application-created trace/span IDs plus eight feature roots per clone. Compare **low 64 trace-ID bits**, not just the timestamp-containing 128-bit ID. Detect duplicates within and across samples. |
| Repeated in-place suspend/resume | Three real AWS cycles with observed `SUSPENDED`/`RUNNING` states. Runtime ID stays unchanged, run count stays one, counters match each cycle, and all features work again with fresh IDs. Duplicate HTTP instrumentation is rejected; retransmitted identical span IDs are not mistaken for newly created spans. |
| Manual instrumentation | Independent root, child, and grandchild exported with exact parent IDs, resource names, string tags, and numeric metric `42.5`; active scope restored after nesting. |
| Rejected async callback | `tracer.trace()` records the expected thrown `Error`, including flag, type, message, and stack, without the app setting error tags. |
| Automatic HTTP instrumentation | Real `node:http` calls to Express; exported client/server ancestry and status codes. App code does not inject propagation headers. |
| W3C propagation | Actual `traceparent` header matches the outbound client span and full root trace ID; incoming Express span is a descendant of that client. |
| HTTP errors | Server 500 has exception details. Stock client error range is **400–499**: a 404 marks the client but not the server as an error; a 500 marks the server but not the client. No custom status validator. |
| Overlapping async tasks | Two tasks overlap across awaits, retain different active child spans, export correctly parented leaves, and restore the caller's span. |
| Concurrent HTTP requests | Four independent roots/request chains overlap without leaking trace or active-span context between them. |
| Pino correlation | Captured, automatically injected JSON trace/span IDs refer to the actual exported manual child; service matches. Handles 128-bit hexadecimal log trace IDs without losing low64 precision. |
| Runtime/custom metrics | Native RSS and custom `microvm.js.fresh:42` packets use the refreshed runtime ID and service. Real HTTP DogStatsD proxy transport, with UDP fallback capture. |
| Disabled serverless pipelines | Stock 6.15.0 forces instrumentation telemetry and Remote Config **off** in serverless mode, even if enabling flags are supplied. Require disabled resolved config, no RC client ID, and no outgoing telemetry/RC requests. |
| Delivery to Datadog | AWS mode forwards original payload bytes/headers to serverless-init and fails on unexpected relay errors. Backend evaluation requires at least five hits per feature operation, five expected-error counts, hits for the four delivery operations, all five structured feature results, and the final marker/result logs matching CloudWatch. |

One image version can contain multiple snapshot builds; matching image ARN/version
alone is not a shared-baseline guarantee. Candidate selection reads `GET /state`
without running feature scenarios. If no pair shares `before.runtimeId` within
six candidates, selection is **inconclusive**, the run fails, and every candidate
is cleaned up. The shared-baseline assertion is never skipped.

The Datadog log pipeline may omit null `rcClientId` fields. Log matching tolerates
only those omissions at the six known identity locations when the required
disabled-RC assertion matches. Runtime IDs, counters, all assertions, and unrelated
fields remain strict.

The pipeline may also expand dotted span fields into nested objects. Delivery
matching accepts both forms of `meta["verify.delivery.marker"]` and
`metrics["_dd.measured"]`, with unchanged marker and measured-value requirements.
An explicit dotted key takes precedence; a missing or incorrect value still fails.

Local-only negative controls also exercise `GET /run`, a lookalike `/run-extra`,
resume before run, a repeated `POST /run`, and an ordinary process without the
MicroVM environment variable. Only the first exact `POST /run` in MicroVM mode
may change runtime identity. These controls never run against deployed AWS VMs.

### What differs from the Python verifier

Do not transplant Python writer-retirement assertions into this app. The JS
6.15.0 implementation refreshes live identity/config subscribers; this verifier
does not claim buffered/open build-time traces are discarded or the trace writer
is replaced. It does not force-enable Remote Config or telemetry to exercise
otherwise-disabled paths. Profiling, crashtracking, OTel exporters, database
plugins, and worker-thread propagation are not part of this scenario set.

## Delivery scenarios (schema 2)

The tracer uses its **native default 2000ms export interval**. The previous
one-hour override is removed. Ready, feature verification, and suspend may still
use the existing explicit flush helper; the new delivery scenarios measure its
counter before/after and require **no change**. Normal exporter timer activity is
not counted as a test-triggered flush.

| Endpoint / transition | Required evidence |
| --- | --- |
| `POST /delivery/automatic` | Finish a uniquely marked independent trace and observe automatic export within 10 seconds, without an explicit flush. Require exact span/runtime/marker identity and a completed successful intake response. |
| `POST /delivery/recovery` | Exercise trace-only HTTP 503 and socket-disconnect faults sequentially. Each case offers 24 traces, issues three real loopback health probes during the outage, restores intake in `finally`, and exports a **new** marked trace successfully. No application retry loop is added. |
| Three AWS suspend/resume cycles | Save each control-plane transition and run all 30 feature assertions after each resume. Compare low64/span IDs across all five samples, preserve runtime identity, and require exact lifecycle counters. |
| `POST /delivery/termination`, then AWS termination | Arm an idempotent marker, without emitting its trace yet. The forwarded `/terminate` hook finishes that trace and logs inside it, observes normal automatic export, then returns. Require final native-hook evidence and backend arrival after the VM is gone. |

The intake retains every attempt in `traceRequests`, including rejected decoded
payloads. Only `outcome=accepted` with a completed 2xx response satisfies delivery.
AWS evaluation additionally requires `forwardToAgent=true` and `forwarded=true`:
capture-only local success cannot claim an Agent acknowledgement. Fault injection
affects only `/v0.4/traces`; metrics and application traffic remain available.

Recovery records health timings, encoder trace-count samples, failed attempts,
restoration time, fresh-span creation, and successful acknowledgements. Health
requests must finish in under one second; each recovery case has a 20-second
deadline. The encoder must start/drain at zero and stay within the finite offered
workload plus the fresh trace. This is an **encoder-queue check**, not a global
memory-pressure or in-flight-retry stress test. Failed payloads may be dropped or
retried according to the native SDK; lossless recovery is not asserted. Expected
tracer error messages during the injected outage are part of the test.

### Termination ordering

MicroVM `/terminate` does **not** guarantee a separate SIGTERM, and JS 6.15.0 does
not automatically flush the tracer merely because that HTTP hook arrives. The
app therefore waits for the **normal export timer**, without calling its explicit
flush helper, until the Agent acknowledges the final span. The
serverless-init lifecycle implementation forwards the hook and then sequentially
flushes its Agent pipelines before responding to AWS. This tests final delivery,
not an invented tracer shutdown callback.

The image sets `DD_AWS_MICROVM_FORWARD_TIMEOUT_MS=15000`; AWS runtime hooks allow
30 seconds. Automatic/final-span observation is limited to 10 seconds. Arming is
performed only after the selected pair's other checks pass. Unselected candidates
never emit a final marker.

Because the VM is destroyed, final evidence comes from `cloudwatch.json`:
`microvm_js_termination_log` and `microvm_js_termination_result`. Full evaluation
also requires matching Datadog records and the termination operation's APM hit.
Missing final records, changed marker/span IDs, or a failed acknowledgement fail
the run.


## Local smoke: no credentials

Requires Node.js 22+ and npm:

```sh
cd sample-verify-tracer-js-app
npm ci
npm run smoke
```

Runs the actual app/tracer in separate MicroVM-simulation and ordinary-process
children. Exercises native automatic export, both real intake faults and recovery,
three synthetic suspend/resume cycles, and the armed final hook, then stops both
children. Logs and JSON evidence are under `results/local-*/`. The local negative
control invokes an extra resume before run; cycle assertions compare the proper
counter deltas rather than pretending those are AWS counters.
Deployment API/app keys are removed from child environments; intake forwarding
is disabled. `LOCAL_PORT` overrides the default app port 18080; capture ports
18126/TCP and 18125/UDP must be free. `make test` additionally uses 8126/TCP for
a controlled upstream; do not run those wire tests alongside a live verifier.

**This is a lifecycle simulation, not snapshot, serverless-init, or Datadog
backend proof.** Local processes and containers cannot establish clone-safe
OpenSSL entropy. Only the two-clone AWS scenario checks that property.

### ARM64 AL2023 container

```sh
make build                       # resolves the pinned init image, installs pinned lockfile
make start PORT=18081             # capture-only; bypasses serverless-init
make check PORT=18081 OUTPUT=results/docker.json
make stop
```

The Dockerfile installs **AL2023 `nodejs22`**, which links the base image's
snapshot-safe libcrypto. Do not replace it with an official `node` image with
bundled OpenSSL. AWS evaluation rejects `bundledOpenSSL=true`, even if local
checks pass. `make check` calls synthetic ready/run hooks and is intended for a
fresh local container only.
This `make check` command runs the 30 feature assertions only. Use `npm run smoke`
for the local delivery/lifecycle scenarios or `./run-all.sh` for their AWS proof.


For an already-running AWS VM whose platform hooks have completed:

```sh
# Supply MICROVM_TOKEN in the environment, not on the command line.
node verify.js --url "https://$MICROVM_ENDPOINT" --output results/manual.json
```

Never add `--simulate` on AWS. The CLI restricts simulation to HTTP loopback
URLs. This standalone command checks guest evidence only, not clone uniqueness
or backend ingestion.

## End-to-end AWS run

```sh
# DD_API_KEY must already be exported for the target Datadog organization.
./run-all.sh
```

Requires `aws`, `aws-vault`, `dd-auth`, `python3`, and `tee`. Authenticate
AWS and the matching dd-auth organization first. No deployment is performed by
`npm run smoke` or the container workflow above.

The AWS runner:

1. Creates a fresh source-only ZIP from the explicit build manifest and records
   SHA-256 hashes of source, package/lockfile, and Dockerfile. The archived
   Dockerfile is the canonical init image dependency.
2. Uploads to S3 and creates/updates an ARM64 MicroVM image. All six lifecycle
   hooks target serverless-init on port 9000, forwarding to the app on 8080.
3. Launches candidates sequentially, stopping at the first pair sharing a
   prepared build-time runtime ID, with a hard limit of **six VMs**. Only AWS
   invokes ready/run. The runner saves each candidate's state and selection.
4. Runs the two initial feature samples plus automatic export and both outage/
   recovery cases on the first selected VM. Then performs **three** explicit AWS
   suspend/resume cycles and verifies all features after every resume. Auto-resume
   is disabled; unselected candidates never run the feature scenarios.
5. Evaluates pre-termination evidence, arms the final marker on the first selected
   VM, and terminates **all candidates**. The actual forwarded terminate hook
   creates the last trace/log; the runner never sends a synthetic AWS hook.
6. Captures CloudWatch, waits for ingestion, and evaluates Datadog via `dd-auth`,
   including the final marker and all five feature result logs.
7. Returns nonzero on failed/missing evidence or inconclusive selection. Cleanup
   runs after failures and interruptions too, including before a pair is selected.
   Each VM has a 600-second maximum lifetime.

Images/versions and S3 archives remain for inspection and incur storage costs;
VM cleanup does not delete those resources. The API key is passed via an
owner-only temporary file; proxy tokens stay in memory. `DD_APP_KEY` is never
packaged. Do not commit results or `.dd-env`.

| Environment variable | Default / purpose |
| --- | --- |
| `AWS_VAULT_PROFILE` | `sso-serverless-sandbox-account-admin` |
| `REGION` | `us-east-2` |
| `BUILD_ROLE_ARN` | `arn:aws:iam::425362996713:role/microvm-build-role` |
| `EXECUTION_ROLE_ARN` | Build role unless overridden |
| `BASE_IMAGE_ARN` | Regional AWS-managed `al2023-1` |
| `IMAGE_NAME` | `sample-verify-tracer-js-app` |
| `S3_BUCKET` | Image name unless overridden |
| `DD_SITE` | `datadoghq.com` |
| `DD_AUTH_DOMAIN` | `dddev.datadoghq.com`; must match deployment key organization |
| `DD_API_URL` | Optional HTTPS Datadog query API base |
| `CHECK_DELAY` | `180` seconds |
| `BUILD_TIMEOUT` | `1800` seconds |
| `RESULTS_DIR` | New `results/<UTC timestamp>-<pid>` directory; existing directories rejected |

The unique results basename becomes `DD_VERSION`, isolating backend queries.
The guest requests full sampling with a 1000 traces/s rate limit so the 128-root
ID probe does not exhaust the default 100 traces/s limiter. Do not override
sampling for a verification run. Backend metrics establish APM arrival, not
retrieval of every individual indexed trace; runtime metric identity is checked
at intake, not through a backend RSS query.

### Evidence and re-evaluation

Results contain `deployment.json`, `selection.json`, `candidates/vm<N>.json`,
`candidates/state<N>.json`, and the selected-pair aliases `vm1.json`/`vm2.json`.
Schema 2 selected-pair evidence includes `verification-1.json`,
`verification-2.json`, `verification-resumed-1.json` through
`verification-resumed-3.json`, `delivery-automatic.json`,
`delivery-recovery.json`, `termination-armed.json`, `clone-checks.json`,
`cloudwatch.json`, `evaluation.json`, numbered step logs, and `summary.txt`.
The final hook result is captured in CloudWatch/Datadog logs, not retrieved from
the terminated VM. Local simulation additionally saves `termination.json`.
Candidate records and selected aliases both record successful termination;
cleanup deduplicates VM IDs.

```sh
# Full existing-run evaluation with credentials injected by dd-auth:
dd-auth --domain dddev.datadoghq.com -- python3 check.py results/<run> \
  --output results/<run>/evaluation-recheck.json

# Recompute using previously captured Datadog responses; no network or credentials:
python3 check.py results/<run> \
  --cached-evaluation results/<run>/evaluation.json \
  --output results/<run>/evaluation-cached.json

# Existing AWS guest + CloudWatch evidence only; explicitly no backend claim:
python3 check.py results/<run> --guest-only \
  --output results/<run>/offline-evaluation.json

# Credential-free Python evaluator/workflow and Node wire-evidence tests:
make test
```

Cached evaluation requires a separate explicit output; it refuses to overwrite
its source or the original `evaluation.json`. It recomputes assertions rather
than trusting old pass/fail fields and labels the result `aws-and-cached-datadog`.
Missing cached responses fail without falling back to live API calls.

Preserved schema 1 runs still use their original three samples and can be
re-evaluated. Their `coverage` explicitly marks the four new scenarios as
`notCovered`; they cannot establish schema 2 coverage.

Private config/exporter/runtime-metric probes are intentionally tied to 6.15.0.
Trace assertions use real wire payloads, not monkey-patched exporters. The intake
binds loopback and uses BigInt-aware MessagePack decoding; converting uint64 IDs
to JS Number would invalidate collision and parent-link checks.

## Verification performed

- The earlier AWS run `results/20261102T041423Z-58922` passed **165/165** schema 1
  checks, including a matched snapshot baseline, real suspend/resume, Agent
  forwarding, backend feature metrics/logs, and cleanup. It predates these four
  delivery scenarios.
- Native and ARM64 AL2023 schema 2 capture-only smoke passed all 30 feature assertions initially
  and after three synthetic cycles, plus automatic export, both outage/recovery
  cases, and final-hook assertions. Automatic export completed in about two
  seconds with manual-flush count unchanged. Both 24-trace recovery workloads
  peaked at 24 queued traces and drained; final export also used no manual flush.
- **53 Python regression tests and four Node wire-evidence tests passed.** They
  cover missing phases/cycles, false acknowledgement from rejected or capture-only
  payloads, changed counters/identity, final marker mismatch, cached-data safety,
  and cleanup after partial failures. Wire tests use a real controlled upstream
  and reject a 200 response whose body has not yet completed.
- No new AWS deployment was performed for schema 2. Run `./run-all.sh` to verify
  real repeated suspension, final Agent lifecycle flushing, and new backend
  evidence. Local capture-only results do not prove those AWS properties.

