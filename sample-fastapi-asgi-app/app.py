#!/usr/bin/env python3
"""FastAPI ASGI sample app for Lambda MicroVM lifecycle hooks."""

import json
import logging
import os
import sys
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from io import StringIO

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

try:
    from ddtrace import tracer as dd_tracer
except ImportError:
    dd_tracer = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [sample-fastapi-asgi-app] %(message)s")
logger = logging.getLogger(__name__)

BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080

app = FastAPI()
micro_vm_id = None


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _body_preview(body, limit=4096):
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


async def _read_json_body(request, log_body=False):
    body = await request.body()

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
    return Response(status_code=status)


def _span(method, path):
    if dd_tracer:
        return dd_tracer.trace("http.request", resource=f"{method} {path}", span_type="web")
    return nullcontext()


@app.get("/health")
async def health(request: Request):
    with _span("GET", request.url.path):
        logger.info("Health check called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return {"status": "healthy"}


@app.post(f"{BASE_PATH}/validate")
async def validate(request: Request):
    with _span("POST", request.url.path):
        logger.info("Validate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/ready")
async def ready(request: Request):
    with _span("POST", request.url.path):
        logger.info("Ready hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/run")
async def run(request: Request):
    global micro_vm_id

    with _span("POST", request.url.path):
        data = await _read_json_body(request, log_body=True)
        micro_vm_id = _first_present(data, "microVmId", "microvmId")
        mesh_ipv6_address = _first_present(data, "meshIpv6Address", "meshIPv6Address", "meshIpv6")

        logger.info(
            "Run hook called [ts=%s, microVmId=%s, meshIpv6Address=%s]",
            _now_ts(),
            micro_vm_id,
            mesh_ipv6_address,
        )
        return _send_empty()


@app.post(f"{BASE_PATH}/resume")
async def resume(request: Request):
    with _span("POST", request.url.path):
        logger.info("Resume hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/suspend")
async def suspend(request: Request):
    with _span("POST", request.url.path):
        logger.info("Suspend hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/terminate")
async def terminate(request: Request):
    with _span("POST", request.url.path):
        logger.info("Terminate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post("/execute")
async def execute_code(request: Request):
    with _span("POST", request.url.path):
        try:
            data = await _read_json_body(request)
            code = data.get("code", "")
            if not code:
                return JSONResponse({"error": "No code provided"}, status_code=400)

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
                return {"success": False, "error": error, "stderr": captured_err.getvalue()}

            return {"success": True, "output": captured_out.getvalue(), "stderr": captured_err.getvalue()}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting sample-fastapi-asgi-app on port %s", PORT)
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
    uvicorn.run(app, host="0.0.0.0", port=PORT)
