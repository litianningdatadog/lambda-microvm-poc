# dd-trace-js MicroVM verification

An executable verifier for **dd-trace 6.15.0**, using the same Express/Pino stack
as `../sample-nodejs-app` and a **locally built serverless-init binary** like
`../sample-verify-tracer-app`. No serverless-init Docker repository dependency.

The scenarios target [dd-trace-js PR #9709](https://github.com/DataDog/dd-trace-js/pull/9709):
a Firecracker snapshot includes the tracer's cached random IDs and process
identity. Each clone needs fresh IDs, but resuming the *same* VM must preserve
its identity. Initializing two ordinary Node processes does not test that bug.

## Scenarios and required evidence

`POST /verify` returns 30 named boolean assertions plus decoded native trace
payloads, metric lines, expected span relationships, and lifecycle state. Any
false assertion makes the result fail. The AWS evaluator also requires the
complete assertion set; a passing subset is not sufficient.

| Scenario | Assertion / evidence |
| --- | --- |
| Version and environment | Exactly 6.15.0; the tracer detects `AWS_LAMBDA_MICROVM_IMAGE_ARN` as a serverless MicroVM. The app never sets that variable itself. |
| Snapshot preparation | Forwarded `/ready` warms the ID cache, records the build-time runtime ID, exercises tracing/custom metrics, and flushes before returning. Repeated ready calls are idempotent. |
| Native `/run` refresh | The real tracer HTTP subscriber refreshes identity before the app's handler executes. The app only reads identity; it never calls refresh/reseed functions or publishes identity channels. |
| Two clones of one snapshot | Launch up to six candidates of the same image version and select the first pair with the same prepared build-time runtime ID. Require different runtime IDs after run. Compare 128 early application-created trace/span IDs plus eight feature roots per clone. Compare **low 64 trace-ID bits**, not just the timestamp-containing 128-bit ID. Detect duplicates within and across samples. |
| In-place suspend/resume | AWS suspend/resume APIs and observed `SUSPENDED`/`RUNNING` states; each hook arrives once. Runtime ID stays unchanged, run count stays one, and all tracing features work again with eight newly generated roots. |
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
| Delivery to Datadog | AWS mode forwards original payload bytes/headers to serverless-init and fails on relay errors. Backend evaluation requires APM hit metrics for all eight feature operations, expected-error metrics, and structured verification logs matching each of the three results. CloudWatch must have matching records in the tested VMs' streams. |

One image version can contain multiple snapshot builds; matching image ARN/version
alone is not a shared-baseline guarantee. Candidate selection reads `GET /state`
without running feature scenarios. If no pair shares `before.runtimeId` within
six candidates, selection is **inconclusive**, the run fails, and every candidate
is cleaned up. The shared-baseline assertion is never skipped.

The Datadog log pipeline may omit null `rcClientId` fields. Log matching tolerates
only those omissions at the six known identity locations when the required
disabled-RC assertion matches. Runtime IDs, counters, all assertions, and unrelated
fields remain strict.

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

## Local smoke: no credentials

Requires Node.js 22+ and npm:

```sh
cd sample-verify-tracer-js-app
npm ci
npm run smoke
```

Runs the actual app/tracer in separate MicroVM-simulation and ordinary-process
children. Exercises the HTTP surface, decodes actual v0.4 MessagePack payloads,
checks all guest assertions, repeats features after simulated suspend/resume,
and stops both children. Logs and JSON evidence are under `results/local-*/`.
Deployment API/app keys are removed from child environments; intake forwarding
is disabled. `LOCAL_PORT` overrides the default app port 18080; internal capture
ports 18126/TCP and 18125/UDP must be free.

**This is a lifecycle simulation, not snapshot, serverless-init, or Datadog
backend proof.** Local processes and containers cannot establish clone-safe
OpenSSL entropy. Only the two-clone AWS scenario checks that property.

### ARM64 AL2023 container

```sh
make build                       # copies local serverless-init, installs pinned lockfile
make start PORT=18081             # capture-only; bypasses serverless-init
make check PORT=18081 OUTPUT=results/docker.json
make stop
```

The Dockerfile installs **AL2023 `nodejs22`**, which links the base image's
snapshot-safe libcrypto. Do not replace it with an official `node` image with
bundled OpenSSL. AWS evaluation rejects `bundledOpenSSL=true`, even if local
checks pass. `make check` calls synthetic ready/run hooks and is intended for a
fresh local container only.

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

Requires `aws`, `aws-vault`, `dd-auth`, `make`, `python3`, and `tee`. Authenticate
AWS and the matching dd-auth organization first. No deployment is performed by
`npm run smoke` or the container workflow above.

The AWS runner:

1. Copies the local binary into the explicit build manifest and records SHA-256
   hashes. Packages source, package/lockfile, Dockerfile, and binary only.
2. Uploads to S3 and creates/updates an ARM64 MicroVM image. All six lifecycle
   hooks target serverless-init on port 9000, forwarding to the app on 8080.
3. Launches candidates sequentially, stopping at the first pair sharing a
   prepared build-time runtime ID, with a hard limit of **six VMs**. Only AWS
   invokes ready/run. The runner saves each candidate's state and selection.
4. Calls `/verify` only on the selected pair, then suspends/resumes the first
   selected VM and verifies it again. Auto-resume is disabled so the AWS state
   transition is explicit. Unselected candidates never run the feature scenarios.
5. Evaluates clone IDs and resume invariants, terminates **all candidates**,
   captures CloudWatch, waits for ingestion, and evaluates Datadog via `dd-auth`.
6. Returns nonzero on failed/missing evidence or inconclusive selection. Cleanup
   runs after failures and interruptions too, including before a pair is selected.
   Each VM has a 600-second maximum lifetime.

Images/versions and S3 archives remain for inspection and incur storage costs;
VM cleanup does not delete those resources. The API key is passed via an
owner-only temporary file; proxy tokens stay in memory. `DD_APP_KEY` is never
packaged. Do not commit results or `.dd-env`.

| Environment variable | Default / purpose |
| --- | --- |
| `SERVERLESS_INIT_BIN` | `/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64` |
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
Selected-pair evidence remains `verification-1.json`, `verification-2.json`,
`verification-resumed.json`, `clone-checks.json`, `cloudwatch.json`,
`evaluation.json`, numbered step logs, and `summary.txt`. Candidate records and
selected aliases both record successful termination; cleanup deduplicates VM IDs.

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

# Credential-free evaluator regression tests:
make test
```

Cached evaluation requires a separate explicit output; it refuses to overwrite
its source or the original `evaluation.json`. It recomputes assertions rather
than trusting old pass/fail fields and labels the result `aws-and-cached-datadog`.
Missing cached responses fail without falling back to live API calls.

Private config/exporter/runtime-metric probes are intentionally tied to 6.15.0.
Trace assertions use real wire payloads, not monkey-patched exporters. The intake
binds loopback and uses BigInt-aware MessagePack decoding; converting uint64 IDs
to JS Number would invalidate collision and parent-link checks.

## Verification performed

- Initial native and ARM64 AL2023 smoke runs passed all 30 guest assertions.
- AWS run `results/20261101T214140Z-20701` exercised real suspend/resume,
  serverless-init forwarding, and Datadog ingestion. All guest assertions and APM
  metrics passed, but its two VMs inherited different build-time baselines.
- Re-evaluation with the targeted RC-field fix is saved separately as
  `evaluation-recheck-rc-null.json`: **164/165 checks pass**, including all three
  Datadog log matches. The shared-baseline check still fails correctly. Original
  evidence files are unchanged; this result does not prove same-snapshot reseeding.
- All **32 evaluator/workflow regression cases passed**, covering pipeline null
  omission, strict identity/counter checks, cached-output safety, A/B/A selection,
  exhaustion of six candidates, startup/state/interruption failures, and cleanup
  retry/idempotence.
- A credential-free CLI smoke used real loopback HTTPS requests and replayed guest
  payloads with simulated AWS responses: baselines A/B/A selected candidates 1/3,
  issued exactly three verification requests to the selected pair, and marked all
  three candidates terminated with synchronized selected aliases.
- No new AWS deployment was performed for these fixes. Re-run `./run-all.sh` to
  verify the corrected candidate-selection workflow against AWS.

