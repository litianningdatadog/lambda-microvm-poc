#!/usr/bin/env python3
"""Django ASGI sample app for Lambda MicroVM lifecycle hooks."""

import json
import logging
import os
import sys
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from io import StringIO

from django.conf import settings

BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080
APP_NAME = "sample-django-asgi-app"

if not settings.configured:
    settings.configure(
        ALLOWED_HOSTS=["*"],
        DEBUG=True,
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
        ROOT_URLCONF=__name__,
        SECRET_KEY=APP_NAME,
    )

import django  # noqa: E402
from django.core.asgi import get_asgi_application  # noqa: E402
from django.http import HttpRequest, HttpResponse, JsonResponse  # noqa: E402
from django.urls import path  # noqa: E402

try:
    from ddtrace import tracer as dd_tracer
except ImportError:
    dd_tracer = None

django.setup()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [sample-django-asgi-app] %(message)s")
logger = logging.getLogger(__name__)
micro_vm_id = None


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _body_preview(body, limit=4096):
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


def _read_json_body(request, log_body=False):
    body = request.body or b""

    if log_body:
        logger.info(
            "Run hook request body [contentLength=%s, transferEncoding=%s, contentType=%s, bodyBytes=%d, body=%r]",
            request.headers.get("Content-Length"),
            request.headers.get("Transfer-Encoding"),
            request.headers.get("Content-Type"),
            len(body),
            _body_preview(body),
        )

    if not body.strip():
        return {}
    return json.loads(body.decode("utf-8"))


def _first_present(data, *keys):
    for key in keys:
        if key in data:
            return data[key]
    return None


def _send_empty(status=200):
    return HttpResponse(status=status)


def _span(method, path):
    if dd_tracer:
        return dd_tracer.trace("http.request", resource=f"{method} {path}", span_type="web")
    return nullcontext()


def health(request: HttpRequest):
    with _span("GET", request.path):
        logger.info("Health check called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return JsonResponse({"status": "healthy"})


def validate(request: HttpRequest):
    with _span("POST", request.path):
        logger.info("Validate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


def ready(request: HttpRequest):
    with _span("POST", request.path):
        logger.info("Ready hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


def run(request: HttpRequest):
    global micro_vm_id

    with _span("POST", request.path):
        data = _read_json_body(request, log_body=True)
        micro_vm_id = _first_present(data, "microVmId", "microvmId")
        mesh_ipv6_address = _first_present(data, "meshIpv6Address", "meshIPv6Address", "meshIpv6")

        logger.info(
            "Run hook called [ts=%s, microVmId=%s, meshIpv6Address=%s]",
            _now_ts(),
            micro_vm_id,
            mesh_ipv6_address,
        )
        return _send_empty()


def resume(request: HttpRequest):
    with _span("POST", request.path):
        logger.info("Resume hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


def suspend(request: HttpRequest):
    with _span("POST", request.path):
        logger.info("Suspend hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


def terminate(request: HttpRequest):
    with _span("POST", request.path):
        logger.info("Terminate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


def execute_code(request: HttpRequest):
    with _span("POST", request.path):
        try:
            data = _read_json_body(request)
            code = data.get("code", "")
            if not code:
                return JsonResponse({"error": "No code provided"}, status=400)

            logger.info("Execute called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)

            old_stdout, old_stderr = sys.stdout, sys.stderr
            captured_out, captured_err = StringIO(), StringIO()
            sys.stdout, sys.stderr = captured_out, captured_err

            error = None
            try:
                exec(code, {})  # noqa: S102
            except Exception:
                error = traceback.format_exc()
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr

            if error:
                return JsonResponse({"success": False, "error": error, "stderr": captured_err.getvalue()})

            return JsonResponse({"success": True, "output": captured_out.getvalue(), "stderr": captured_err.getvalue()})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


urlpatterns = [
    path("health", health),
    path(f"{BASE_PATH.lstrip('/')}/validate", validate),
    path(f"{BASE_PATH.lstrip('/')}/ready", ready),
    path(f"{BASE_PATH.lstrip('/')}/run", run),
    path(f"{BASE_PATH.lstrip('/')}/resume", resume),
    path(f"{BASE_PATH.lstrip('/')}/suspend", suspend),
    path(f"{BASE_PATH.lstrip('/')}/terminate", terminate),
    path("execute", execute_code),
]

application = get_asgi_application()


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting %s on port %s", APP_NAME, PORT)
    env_lines = "\n".join(f"  {k}={v}" for k, v in sorted(os.environ.items()) if k != "DD_API_KEY")
    logger.info("Environment variables:\n%s", env_lines)
    print(f"""
Sample commands (server running on port {PORT}):

  curl http://127.0.0.1:{PORT}/health

  curl -X POST http://127.0.0.1:{PORT}{BASE_PATH}/ready

  curl -X POST http://127.0.0.1:{PORT}{BASE_PATH}/run \\
    -H 'Content-Type: application/json' \\
    -d '{{"microVmId": "hello_world", "meshIpv6Address": "::1"}}'

  curl -X POST http://127.0.0.1:{PORT}{BASE_PATH}/resume

  curl -X POST http://127.0.0.1:{PORT}{BASE_PATH}/suspend

  curl -X POST http://127.0.0.1:{PORT}{BASE_PATH}/terminate

  curl -X POST http://127.0.0.1:{PORT}/execute \\
    -H 'Content-Type: application/json' \\
    -d '{{"code": "print(1 + 1)"}}'
""")
    uvicorn.run(application, host="0.0.0.0", port=PORT)
