#!/usr/bin/env python3
"""
Sample guest application that implements Lambda MicroVMs lifecycle hooks.

Uses only the Python standard library (no external dependencies).

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
import logging
import os
import sys
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

try:
    from ddtrace import tracer as dd_tracer
except ImportError:
    dd_tracer = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [sample-python-app] %(message)s')
logger = logging.getLogger(__name__)

BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080

micro_vm_id = None


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


def _body_preview(body, limit=4096):
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}...<truncated {len(text) - limit} chars>"
    return text


def _read_json_body(handler, log_body=False):
    transfer_encoding = handler.headers.get("Transfer-Encoding", "").lower()
    if "chunked" in transfer_encoding:
        body = _read_chunked_body(handler)
    else:
        length = int(handler.headers.get("Content-Length", 0))
        body = handler.rfile.read(length) if length else b""

    if log_body:
        logger.info(
            "Run hook request body [contentLength=%s, transferEncoding=%s, contentType=%s, bodyBytes=%d, body=%r]",
            handler.headers.get("Content-Length"),
            handler.headers.get("Transfer-Encoding"),
            handler.headers.get("Content-Type"),
            len(body),
            _body_preview(body),
        )

    if not body.strip():
        return {}
    return json.loads(body.decode("utf-8"))


def _read_chunked_body(handler):
    chunks = []
    while True:
        line = handler.rfile.readline()
        if not line:
            break

        size_text = line.split(b";", 1)[0].strip()
        chunk_size = int(size_text, 16)
        if chunk_size == 0:
            # Consume any trailer headers plus the final blank line.
            while True:
                trailer = handler.rfile.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break

        chunks.append(handler.rfile.read(chunk_size))
        handler.rfile.read(2)

    return b"".join(chunks)


def _first_present(data, *keys):
    for key in keys:
        if key in data:
            return data[key]
    return None


def _send_json(handler, status, body):
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", len(payload))
    handler.end_headers()
    handler.wfile.write(payload)


def _send_empty(handler, status=200):
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _span(method, path):
    if dd_tracer:
        return dd_tracer.trace("http.request", resource=f"{method} {path}", span_type="web")
    return nullcontext()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log; we use our own logger

    def do_GET(self):
        with _span("GET", self.path):
            if self.path == "/health":
                logger.info(f"Health check called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_json(self, 200, {"status": "healthy"})
            else:
                _send_empty(self, 404)

    def do_POST(self):
        global micro_vm_id

        with _span("POST", self.path):
            if self.path == f"{BASE_PATH}/validate":
                logger.info(f"Validate hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_empty(self)

            elif self.path == f"{BASE_PATH}/ready":
                logger.info(f"Ready hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_empty(self)

            elif self.path == f"{BASE_PATH}/run":
                data = _read_json_body(self, log_body=True)
                micro_vm_id = _first_present(data, "microVmId", "microvmId")
                mesh_ipv6_address = _first_present(data, "meshIpv6Address", "meshIPv6Address", "meshIpv6")
                logger.info(f"Run hook called — ts={_now_ts()}, microVmId={micro_vm_id}, meshIpv6Address={mesh_ipv6_address}")
                _send_empty(self)

            elif self.path == f"{BASE_PATH}/resume":
                logger.info(f"Resume hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_empty(self)

            elif self.path == f"{BASE_PATH}/suspend":
                logger.info(f"Suspend hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_empty(self)

            elif self.path == f"{BASE_PATH}/terminate":
                logger.info(f"Terminate hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
                _send_empty(self)

            elif self.path == "/execute":
                self._handle_execute()

            else:
                _send_empty(self, 404)

    def _handle_execute(self):
        try:
            data = _read_json_body(self)
            code = data.get("code", "")
            if not code:
                _send_json(self, 400, {"error": "No code provided"})
                return

            logger.info(f"Execute called [ts={_now_ts()}, microVmId={micro_vm_id}]")

            old_stdout, old_stderr = sys.stdout, sys.stderr
            captured_out, captured_err = StringIO(), StringIO()
            sys.stdout, sys.stderr = captured_out, captured_err

            error = None
            try:
                # sandbox: executes caller-supplied Python code in an isolated globals dict
                exec(code, {})  # noqa: S102
            except Exception:
                error = traceback.format_exc()
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr

            if error:
                _send_json(self, 200, {
                    "success": False,
                    "error": error,
                    "stderr": captured_err.getvalue(),
                })
            else:
                _send_json(self, 200, {
                    "success": True,
                    "output": captured_out.getvalue(),
                    "stderr": captured_err.getvalue(),
                })
        except Exception as e:
            _send_json(self, 500, {"error": str(e)})


if __name__ == "__main__":
    logger.info(f"Starting sample-python-app on port {PORT}")
    env_lines = '\n'.join(f'  {k}={v}' for k, v in sorted(os.environ.items()) if k != 'DD_API_KEY')
    logger.info(f"Environment variables:\n{env_lines}")
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
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
