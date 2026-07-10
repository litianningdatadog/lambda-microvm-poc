# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Start

- At the start of every new session, invoke the terminal-title skill (Skill tool with `skill="terminal-title"`) after receiving the user's first task prompt. Do this automatically without being asked.

## Coding Philosophy

These guidelines bias toward caution over speed. The test: "Would a senior engineer say this is overcomplicated?"

### Think Before Coding

-   Surface assumptions before writing code. When requirements are ambiguous, present multiple interpretations rather than silently picking one.
-   If something seems off or unnecessarily complex, stop and name what's confusing. Ask clarifying questions before proceeding.
-   Suggest simpler approaches when you see them — even if it means less work for you.

### Simplicity First

-   Write the minimum code that solves the problem. No speculative features, no abstractions for single-use code, no unrequested flexibility.
-   Don't add error handling for scenarios that can't happen. Trust internal code and framework guarantees. Only validate at system boundaries.
-   Three similar lines is better than a premature abstraction. No half-finished implementations.

### Surgical Changes

-   Touch only what's necessary. Don't improve adjacent code, refactor unbroken things, or change style to personal preference.
-   When your changes create orphans, clean up only what YOUR changes made unused. Don't remove pre-existing dead code unless asked.
-   Every changed line should trace directly to the user's request.

### Goal-Driven Execution

-   Define verifiable success criteria before writing code. "Add validation" becomes "write tests for invalid inputs, then make them pass."
-   For multi-step tasks, state a brief plan with steps and verification checks before implementing.
-   Verify work is complete before claiming it's done. Run the dev server, test edge cases, check for regressions.
- 
## Overview

This is an **AWS Lambda MicroVM** developer kit (started during Private Preview; the service is now GA). Lambda MicroVMs are serverless ephemeral compute environments (max 8 hours) powered by Firecracker virtualization, combining VM-level isolation with container resource efficiency. `lambda-microvms` now ships natively in AWS CLI v2 and boto3 GA — no custom SDK wheels or `aws configure add-model` step required (see Setup below). This kit contains SDK reference material and several example applications.

**Constraints:**
- ARM64 (aarch64) architecture only — confirmed in the GA schema (`Architecture` enum is `ARM_64` only)
- US East 2 (Ohio) region only, for this dev kit's configured account/environment
- Snapshot optimization is required (on by default)
- No container image support — zip artifact + Dockerfile only

