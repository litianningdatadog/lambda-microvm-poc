#!/usr/bin/env python3
"""
Sample guest application that implements Lambda MicroVMs lifecycle hooks.

This application listens on port 9000 and implements ready, launch, resume,
suspend, and terminate hooks.

Endpoints:
- POST /aws/lambda-microvms/runtime/v1/validate
- POST /aws/lambda-microvms/runtime/v1/ready
- POST /aws/lambda-microvms/runtime/v1/run
- POST /aws/lambda-microvms/runtime/v1/resume
- POST /aws/lambda-microvms/runtime/v1/suspend
- POST /aws/lambda-microvms/runtime/v1/terminate
"""

import logging
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from flask import Flask, request, jsonify
import traceback

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _now_ts():
    return datetime.now(timezone.utc).isoformat()


BASE_PATH = "/aws/lambda-microvms/runtime/v1"
PORT = 8080

app = Flask(__name__)

micro_vm_id = None


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    logger.info(f"Health check called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return jsonify({"status": "healthy"})


@app.route(f"{BASE_PATH}/validate", methods=["POST"])
def validate():
    """Handle validate hook from Lambda MicroVMs."""
    logger.info(f"Validate hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return "", 200


@app.route(f"{BASE_PATH}/ready", methods=["POST"])
def ready():
    """Handle ready hook from Lambda MicroVMs."""
    logger.info(f"Ready hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return "", 200


@app.route(f"{BASE_PATH}/run", methods=["POST"])
def run():
    """Handle launch hook from Lambda MicroVMs (CLONED resume type)."""
    global micro_vm_id
    data = request.get_json() or {}

    micro_vm_id = data.get("microVmId")
    mesh_ipv6_address = data.get("meshIpv6Address")

    logger.info(f"Run hook called — ts={_now_ts()}, microVmId={micro_vm_id}, meshIpv6Address={mesh_ipv6_address}")

    return "", 200


@app.route(f"{BASE_PATH}/resume", methods=["POST"])
def resume():
    """Handle resume hook from Lambda MicroVMs (PAUSED resume type)."""
    logger.info(f"Resume hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return "", 200


@app.route(f"{BASE_PATH}/suspend", methods=["POST"])
def suspend():
    """Handle suspend hook from Lambda MicroVMs."""
    logger.info(f"Suspend hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return "", 200


@app.route(f"{BASE_PATH}/terminate", methods=["POST"])
def terminate():
    """Handle terminate hook from Lambda MicroVMs."""
    logger.info(f"Terminate hook called [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return "", 200


@app.route('/execute', methods=['POST'])
def execute_code():
    try:
        code = request.json.get('code', '')
        if not code:
            return jsonify({'error': 'No code provided'}), 400

        # Capture stdout and stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirected_output = StringIO()
        redirected_error = StringIO()
        sys.stdout = redirected_output
        sys.stderr = redirected_error

        result = None
        error = None

        logger.info(f"Execute called [ts={_now_ts()}, microVmId={micro_vm_id}]")

        try:
            # Execute the code
            exec_globals = {}
            exec(code, exec_globals)
            result = redirected_output.getvalue()
        except Exception as e:
            error = traceback.format_exc()
        finally:
            # Restore stdout and stderr
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        if error:
            return jsonify({
                'success': False,
                'error': error,
                'stderr': redirected_error.getvalue()
            }), 200

        return jsonify({
            'success': True,
            'output': result,
            'stderr': redirected_error.getvalue()
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _log_env_vars():
    """Dump every env var, one per line, sorted — so hook logs can be
    grepped by variable name in CloudWatch. No redaction: assume the log
    sink is trusted (inside the MicroVM boundary)."""
    env = os.environ
    logger.info(f"Environment dump ({len(env)} vars) [ts={_now_ts()}]")
    for key in sorted(env):
        logger.info(f"  env {key}={env[key]}")


if __name__ == "__main__":
    logger.info(f"Starting sample guest application on port {PORT}")
    _log_env_vars()
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
