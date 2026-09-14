#!/usr/bin/env python3
"""
Sample Flask guest application that implements Lambda MicroVMs lifecycle hooks.

Endpoints:
- GET  /health
- POST /aws/lambda-microvms/runtime/v1/ready
- POST /aws/lambda-microvms/runtime/v1/validate
- POST /aws/lambda-microvms/runtime/v1/run
- POST /aws/lambda-microvms/runtime/v1/resume
- POST /aws/lambda-microvms/runtime/v1/suspend
- POST /aws/lambda-microvms/runtime/v1/terminate
- POST /execute
"""

import json
import os
import sys
import traceback
from contextlib import nullcontext
from io import StringIO

import structlog
from flask import Flask, jsonify, request

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)
logger = structlog.get_logger().bind(service="sample-flask-app-without-tracer")

BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080

app = Flask(__name__)
micro_vm_id = None


def _body_preview(body, limit=4096):
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


def _read_json_body(log_body=False):
    body = request.get_data(cache=True) or b""

    if log_body:
        logger.info(
            "run_hook_request_body",
            content_length=request.headers.get("Content-Length"),
            transfer_encoding=request.headers.get("Transfer-Encoding"),
            content_type=request.headers.get("Content-Type"),
            body_bytes=len(body),
            body=_body_preview(body),
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


def _span(_method, _path):
    return nullcontext()


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    with _span("GET", request.path):
        logger.info("health_check_called", micro_vm_id=micro_vm_id)
        return jsonify({"status": "healthy"})


@app.route(f"{BASE_PATH}/validate", methods=["POST"])
def validate():
    """Handle validate hook from Lambda MicroVMs."""
    with _span("POST", request.path):
        logger.info("validate_hook_called", micro_vm_id=micro_vm_id)
        return _send_empty()


@app.route(f"{BASE_PATH}/ready", methods=["POST"])
def ready():
    """Handle ready hook from Lambda MicroVMs."""
    with _span("POST", request.path):
        logger.info("ready_hook_called", micro_vm_id=micro_vm_id)
        return _send_empty()


@app.route(f"{BASE_PATH}/run", methods=["POST"])
def run():
    """Handle run hook from Lambda MicroVMs."""
    global micro_vm_id

    with _span("POST", request.path):
        data = _read_json_body(log_body=True)
        micro_vm_id = _first_present(data, "microVmId", "microvmId")
        mesh_ipv6_address = _first_present(data, "meshIpv6Address", "meshIPv6Address", "meshIpv6")

        logger.info(
            "run_hook_called",
            micro_vm_id=micro_vm_id,
            mesh_ipv6_address=mesh_ipv6_address,
        )
        return _send_empty()


@app.route(f"{BASE_PATH}/resume", methods=["POST"])
def resume():
    """Handle resume hook from Lambda MicroVMs."""
    with _span("POST", request.path):
        logger.info("resume_hook_called", micro_vm_id=micro_vm_id)
        return _send_empty()


@app.route(f"{BASE_PATH}/suspend", methods=["POST"])
def suspend():
    """Handle suspend hook from Lambda MicroVMs."""
    with _span("POST", request.path):
        logger.info("suspend_hook_called", micro_vm_id=micro_vm_id)
        return _send_empty()


@app.route(f"{BASE_PATH}/terminate", methods=["POST"])
def terminate():
    """Handle terminate hook from Lambda MicroVMs."""
    with _span("POST", request.path):
        logger.info("terminate_hook_called", micro_vm_id=micro_vm_id)
        return _send_empty()


@app.route("/execute", methods=["POST"])
def execute_code():
    with _span("POST", request.path):
        try:
            data = _read_json_body()
            code = data.get("code", "")
            if not code:
                return jsonify({"error": "No code provided"}), 400

            logger.info("execute_called", micro_vm_id=micro_vm_id)

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
                return jsonify({
                    "success": False,
                    "error": error,
                    "stderr": captured_err.getvalue(),
                }), 200

            return jsonify({
                "success": True,
                "output": captured_out.getvalue(),
                "stderr": captured_err.getvalue(),
            }), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    logger.info("app_starting", port=PORT)
    env_lines = '\n'.join(f'  {k}={v}' for k, v in sorted(os.environ.items()) if k != 'DD_API_KEY')
    logger.info("environment_variables", values=env_lines)
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
    app.run(host="0.0.0.0", port=PORT)
