# sample-fastapi-asgi-app

A FastAPI ASGI sample guest application for Lambda MicroVMs. It mirrors
`sample-flask-app` while using FastAPI's ASGI model.

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

The ASGI app listens on port `8080`.

## Docker

`make build` follows the same installation flow as `sample-flask-app`: it
copies `serverless-init-linux-arm64`, copies the configured local `ddtrace`
wheel, installs FastAPI/Uvicorn into Python 3.12, installs `ddtrace` into
`/dd_tracer/python`, and runs the app through `serverless-init`.

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
`serverless-init` is exposed on port `9000` and forwards hook calls to the ASGI
app.