GA added a few capabilities the old preview docs said were unsupported:
- **VPC egress is supported** — attach an egress network connector of type `VPC_EGRESS` to reach RDS/Aurora, ElastiCache, internal NLBs, etc. (see Step 2 below and `networking.md` in the [official reference](https://github.com/aws/agent-toolkit-for-aws/tree/main/skills/specialized-skills/serverless-skills/aws-lambda-microvms/references)).
- **Updating a MicroVM Image is supported** via `update-microvm-image` — this adds a new *version* to an existing image (same name, same underlying S3-backed artifact history) rather than requiring a brand-new image resource per deploy. `deploy-microvm.sh` does this automatically: it reuses the existing image by name and calls `update-microvm-image` instead of `create-microvm-image` when one already exists.

## Repository Layout

This repo mixes **legacy preview SDK artifacts** (schemas, custom boto3 wheels — no longer required now that GA ships natively) with **runnable examples** at two distinct layers of the stack:

| Concern | Location |
|---------|----------|
| GA API schema (authoritative) | `lambdamicrovms-ga.api.json` — sourced from botocore; also what AWS CLI v2 / boto3 already ship natively (see Setup) |
| Old preview API schema (historical) | `lambdamicrovms-2025-09-09.json` (canonical), `lambdamicrovms-2025-09-09-2026-03-07.json` (diff snapshot) — superseded by GA; kept for diffing |
| Legacy preview SDK wheels | `Boto3CliV1Artifacts/` — **no longer needed**; GA CLI/boto3 already support `lambda-microvms` natively. Kept for historical reference only |
| Canonical boto3 usage | `microvms_boto3_example.py` (minimal `list_micro_vms` example — note this uses the old preview operation name) |
| IAM policies for the build role | `build-role-policy.json`, `build-role-trust-policy.json` — must trust `lambda.amazonaws.com` (GA) with an `aws:SourceAccount` condition; the repo's trust policy also still carries the legacy `lambda-microvms-private-preview.amazonaws.com` principal for backward compatibility |
| One-shot deploy / run scripts | `deploy-microvm.sh` (zip → S3 → create-or-update image → poll → run → token), `run-microvm.sh` (run a MicroVM from an existing image ARN or clone an existing MicroVM's config), `get-microvm-token.sh` (mint a fresh auth token for an existing MicroVM) |
| **User-app examples** (what runs on port 8080 / 50051) | `simple-python-repl-app/` (Flask REPL), `nodejs-grpc-example/` (gRPC echo) |
| **Lifecycle-hook examples** (what runs on port 9000) | `sample-flask-app/` (bare Flask), `sample-flask-app-using-serverless-comp-poc/` (wrapped by `datadog-serverless-compat` running as PID 1) |
| Sidecar (WIP Rust) | `microvm-sidecar/` — static ARM64 binary that handles all 5 hooks so user apps don't have to |
| Sidecar design + plan | `docs/superpowers/specs/2026-04-09-microvm-sidecar-design.md`, `docs/superpowers/plans/2026-04-09-microvm-sidecar-lifecycle.md` |
| Lifecycle-hook API spec | `lifecycle_hooks_openapi.json` |

## Setup

`lambda-microvms` is GA and ships natively in current AWS CLI v2 and boto3 — **no wheel installation and no `aws configure add-model` step needed.** (Confirmed: AWS CLI 2.35.20 and boto3 1.42.57 both recognize the service out of the box.) The `Boto3CliV1Artifacts/` custom wheels were required during Private Preview and are kept only for historical reference — installing them today would shadow the GA-capable stock SDK with an older preview-only one that's missing operations like `update-microvm-image` and `run-microvm`.

### CLI Setup (AWS CLI v2)

```bash
# Verify the service is recognized (no add-model / endpoint override needed):
awsv aws lambda-microvms list-microvm-images --region us-east-2
```

**`--region` is required; `--endpoint` is not** — GA resolves the real regional endpoint. (`deploy-microvm.sh` / `run-microvm.sh` / `get-microvm-token.sh` only ever pass `--region`.)

### boto3 client pattern

```python
import boto3
client = boto3.client('lambda-microvms', region_name="us-east-2")
```

## Core Workflow

### 1. Create a MicroVM Image

Package your app as a zip containing a `Dockerfile` at the root, upload to S3 (us-east-2), then:

```shell
cd <YOUR_MICROVM_APP_DIR>
ZIP_PATH="$(dirname "$PWD")/$(basename "$PWD").zip"
zip -r "$ZIP_PATH" . -x '*.DS_Store' 'claude-notifications.jsonl'
echo "Created: $ZIP_PATH"
```

[S3 bucket example](https://us-east-2.console.aws.amazon.com/s3/buckets/microvm-425362996713-us-east-2-an?region=us-east-2&tab=objects)

[microvm-build-role](https://us-east-1.console.aws.amazon.com/iam/home?region=us-east-2#/roles/details/microvm-build-role)

```bash
# Convention: one S3 bucket per app (bucket name = app name).
# Object key is just a timestamp — the bucket already disambiguates:
#   s3://<app-name>/YYYYMMDD_HHMMSS.zip
# Old builds stay in the bucket so you can roll back by pointing a new
# image at a prior key.
awsv aws lambda-microvms create-microvm-image \
  --code-artifact uri=s3://simple-python-repl-app/20260421_095217.zip \
  --name simple-python-repl-app \
  --base-image-arn arn:aws:lambda:us-east-2:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::425362996713:role/microvm-build-role \
  --region us-east-2
```

For the full zip → upload → create-or-update → poll → launch flow in one command, use `./deploy-microvm.sh <app-dir>`.

**Iterating on an existing image (GA):** re-running against an image name that already exists calls `update-microvm-image` instead of `create-microvm-image` — this adds a new *version* to the same image rather than creating a new image resource per deploy. `deploy-microvm.sh` does this automatically (it looks up the image by name first); to do it by hand:

```bash
awsv aws lambda-microvms update-microvm-image \
  --image-identifier arn:aws:lambda:us-east-2:425362996713:microvm-image:simple-python-repl-app \
  --code-artifact uri=s3://simple-python-repl-app/20260421_101533.zip \
  --base-image-arn arn:aws:lambda:us-east-2:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::425362996713:role/microvm-build-role \
  --region us-east-2
```

Note `update-microvm-image` uses **PUT semantics** — every required field (`codeArtifact`, `baseImageArn`, `buildRoleArn`) must be resent, not just the ones that changed.

Build logs stream to CloudWatch under `/aws/lambda-microvms/<image-name>`.

**Build-time IAM role** must trust `lambda.amazonaws.com` (GA; the repo's `build-role-trust-policy.json` also still carries the legacy `lambda-microvms-private-preview.amazonaws.com` principal for backward compatibility) and have:
- `s3:GetObject` — download your zip
- `logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents` — CloudWatch logs
- `ecr:GetAuthorizationToken` — only if Dockerfile uses a private ECR image

### 2. Launch a MicroVM

```bash
awsv aws lambda-microvms run-microvm \
  --image-identifier arn:aws:lambda:us-east-2:425362996713:microvm-image:simple-python-repl-app-2 \
  --image-version 1.0 \
  --execution-role-arn arn:aws:iam::425362996713:role/microvm-build-role \
  --ingress-network-connectors '["arn:aws:lambda:us-east-2:aws:network-connector:aws-network-connector:HTTP_INGRESS"]' \
  --egress-network-connectors '["arn:aws:lambda:us-east-2:aws:network-connector:aws-network-connector:INTERNET_EGRESS"]' \
  --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,"suspendedDurationSeconds":300}' \
  --region us-east-2
```

**Network connectors are optional (GA).** With no `--ingress-network-connectors`
/ `--egress-network-connectors`, the service defaults to `HTTP_INGRESS` +
`INTERNET_EGRESS` — all an HTTP app needs. Per-port access is governed by the
auth token's `--allowed-ports`, *not* by ingress connectors. Omit them unless
you need **shell access** (attach `SHELL_INGRESS` alongside `HTTP_INGRESS`) or
**VPC egress** (a `VPC_EGRESS` connector). `./deploy-microvm.sh` only passes
connectors when `SHELL_ENABLED=true`.

When you do pass connector ARNs, they are fully-qualified as
`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:<NAME>`
(region in the region slot, literal `aws` in the account slot — AWS-managed
connectors). The old preview `arn:aws:lambda:::network-connector:…` form is
rejected, and `ALL_INGRESS` is **exclusive** (cannot be combined with any other
ingress connector). `HTTP_INGRESS` covers HTTP/HTTP2 (incl. gRPC).

Resources per MicroVM: up to 4 vCPUs / 8 GB memory / 32 GB disk.

### 3. Generate an Auth Token

```bash
awsv aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier microvm-74c4447e-c05c-31a5-b310-67af45a34d79 \
  --expiration-in-minutes 30 \
  --allowed-ports '[{"port":8080}]' \
  --region us-east-2 \
  --query 'authToken."X-aws-proxy-auth"' --output text
```

`--allowed-ports` is **required** in GA — scope the token to the port(s) your app/hooks actually listen on. Each entry is `{"port": N}`, `{"range": {"startPort": N, "endPort": M}}`, or `{"allPorts": {}}`. Max TTL is 60 minutes. For shell access instead, use `create-microvm-shell-auth-token` with the `SHELL_INGRESS` connector attached at run time.

### 4. Connect to a MicroVM

**Proxy endpoint (GA):** there is **no fixed proxy host**. Each MicroVM has its
own data-plane endpoint, returned in the `endpoint` field of `run-microvm` /
`get-microvm`, with the pattern `<uuid>.lambda-microvm-gamma.<region>.on.aws`.
The old preview `cell01.<region>.gamma.arp.kepler-analytics.aws.dev` host no
longer resolves. (`./deploy-microvm.sh` captures this into `MICROVM_ENDPOINT`
in the generated `.microvm-token.*` file.)

```bash
# Fetch the endpoint for a running MicroVM:
awsv aws lambda-microvms get-microvm --microvm-identifier <MICROVM_ID> \
  --region us-east-2 \
  --query 'endpoint' --output text
```

Pass auth via headers:
- `X-aws-proxy-auth: <token>` — required
- `X-aws-proxy-port: <port>` — optional, defaults to routing 443 → 8080

```bash
curl "https://$MICROVM_ENDPOINT/" \
  -H "X-aws-proxy-auth: $MICROVM_TOKEN" \
  -H "X-aws-proxy-port: $APP_PORT"
```

For a REPL-style `/execute` endpoint:
```bash
curl -X POST "https://$MICROVM_ENDPOINT/execute" \
  -d '{"code": "print(100000+1)"}' \
  -H "Content-Type: application/json" \
  -H "X-aws-proxy-auth: $MICROVM_TOKEN"
```

Attention:
- Use -X <method> if the method is other than GET
- Append your app's path to `https://$MICROVM_ENDPOINT` (see your app)

**Browser WebSockets** — browsers cannot set arbitrary headers on WebSocket connections; use subprotocols instead:
```js
const ws = new WebSocket(endpoint, [
  "lambda-microvms",
  "lambda-microvms.authentication.<token>",
  "lambda-microvms.port.<port>"
]);
```

**gRPC** — pass the same values as HTTP/2 metadata headers on every call.

## Example Applications

Two layers are exemplified: the **user app** (what runs inside the MicroVM serving traffic) and the **lifecycle-hook server** (the MicroVM platform calls this on port 9000).

| App | Layer | Port | Purpose |
|-----|-------|------|---------|
| `simple-python-repl-app/` | User app | 8080 | Flask REPL with `/execute`; what gets packaged into a MicroVM Image |
| `nodejs-grpc-example/` | User app | 50051 | gRPC echo (unary + bidi streaming); self-signed TLS cert auto-generated |
| `sample-flask-app/` | Lifecycle hooks | 9000 | Reference implementation of all 5 hooks + `/execute` + `/health` |
| `sample-flask-app-using-serverless-comp-poc/` | Lifecycle hooks + user app | 9000 (hooks) + 8080 (app) | Same Flask app, wrapped by `datadog-serverless-compat` as PID 1 in init mode — captures stdout/stderr into DD's log pipeline |
| `microvm-sidecar/` (WIP) | Lifecycle hooks | 9000 | Static Rust binary; goal is to remove hook boilerplate from user apps — see its own section below |

### simple-python-repl-app (Flask, port 8080)

```bash
cd simple-python-repl-app
pip install flask==3.0.0
python app.py

# Or via Docker (ARM64)
docker build -t python-repl .
docker run -p 8080:8080 python-repl

# Test it
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1+1)"}'
```

### nodejs-grpc-example (gRPC, port 50051)

The server auto-generates a self-signed TLS certificate on first run.

```bash
cd nodejs-grpc-example
npm install
node server.js      # run server
node client.js      # run client (update YOUR_MICROVM_AUTH_TOKEN in client.js:21 first)
```

### sample-flask-app / sample-flask-app-using-serverless-comp-poc

Both implement the 5 lifecycle hooks on port 9000. The `-using-serverless-comp-poc` variant additionally ships the `datadog-serverless-compat` ARM64 binary as the container entrypoint — it runs as PID 1, execs the user's Python app as a child, forwards signals, and streams stdout/stderr into DataDog. Init mode requires `AWS_LAMBDA_FUNCTION_ARN` to contain `"microvm"` (set by the MicroVM runtime; faked in the Dockerfile for local runs).

```bash
cd sample-flask-app-using-serverless-comp-poc
make build      # docker build -t flask-sidecar-poc .
make run        # docker run -p 8080:8080 -p 9000:9000 flask-sidecar-poc
```

### microvm-sidecar (Rust, port 9000) — IN PROGRESS

Static ARM64 Rust binary (`axum` + `tokio` + `reqwest`) that implements all 5 lifecycle hooks and emits DataDog events, metrics, and logs on each transition. **Current state:** only `src/config/mod.rs` is implemented; `hooks/`, `datadog/`, `emitter.rs`, `collector/`, and `flush.rs` are zero-byte stubs. Read the design + task-by-task plan under `docs/superpowers/` before touching the implementation.

```bash
cd microvm-sidecar
cargo build --release                                # default build
cargo build --release --features system-metrics      # opt-in buffered metrics path
cargo test                                           # wiremock, tempfile, serial_test
cargo test config::                                  # run a single module's tests
```

**Architecture:** Two observability paths — *immediate* for lifecycle hooks (`tokio::spawn` straight to the DD API, no buffer) and *buffered* for platform metrics (bounded `mpsc` channel → batched flush). Tests that mutate process env are marked `#[serial]`.

**Config resolution order:** `$SIDECAR_CONFIG` → `/etc/microvm-sidecar.yaml` → `./microvm-sidecar.yaml` → built-in defaults. Env overrides: `SIDECAR_PORT`, `DD_API_KEY`, `DD_SITE`, `DD_SERVICE`, `DD_ENV`, `DD_TAGS` (comma-separated), `COLLECTORS_SYSTEM_METRICS_ENABLED`.

## Lifecycle Hooks

Implement hooks as HTTP endpoints on port 9000 inside your MicroVM. (The
`lifecycle_hooks_openapi.json` in this repo is the **preview** spec — it still
lists `launch` and `/beta/v1/` paths; the authoritative GA hook config is in
`lambdamicrovms-2025-09-09.json` / `lambdamicrovms-ga.api.json`.)

GA renamed `launch` → `run`, added a build-time `validate` hook, and dropped
`/beta` from the path (now `/aws/lambda-microvms/runtime/v1/<hook>`). Timeouts
below are the configurable maximums from the GA schema.

**Build-time hooks** (`microvmImageHooks`, invoked during image build):

| Hook | Path | Max timeout | Purpose |
|------|------|-------------|---------|
| Validate | `POST /aws/lambda-microvms/runtime/v1/validate` | 3600s | Validate the MicroVM image build |
| Ready | `POST /aws/lambda-microvms/runtime/v1/ready` | 3600s | Signal app startup complete during image build |

**Runtime hooks** (`microvmHooks`, invoked on a running MicroVM):

| Hook | Path | Max timeout | Purpose |
|------|------|-------------|---------|
| Run | `POST /aws/lambda-microvms/runtime/v1/run` | 60s | Health checks / reset unique state after run from snapshot (receives `microVmId`, `meshIpv6Address`) |
| Resume | `POST /aws/lambda-microvms/runtime/v1/resume` | 60s | Recreate connections after in-place resume |
| Suspend | `POST /aws/lambda-microvms/runtime/v1/suspend` | 60s | Clean up connections before suspend |
| Terminate | `POST /aws/lambda-microvms/runtime/v1/terminate` | 60s | Flush data before termination |

## Snapshot Uniqueness

MicroVM Images are Firecracker snapshots shared across all MicroVMs launched from that image. **Do not generate unique content (IDs, secrets, random seeds) at image build time.** Generate unique content in the `run` hook or after launch.

Use CSPRNGs: Java `SecureRandom`, Python `secrets`/`random.SystemRandom`, Node.js `crypto.randomBytes`/`crypto.randomUUID`, .NET `RandomNumberGenerator`, Go `crypto/rand`, Rust `rand::rngs::OsRng`, C/C++ `getrandom(2)`, or read from `/dev/urandom` per-call. Avoid `Math.random()`, `random.random()`, `System.Random`, `math/rand`, `rand::thread_rng()` seeded once, and caching `/dev/urandom` bytes read once at build time.

## Operating MicroVMs

**Shell access:** requires `SHELL_INGRESS` attached at run time (see Section 2), then `create-microvm-shell-auth-token` — connect via the AWS console "Connect" button on the MicroVM detail page or a WebSocket client. The official GA docs describe the shell landing directly in the app's container; in this dev kit's environment the shell has opened on the host OS instead, requiring an extra step to reach the app container:
```bash
ctr task ls                                              # find container ID
ctr task exec -t --exec-id shell <container_id> /bin/sh  # enter container
```

**Logs:** Stream to CloudWatch under `/aws/lambda-microvms/<image-name>`. When using `NO_EGRESS` mode, logs are only accessible via `wscat` directly and are lost on termination.

**Idle/suspend:** Idle time is measured by traffic on the endpoint URL. Async apps that don't serve endpoint traffic should disable auto-suspend or set a generous `maxIdleDurationSeconds`.

## Service Quotas

| Quota | Limit |
|-------|-------|
| Concurrent MicroVMs | 1000 per account |
| Resources per MicroVM | 4 vCPUs / 8 GB memory / 32 GB disk |
| MicroVM Images per account | 1000 |
| Zip artifact size | 32 GB |

## Additional References

See **Repository Layout** above for the canonical file map. Two items not covered there:

- `boto3-installation-instructions.md` — standalone copy of the old preview wheel-install steps; **stale** now that GA CLI/boto3 ship `lambda-microvms` natively (see Setup above)
- `Boto3CliV1Artifacts/reviews/` — diffs and commit history for the custom preview boto3/CLI builds
