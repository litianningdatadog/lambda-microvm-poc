# sample-django-asgi-app

A Django ASGI sample guest application for Lambda MicroVMs. It mirrors
`sample-fastapi-asgi-app` while using Django's ASGI handler.

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
python3 -m pip install -r requirements.txt
python3 app.py
```

For the ASGI server used by the container:

```bash
uvicorn app:application --host 0.0.0.0 --port 8080
```

The ASGI app listens on port `8080`.

## Tests

```bash
make test
```

## Docker

`make build` follows the same installation flow as `sample-fastapi-asgi-app`: it
uses the repo-local `serverless-init-linux-arm64` and `ddtrace` wheel in this
directory, installs Django/Uvicorn into Python 3.12, installs `ddtrace` into
`/dd_tracer/python`, and runs the app through `serverless-init` and
`ddtrace-run`. The `ddtrace-run` launcher is required so Django's ASGI handler
is instrumented and the MicroVM lifecycle hook can refresh the runtime
identity.

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
