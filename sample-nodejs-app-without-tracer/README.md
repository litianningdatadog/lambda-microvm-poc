# sample-nodejs-app-without-tracer

A Node.js sample guest application for Lambda MicroVMs. This is a copy of
[`sample-nodejs-app/`](../sample-nodejs-app/) without `dd-trace` or
`NODE_OPTIONS=--require dd-trace/init`.

It implements Lambda MicroVM lifecycle hooks plus a `/execute` endpoint that
evaluates JavaScript via the built-in [`vm`](https://nodejs.org/api/vm.html)
module with a 5-second timeout and a sandboxed `console`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/health` | Health check |
| POST | `/aws/lambda-microvms/runtime/v1/validate` | Image validation hook |
| POST | `/aws/lambda-microvms/runtime/v1/ready` | Image ready hook |
| POST | `/aws/lambda-microvms/runtime/v1/run` | Runtime run hook |
| POST | `/aws/lambda-microvms/runtime/v1/resume` | Runtime resume hook |
| POST | `/aws/lambda-microvms/runtime/v1/suspend` | Runtime suspend hook |
| POST | `/aws/lambda-microvms/runtime/v1/terminate` | Runtime terminate hook |
| POST | `/execute` | Run a JavaScript snippet (`{"code": "..."}`) |

## Running locally

```bash
npm install
node app.js
```

The application listens on port `8080`.

## Docker

```bash
make build
make start
make check
make stop
```

For the local-debug image with the expanded Datadog environment:

```bash
make build-local
make start
make check
make stop
```

The Docker image keeps `serverless-init` as the entrypoint, but starts the app
plainly with `node app.js` and does not install or preload `dd-trace`.
