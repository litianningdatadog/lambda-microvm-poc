# Onboard a Node.js application to Lambda MicroVM observability

This guide instruments a Node.js application running in an [AWS Lambda MicroVM](https://aws.amazon.com/lambda/microvms/) with Datadog traces, logs, log/trace correlation, and enhanced metrics.

The setup uses Datadog's **in-container** model:

- `dd-trace` instruments the Node.js process.
- `serverless-init` runs as PID 1, starts the application, collects stdout/stderr, and handles the MicroVM lifecycle-hook integration.
- The application exposes its lifecycle-hook endpoints on port `8080` by default.
- `serverless-init` listens for platform lifecycle events on port `9000` by default and forwards them to the application.

This is the MicroVM equivalent of Datadog's [in-container Node.js instrumentation](https://docs.datadoghq.com/serverless/azure_container_apps/manual_instrumentation?instrumentation_method=in_container&prog_lang=node_js&tab=maven).

## Prerequisites

You need:

- An AWS account with Lambda MicroVM access in your target Region.
- AWS CLI v2 with the `lambda-microvms` service available. The repository deployment script expects the `awsv` command to resolve to that CLI.
- Docker with Linux ARM64 build support. MicroVM images are ARM64.
- `jq` and `zip`.
- An S3 bucket in the same Region as the MicroVM image for the image artifact. The deployment script creates the bucket when needed.
- A MicroVM build role with permission to read the artifact from S3 and write build logs to CloudWatch. The role must trust `lambda.amazonaws.com` with an `aws:SourceAccount` condition.
- A Datadog API key. Store it in a secret manager or protected CI variable; do not commit it or print it in logs.

The artifact bucket, MicroVM image, and network connectors must use the same AWS Region.

## 1. Install the Node.js tracer

Install `dd-trace` version `6.15.0` or newer in the application package:

```bash
npm install dd-trace
```

The sample application pins the minimum supported `dd-trace` version in `package.json`:

```json
{
  "dependencies": {
    "dd-trace": "6.15.0"
  }
}
```

Initialize the tracer before the application loads by setting `NODE_OPTIONS` in the image. This enables automatic instrumentation without changing the application entry point:

```dockerfile
ENV NODE_OPTIONS="--require dd-trace/init"
```

For details about supported modules and tracer configuration, see [Tracing Node.js applications](https://docs.datadoghq.com/tracing/trace_collection/automatic_instrumentation/dd_libraries/nodejs/).

## 2. Wrap the application with `serverless-init`

`serverless-init` version `1.10.4` or newer must be the container entrypoint. It starts the command supplied in `CMD`, forwards lifecycle-hook requests, and reads application logs from stdout and stderr.

Add the ARM64-compatible init binary to the Dockerfile. The sample pins the minimum supported version; pin a compatible version in production rather than using `latest`.

```dockerfile
COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
```

The application must listen on all interfaces, not only `127.0.0.1`:

```js
app.listen(<DD_AWS_MICROVM_USER_APP_PORT>, "0.0.0.0");
```

Tell `serverless-init` which port the application uses for its lifecycle-hook endpoints:

```dockerfile
ENV DD_AWS_MICROVM_USER_APP_PORT=8080
```

If the application listens on a port other than the default `8080`, set `DD_AWS_MICROVM_USER_APP_PORT` to that port. Keep the application bind address, Docker `EXPOSE` value, deployment `APP_PORT`, and auth-token `--allowed-ports` value consistent with it.

The sample application's relevant Dockerfile configuration is:

```dockerfile
FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app
RUN dnf install -y nodejs22 nodejs22-npm && dnf clean all

COPY package*.json ./
RUN npm ci --omit=dev
COPY app.js .

COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

EXPOSE 8080
ENV NODE_OPTIONS="--require dd-trace/init"
ENV DD_AWS_MICROVM_USER_APP_PORT=8080
ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
```

Do not generate per-MicroVM IDs, secrets, or random state while the image is built. The image is snapshotted and reused. Generate unique state in the `run` hook or after launch.

## 3. Configure Datadog telemetry

Set the base identity variables in the deployment configuration. Keep `DD_API_KEY` out of the Dockerfile and source tree:

```bash
export DD_API_KEY="YOUR_DATADOG_API_KEY"
```

The sample deployment script passes the key into the MicroVM image environment. Treat the resulting image configuration as secret-bearing, and inject the key from protected CI or secret-management infrastructure instead of exporting it in a shared shell.

Enable only the telemetry features your application needs:

```dockerfile
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=<YOUR_APP_NAME>
ENV DD_ENV=<YOUR_DD_ENV>
ENV DD_VERSION=1
ENV DD_SOURCE=nodejs

# Enable if log collection is needed.
ENV DD_LOGS_ENABLED=true

# Enable both if tracing and log/trace correlation are needed.
ENV DD_LOGS_INJECTION=true
ENV DD_TRACE_ENABLED=true
ENV DD_TRACE_SAMPLE_RATE=1.0

# Enable if enhanced MicroVM/runtime metrics are needed.
ENV DD_ENHANCED_METRICS_ENABLED=true
```

The sample application enables all three telemetry groups. `DD_LOGS_INJECTION` requires logs to be enabled and a supported JSON logger for automatic log/trace correlation.

Use a service name that identifies the application and stable `DD_ENV` and `DD_VERSION` values that match your release process. Replace `datadoghq.com` when using another [Datadog site](https://docs.datadoghq.com/getting_started/site/).

### What each setting enables

| Variable | Purpose |
| --- | --- |
| `DD_API_KEY` | Authenticates telemetry submission. Inject it as a secret; never commit it. |
| `DD_SITE` | Selects the Datadog site. |
| `DD_SERVICE` | Groups traces, logs, and metrics under the application service. |
| `DD_ENV` | Identifies the deployment environment. |
| `DD_VERSION` | Identifies the deployed application version. |
| `DD_SOURCE` | Applies Node.js log processing. |
| `DD_LOGS_ENABLED` | Enables stdout/stderr log collection through `serverless-init`. |
| `DD_LOGS_INJECTION` | Adds trace and span identifiers to supported logs. |
| `DD_TRACE_ENABLED` | Enables APM tracing. |
| `DD_TRACE_SAMPLE_RATE` | Samples every trace for a development smoke test. Use an appropriate rate in production. |
| `DD_ENHANCED_METRICS_ENABLED` | Enables enhanced MicroVM/runtime metrics in the sample setup. |
| `DD_AWS_MICROVM_USER_APP_PORT` | Tells the init layer which port to forward user traffic. |

The deployment script also sets the local trace endpoint, startup diagnostics, and the MicroVM lifecycle-forwarding flags. These values are included in the image environment configuration sent to AWS.
When using `deploy-microvm.sh`, the following additional values are supplied to the image configuration:

```text
DD_TRACE_AGENT_URL=http://localhost:8126
DD_TRACE_STARTUP_LOGS=true
DD_REMOTE_CONFIGURATION_ENABLED=true
DD_AWS_MICROVM_ENABLE_READY=true
DD_AWS_MICROVM_ENABLE_VALIDATE=true
DD_AWS_MICROVM_ENABLE_RUN=true
DD_AWS_MICROVM_ENABLE_RESUME=true
DD_AWS_MICROVM_ENABLE_SUSPEND=true
DD_AWS_MICROVM_ENABLE_TERMINATE=true
```

The `DD_AWS_MICROVM_ENABLE_*` flags opt into forwarding each platform lifecycle hook from `serverless-init` to the application. Set the flags that correspond to hooks your application implements when using a different deployment flow.

For log/trace correlation guidance, see [Correlating Node.js logs and traces](https://docs.datadoghq.com/tracing/other_telemetry/connect_logs_and_traces/nodejs/).

### Use a structured JSON logger

Use a structured logging framework such as [Pino](https://github.com/pinojs/pino), [Winston](https://github.com/winstonjs/winston), or [Bunyan](https://github.com/trentm/node-bunyan) instead of plain-text `console` logging:

```bash
npm install pino
```

Configure the selected logger to emit JSON. Datadog's Node.js tracer supports automatic trace and span ID injection for Pino, Winston, and Bunyan when JSON output is enabled. With `DD_LOGS_INJECTION=true`, logs emitted inside an active trace include correlation fields that Datadog uses to connect the log to the trace.

Examples:

```js
// Pino: JSON output is the default.
const pino = require("pino");
const logger = pino({ name: process.env.DD_SERVICE });
logger.info({ microVmId }, "Request completed");
```

```js
// Winston: explicitly select JSON output.
const { createLogger, format, transports } = require("winston");
const logger = createLogger({
  format: format.json(),
  transports: [new transports.Console()],
});
logger.info("Request completed");
```

Bunyan also emits JSON records by default:

```js
const bunyan = require("bunyan");
const logger = bunyan.createLogger({ name: process.env.DD_SERVICE });
logger.info("Request completed");
```

Automatic injection only applies to JSON-formatted records. If you use an unsupported logger, keep JSON output and [inject correlation fields manually](https://docs.datadoghq.com/tracing/other_telemetry/connect_logs_and_traces/nodejs/#manual-injection).

The sample application uses Pino and writes structured records to stdout, which `serverless-init` forwards to Datadog.

## 4. Implement the MicroVM lifecycle hooks

The platform calls lifecycle hooks during image build and MicroVM execution. Keep runtime hooks fast and idempotent.

The sample application exposes the GA hook paths under:

```text
/aws/lambda-microvms/runtime/v1/validate
/aws/lambda-microvms/runtime/v1/ready
/aws/lambda-microvms/runtime/v1/run
/aws/lambda-microvms/runtime/v1/resume
/aws/lambda-microvms/runtime/v1/suspend
/aws/lambda-microvms/runtime/v1/terminate
```

The deployment script configures `serverless-init` to listen for platform lifecycle events on port `9000`:

- **Build-time:** `ready`, `validate`
- **Runtime:** `run`, `resume`, `suspend`, `terminate`

With `serverless-init`, the platform sends lifecycle events to port `9000`, and `serverless-init` forwards them to the application's lifecycle-hook endpoints on port `8080` by default. The `run` hook is the right place to assign the MicroVM-specific ID and recreate state that must not be shared from the image snapshot.

### Required `run` hook and custom hook timeouts

When using `dd-trace`, the application **must implement the `/run` hook** and the deployment must set:

```text
DD_AWS_MICROVM_ENABLE_RUN=true
```

The `/run` handler should initialize or refresh MicroVM-specific state and return HTTP `200` after it succeeds. The sample application receives the `microVmId` and `meshIpv6Address` payload in this hook.

The default lifecycle-hook timeouts are defined in the [AWS MicroVM image documentation](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html). If you need values other than those defaults, pass the timeout values in milliseconds through the corresponding environment variables:

| Environment variable | Hooks |
| --- | --- |
| `DD_AWS_MICROVM_FORWARD_TIMEOUT_MS` | `run`, `terminate`, `suspend`, and `resume` |
| `DD_AWS_MICROVM_READY_TIMEOUT_MS` | `ready` |
| `DD_AWS_MICROVM_VALIDATE_TIMEOUT_MS` | `validate` |

For example, add the values to the image or deployment environment:

```text
DD_AWS_MICROVM_FORWARD_TIMEOUT_MS=YOUR_RUNTIME_HOOK_TIMEOUT_MS
DD_AWS_MICROVM_READY_TIMEOUT_MS=YOUR_READY_TIMEOUT_MS
DD_AWS_MICROVM_VALIDATE_TIMEOUT_MS=YOUR_VALIDATE_TIMEOUT_MS
```

For build-time `ready` and `validate`, return HTTP `503` immediately when more time is needed so Lambda can retry according to its hook semantics. Keep runtime hooks within the configured forwarding timeout and return promptly.

At minimum, return HTTP `200` when each hook has completed successfully. Return a non-success response when the application is not ready; this prevents the platform from treating an incomplete image or transition as healthy.

## 5. Build and deploy the sample application

1. Builds the ARM64 Docker image locally.
2. Packages your application into a timestamped ZIP with the Dockerfile at the ZIP root.
3. Uploads the ZIP to S3.
4. Creates the MicroVM image, or adds a new version to the existing image with the same name.
5. Enables the build-time and runtime lifecycle hooks.
6. Runs a MicroVM with HTTP ingress, Internet egress, and shell ingress enabled by default in this repository's script.

## 6. Connect to the deployed MicroVM

To connect to the application, identify these three values:

1. **MicroVM endpoint** — the per-MicroVM `endpoint` returned by `run-microvm` or `get-microvm`.
2. **Auth token** — a token created with `create-microvm-auth-token` and scoped to the application port through `--allowed-ports`.
3. **Application port** — the user application's port.

Send the token in the `X-aws-proxy-auth` header and the application port in the `X-aws-proxy-port` header when calling the endpoint.

## 7. Verify telemetry in Datadog

1. Open **APM > Services** and select the value of `DD_SERVICE`.
2. Filter **Logs Explorer** by the same service and `DD_ENV`.
3. Open a request trace and confirm that correlated application logs contain matching trace/span identifiers.
4. Check the runtime or enhanced metrics produced by the MicroVM integration.
5. Confirm lifecycle messages for `ready`, `validate`, `run`, and any suspend/resume transitions.

If traces are present but logs are missing, first verify `DD_LOGS_ENABLED=true` and that the application writes to stdout/stderr. If logs are present without correlation fields, verify `DD_LOGS_INJECTION=true` and that the logger emits structured or supported log records.
