# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an **AWS Lambda MicroVM Private Preview** developer kit. Lambda MicroVMs are serverless ephemeral compute environments (max 8 hours) powered by Firecracker virtualization, combining VM-level isolation with container resource efficiency. This kit contains the preview SDK artifacts and two example applications.

**Preview constraints:**
- ARM64 (aarch64) architecture only
- US East 2 (Ohio) region only
- No VPC egress connectivity
- Snapshot optimization is required (on by default)
- No container image support — zip artifact + Dockerfile only
- Updating a MicroVM Image is not supported; create a new one instead

## Repository Layout

This repo mixes **preview SDK artifacts** (schemas, custom boto3 wheels) with **runnable examples** at two distinct layers of the stack:

| Concern | Location |
|---------|----------|
| Preview API schema | `lambdamicrovms-2025-09-09.json` (canonical), `lambdamicrovms-2025-09-09-2026-03-07.json` (diff snapshot) |
| Custom preview SDK wheels | `Boto3CliV1Artifacts/` — install before anything else (see Setup) |
| Canonical boto3 usage | `microvms_boto3_example.py` (minimal `list_micro_vms` example) |
| IAM policies for the build role | `build-role-policy.json`, `build-role-trust-policy.json` (trust `lambda-microvms-private-preview.amazonaws.com`) |
| **User-app examples** (what runs on port 8080 / 50051) | `simple-python-repl-app/` (Flask REPL), `nodejs-grpc-example/` (gRPC echo) |
| **Lifecycle-hook examples** (what runs on port 9000) | `sample-flask-app/` (bare Flask), `sample-flask-app-using-serverless-comp-poc/` (wrapped by `datadog-serverless-compat` running as PID 1) |
| Sidecar (WIP Rust) | `microvm-sidecar/` — static ARM64 binary that handles all 5 hooks so user apps don't have to |
| Sidecar design + plan | `docs/superpowers/specs/2026-04-09-microvm-sidecar-design.md`, `docs/superpowers/plans/2026-04-09-microvm-sidecar-lifecycle.md` |
| Lifecycle-hook API spec | `lifecycle_hooks_openapi.json` |

## Setup: Private Preview SDK

The `Boto3CliV1Artifacts/` directory contains custom-built wheel files that must be installed before using the API. Use a virtual environment:

```bash
cd Boto3CliV1Artifacts
python3 -m venv python-sdk-test && source python-sdk-test/bin/activate
python3 -m pip install botocore-1.42.39-py3-none-any.whl
python3 -m pip install boto3-1.42.39-py3-none-any.whl
# Optional AWS CLI v1:
python3 -m pip install awscli-1.44.29-py3-none-any.whl
```

### CLI Setup (AWS CLI v2)

```bash
# Run from the repo root; the schema ships in this repo.
awsv aws configure add-model \
  --service-model "file://$(pwd)/lambdamicrovms-2025-09-09.json" \
  --service-name lambda-microvms

# Verify:
awsv aws lambda-microvms list-micro-vm-images \
  --region us-east-2 \
  --endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev
```

**Region and `--endpoint` are required on every CLI command** — the endpoint is an internal preview-only URL.

### boto3 client pattern

```python
import boto3
client = boto3.client(
    'lambda-microvms',
    region_name="us-east-2",
    endpoint_url='https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev'
)
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
awsv aws lambda-microvms create-micro-vm-image \
  --code-artifact uri=s3://simple-python-repl-app/20260421_095217.zip \
  --name simple-python-repl-app \
  --base-micro-vm-image-arn arn:aws:lambda:::microvm-image:lambda-microvms-al2023-1 \
  --build-role-arn arn:aws:iam::425362996713:role/microvm-build-role \
  --region us-east-2 \
  --endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev
```

For the full zip → upload → create → poll → launch flow in one command, use `./deploy-microvm.sh <app-dir>`.

Build logs stream to CloudWatch under `/aws/lambda/microvms/<image-name>`.

**Build-time IAM role** must trust `lambda-microvms-private-preview.amazonaws.com` and have:
- `s3:GetObject` — download your zip
- `logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents` — CloudWatch logs
- `ecr:GetAuthorizationToken` — only if Dockerfile uses a private ECR image

