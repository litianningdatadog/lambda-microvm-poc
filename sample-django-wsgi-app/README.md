# sample-django-wsgi-app

A Django WSGI sample guest application for Lambda MicroVMs. It mirrors
`sample-flask-app` while using Django's WSGI handler.

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

For the WSGI server used by the container:

```bash
gunicorn --bind 0.0.0.0:8080 app:application
```

The WSGI app listens on port `8080`.

## Tests

```bash
make test
```

## Docker

`make build` follows the same installation flow as `sample-flask-app`: it
copies the configured local `ddtrace` wheel into this directory, installs
Django/Gunicorn into Python 3.12, installs `ddtrace` into `/dd_tracer/python`,
and runs the app through `serverless-init`. Both Dockerfiles copy
`/datadog-init` directly from `datadog/serverless-init:1.11.4`; no local init
binary is required.

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
`serverless-init` is exposed on port `9000` and forwards hook calls to the WSGI
app.
