#!/usr/bin/env python3
"""
USER APPLICATION — intentionally minimal.

This file represents what a typical user would write for a MicroVM app.
Note what's NOT here:
  * No imports from datadog / ddtrace.
  * No lifecycle-hook endpoints (those live in the platform's
    /opt/platform/hook_server.py, on port 9000).
  * No DD_HOSTNAME plumbing, no auth_token handling, no agent start/stop.
  * No special stdout redirection — supervisord captures stdout/stderr
    to /var/log/app/app.log, which the Datadog Agent tails.

What the user DOES get, for free, from the platform layer:
  * APM auto-instrumentation via `ddtrace-run` (wired in supervisord.conf).
  * Stdout/stderr captured and shipped as DD logs.
  * DD_HOSTNAME = microVmId so every MicroVM is its own host in DD.
  * Lifecycle events on the DD timeline.
"""

import logging
import os
import sys
from datetime import datetime, timezone

from flask import Flask, jsonify

DD_HOSTNAME = os.environ.get("DD_HOSTNAME", "user-app")

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s - {DD_HOSTNAME} - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


PORT = int(os.environ.get("APP_PORT", "8080"))

app = Flask(__name__)


@app.route("/")
def hello():
    logger.info(f"Request received at {datetime.now(timezone.utc).isoformat()}")
    return jsonify({"message": "Hello from the user app",
                    "hostname": os.uname().nodename})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    logger.info(f"User app starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
