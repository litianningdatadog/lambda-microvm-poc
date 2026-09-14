# sample-flask-app-without-tracer

A Flask sample guest application for Lambda MicroVMs. This is a copy of
`sample-flask-app` that starts the app directly with Python instead of
`ddtrace-run`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/aws/lambda-microvms/runtime/v1/validate` | Image validation hook |
| POST | `/aws/lambda-microvms/runtime/v1/ready` | Image ready hook |
| POST | `/aws/lambda-microvms/runtime/v1/run` | Runtime run hook |
| POST | `/aws/lambda-microvms/runtime/v1/resume` | Runtime resume hook |
| POST | `/aws/lambda-microvms/runtime/v1/suspend` | Runtime suspend hook |
| POST | `/aws/lambda-microvms/runtime/v1/terminate` | Runtime terminate hook |
| POST | `/execute` | Execute a Python snippet |

## Local Python

```bash
python3.12 -m pip install -r requirements.txt
python3.12 app.py
```

The Flask app listens on port `8080`.

## Docker

The Docker image installs Flask into Python 3.12 and runs the app through
`serverless-init`, but without installing the local `ddtrace` wheel or wrapping
the process with `ddtrace-run`.

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

The user app is exposed on port `8080`. The lifecycle hook server provided by
`serverless-init` is exposed on port `9000` and forwards hook calls to the Flask
app.
