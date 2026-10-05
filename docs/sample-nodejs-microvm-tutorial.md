# Build, deploy, and test the sample Node.js Lambda MicroVM

This tutorial builds `sample-nodejs-app`, creates or updates a Lambda MicroVM image, runs a MicroVM from that image, and tests the `/health` and `/execute` endpoints.

POC code repo: <https://github.com/litianningdatadog/lambda-microvm-poc>

The sample app:

- Runs on port `8080`.
- Uses `serverless-init` as PID 1.
- Enables `dd-trace` through `NODE_OPTIONS="--require dd-trace/init"`.
- Implements MicroVM lifecycle hooks under `/aws/lambda-microvms/runtime/v1`.
- Exposes:
  - `GET /health`
  - `POST /execute`

## Datadog component versions

Use [`dd-trace-js` 6.15.0](https://github.com/DataDog/dd-trace-js/releases) or newer. The sample pins `dd-trace` to `6.15.0` in `sample-nodejs-app/package.json` and loads it with:

```dockerfile
ENV NODE_OPTIONS="--require dd-trace/init"
```

Use [`serverless-init` 1.10.4](https://github.com/DataDog/datadog-agent/releases/tag/serverless-init-1.10.4) or newer. The sample copies the ARM64 init binary from the published image and runs it as PID 1:

```dockerfile
COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init
ENTRYPOINT ["/serverless-init"]
```

`dd-trace` instruments the Node.js process. `serverless-init` starts the app, forwards MicroVM lifecycle hooks to the app, and collects stdout/stderr logs for Datadog.

## Prerequisites

From this repository root, make sure you have:

- Docker with Linux ARM64 image build support.
- AWS CLI v2 installed.
- `aws-vault` configured with the `sso-serverless-sandbox-account-admin` profile.
- `jq` installed.
- A Datadog API key in `DD_API_KEY`.
- Lambda MicroVM access in `us-east-2`.
- The default build and execution role used by `deploy-microvm.sh`, or override `BUILD_ROLE_ARN` / `EXECUTION_ROLE_ARN`.

`deploy-microvm.sh` defines its own `awsv` helper:

```zsh
awsv() {
  AWS_PAGER="" aws-vault exec sso-serverless-sandbox-account-admin -- "$@"
}
```

Do not commit `DD_API_KEY` or generated `.microvm-token.*` files. The token file contains a short-lived MicroVM auth token.

## 1. Build, create, deploy, and run the MicroVM

Run this from the repository root:

```bash
export DD_API_KEY=<YOUR_DD_API_KEY>
cd sample-nodejs-app
make build
cd -
export S3_BUCKET=microvm-sample-nodejs-app
./deploy-microvm.sh sample-nodejs-app
```

What happens:

1. `make build` builds the local ARM64 Docker image for `sample-nodejs-app`.
2. `deploy-microvm.sh sample-nodejs-app` zips the app directory with the Dockerfile at the ZIP root.
3. The script uploads the ZIP to `s3://microvm-sample-nodejs-app/<timestamp>.zip`.
4. The script creates a new MicroVM image, or updates the existing `sample-nodejs-app` image with a new version.
5. It waits for the image version to become `SUCCESSFUL`.
6. It runs a MicroVM from the new image version.
7. It creates a 30-minute auth token for `APP_PORT=8080`.
8. It writes the MicroVM connection values to:

```text
<PATH_TO_lambda-microvm-poc>/.microvm-token.sample-nodejs-app
```

The deploy script enables all lifecycle hooks and forwards them through `serverless-init` to the Node.js app.

## 2. Source the MicroVM connection values

Watch the output of `deploy-microvm.sh` and look for the source command it prints. It should look like this:

```bash
source <PATH_TO_lambda-microvm-poc>/.microvm-token.sample-nodejs-app
```

Run that command in your shell. It exports:

- `MICROVM_ID`
- `MICROVM_TOKEN`
- `MICROVM_ENDPOINT`
- `APP_PORT`

If the token expires, rerun `deploy-microvm.sh` or create a fresh token for the same MicroVM.

## 3. Invoke the `/health` endpoint

```bash
curl \
  -H "X-aws-proxy-auth: $MICROVM_TOKEN" \
  -H "X-aws-proxy-port: $APP_PORT" \
  https://$MICROVM_ENDPOINT/health
```

Expected response:

```json
{"status":"healthy"}
```

## 4. Invoke the `/execute` endpoint

```bash
curl \
  -H "X-aws-proxy-auth: $MICROVM_TOKEN" \
  -H "X-aws-proxy-port: $APP_PORT" \
  https://$MICROVM_ENDPOINT/execute \
  -H 'Content-Type: application/json' \
  -d '{"code": "console.log(1 + 2032)"}'
```

Expected response includes `success: true` and the JavaScript output:

```json
{"success":true,"output":"2033\n","stderr":""}
```

## 5. View Datadog telemetry

Open the Lambda MicroVM monitoring dashboard:

<https://ddserverless.datadoghq.com/dashboard/4v6-3ei-kku/lambda-microvm-monitoring>

Use the dashboard to inspect traces, logs, and enhanced MicroVM/runtime metrics for `sample-nodejs-app`.

## Troubleshooting

- If `aws-vault` is not found, install it and configure the `sso-serverless-sandbox-account-admin` profile.
- If image creation fails, check CloudWatch logs under `/aws/lambda-microvms/sample-nodejs-app`.
- If `curl` returns auth errors, source the token file again or mint a fresh token; MicroVM auth tokens expire after 30 minutes.
- If `curl` cannot reach the app, verify `APP_PORT=8080` and that `X-aws-proxy-port` matches it.