### 2. Launch a MicroVM

```bash
awsv aws lambda-microvms launch-micro-vm \
  --micro-vm-image-arn arn:aws:lambda:us-east-2:425362996713:microvm-image:simple-python-repl-app-2 \
  --micro-vm-image-version 1.0 \
  --ingress-network-connectors "arn:aws:lambda:::network-connector:aws-network-connector:ALL_INGRESS" \
  --egress-network-connectors "arn:aws:lambda:::network-connector:aws-network-connector:INTERNET_EGRESS" \
  --idle-policy autoResumeEnabled=true,maxIdleDurationSeconds=900,suspendedDurationSeconds=300 \
  --region us-east-2 \
  --endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev
```

Resources per MicroVM: up to 4 vCPUs / 8 GB memory / 32 GB disk.

### 3. Generate an Auth Token

```bash
awsv aws lambda-microvms generate-micro-vm-auth-token \
  --micro-vm-id ai-a2ceb494-7cf7-9581-8a37-b6078af5ec85 \
  --expiration-minutes 30 \
  --region us-east-2 \
  --endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev
```

### 4. Connect to a MicroVM

**Proxy endpoint:** `https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev`

Pass auth via headers:
- `X-aws-proxy-auth: <token>` — required
- `X-aws-proxy-port: <port>` — optional, defaults to routing 443 → 8080

```bash
curl 'https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev' \
  -H 'X-aws-proxy-auth: <TOKEN>' \
  -H 'X-aws-proxy-port: <PORT>'
```

For [simple-python-repl-app-2](https://us-east-2.console.aws.amazon.com/lambda/home?region=us-east-2#/microvm-images) function 
```bash
curl -X POST 'https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev/execute' \
  -d '{"code": "print(100000+1)"}' \
  -H "Content-Type: application/json" \
  -H 'X-aws-proxy-auth: <TOKEN>'
```

Attention:
- Use -X <method> if the method is other than GET
- Append path to 'https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev' if any (see your app)

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

Implement hooks as HTTP endpoints on port 9000 inside your MicroVM. Full OpenAPI spec is in `lifecycle_hooks_openapi.json`.

| Hook | Path | Timeout | Purpose |
|------|------|---------|---------|
| Ready | `POST /aws/lambda-microvms/runtime/beta/v1/ready` | 60m | Signal app startup complete during image build |
| Launch | `POST /aws/lambda-microvms/runtime/beta/v1/launch` | 60m | Health checks / reset unique state after launch from snapshot |
| Suspend | `POST /aws/lambda-microvms/runtime/beta/v1/suspend` | 120s | Clean up connections before suspend |
| Resume | `POST /aws/lambda-microvms/runtime/beta/v1/resume` | 120s | Recreate connections after in-place resume |
| Terminate | `POST /aws/lambda-microvms/runtime/beta/v1/terminate` | 60s | Flush data before termination |

## Snapshot Uniqueness

MicroVM Images are Firecracker snapshots shared across all MicroVMs launched from that image. **Do not generate unique content (IDs, secrets, random seeds) at image build time.** Generate unique content in the `launch` hook or after launch.

Use CSPRNGs: Java `SecureRandom`, Python `random.SystemRandom`, Node.js `crypto.randomBytes`, .NET `RandomNumberGenerator`, or read from `/dev/urandom`.

## Operating MicroVMs

**Shell access (preview):** The shell opens on the host OS. To enter your app container:
```bash
ctr task ls                                              # find container ID
ctr task exec -t --exec-id shell <container_id> /bin/sh  # enter container
```

**Logs:** Stream to CloudWatch under `/aws/lambda/microvms/<image-name>`. When using `NO_EGRESS` mode, logs are only accessible via `wscat` directly and are lost on termination.

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

- `boto3-installation-instructions.md` — standalone copy of the SDK/CLI install steps (duplicates the Setup section above)
- `Boto3CliV1Artifacts/reviews/` — diffs and commit history for the custom preview boto3/CLI builds
