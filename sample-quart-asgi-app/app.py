#!/usr/bin/env python3
"""ASGI sample app for Lambda MicroVM lifecycle hooks."""

import asyncio
import json
import logging
import os
import sys
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from io import StringIO

from quart import Quart, jsonify, request

try:
    from ddtrace import tracer as dd_tracer
except ImportError:
    dd_tracer = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [sample-quart-asgi-app] %(message)s")
logger = logging.getLogger(__name__)

BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080

app = Quart(__name__)
micro_vm_id = None


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _body_preview(body, limit=4096):
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


async def _read_json_body(log_body=False):
    body = await request.get_data(cache=True) or b""

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
    return "", status


def _span(method, path):
    if dd_tracer:
        return dd_tracer.trace("http.request", resource=f"{method} {path}", span_type="web")
    return nullcontext()


@app.get("/health")
async def health():
    with _span("GET", request.path):
        logger.info("Health check called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return jsonify({"status": "healthy"})


@app.post(f"{BASE_PATH}/validate")
async def validate():
    with _span("POST", request.path):
        logger.info("Validate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/ready")
async def ready():
    with _span("POST", request.path):
        logger.info("Ready hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/run")
async def run():
    global micro_vm_id

    with _span("POST", request.path):
        data = await _read_json_body(log_body=True)
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
async def resume():
    with _span("POST", request.path):
        logger.info("Resume hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/suspend")
async def suspend():
    with _span("POST", request.path):
        logger.info("Suspend hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post(f"{BASE_PATH}/terminate")
async def terminate():
    with _span("POST", request.path):
        logger.info("Terminate hook called [ts=%s, microVmId=%s]", _now_ts(), micro_vm_id)
        return _send_empty()


@app.post("/execute")
async def execute_code():
    with _span("POST", request.path):
        try:
            data = await _read_json_body()
            code = data.get("code", "")
            if not code:
                return jsonify({"error": "No code provided"}), 400

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
                return jsonify({"success": False, "error": error, "stderr": captured_err.getvalue()}), 200

            return jsonify({"success": True, "output": captured_out.getvalue(), "stderr": captured_err.getvalue()}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    logger.info("Starting sample-quart-asgi-app on port %s", PORT)
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
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    asyncio.run(serve(app, config))
