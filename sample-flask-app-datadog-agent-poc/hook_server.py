#!/usr/bin/env python3
"""
Lambda MicroVM lifecycle-hook server — PLATFORM-OWNED process.

This is NOT user code. It lives at /opt/platform/hook_server.py inside
the container and is run by supervisord. The user's application code
(in /app/app.py) has zero awareness of this process, the Datadog Agent,
or the MicroVM lifecycle-hook contract.

Responsibilities:
  1. Own port 9000 — answer the five MicroVM lifecycle hooks.
  2. At /launch, capture microVmId from the payload, render
     /etc/datadog-agent/datadog.yaml with DD_HOSTNAME=microVmId,
     then `supervisorctl start datadog-agent`.
  3. Emit a DogStatsD event + counter on every hook so the
     suspend/resume/terminate timeline is visible in Datadog.
  4. At /terminate, call `datadog-agent stop` (graceful flush via the
     agent's own IPC socket — cleaner than supervisorctl stop, which
     would SIGTERM the agent before it's finished flushing).

This file is identical for every MicroVM app; it's platform infrastructure.
"""

import logging
import os
import re
import string
import subprocess
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from datadog import initialize as dd_initialize
from datadog import statsd

BASE_PATH = "/aws/lambda-microvms/runtime/beta/v1"
PORT = 9000

DD_CONFIG_TEMPLATE = "/etc/datadog-agent/datadog.yaml.template"
DD_CONFIG_RENDERED = "/etc/datadog-agent/datadog.yaml"
DD_AGENT_AUTH_TOKEN = "/etc/datadog-agent/auth_token"
DD_AGENT_BINARY = "/opt/datadog-agent/bin/agent/agent"
DD_AGENT_IPC_SOCKET = "/var/run/datadog/agent_ipc.socket"
SUPERVISORD_CONFIG = "/etc/supervisord.conf"

# Must match the [program:user-app] block name in supervisord.conf.
# /ready asks supervisord about this program; any mismatch = permanent 403.
USER_APP_PROGRAM_NAME = "user-app"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - hook-server - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# DogStatsD client to the Agent at 127.0.0.1:8125. Drops UDP packets
# silently if the Agent isn't up yet (everything before /launch).
dd_initialize(statsd_host="localhost", statsd_port=8125)


app = Flask(__name__)

micro_vm_id: str | None = None


def _emit_hook_event(hook_name: str, extra_text: str = "") -> None:
    tags = [f"hook:{hook_name}"]
    if micro_vm_id:
        tags.append(f"microvm_id:{micro_vm_id}")
    try:
        # datadog.statsd.event (DogStatsd client) uses `message=`, not
        # `text=`. `text=` belongs to datadog.api.Event.create (backend
        # REST API), which is a different product surface entirely.
        statsd.event(
            title=f"microvm.{hook_name}",
            message=extra_text or f"Lifecycle hook '{hook_name}' fired at {_now_ts()}",
            alert_type="info",
            tags=tags,
        )
        statsd.increment("microvm.lifecycle.hook", tags=tags)
    except Exception as e:  # noqa: BLE001 — never let DD failures break a hook
        logger.warning(f"DogStatsD emit failed for hook={hook_name}: {e}")


def _render_datadog_yaml(hostname: str) -> None:
    """Render datadog.yaml with hostname=microVmId. Called from /launch
    before the Agent is started — the rendered file is what the Agent
    will read on its first process-boot inside this specific clone.

    Auto-discovers `${VAR}` placeholders in the template and pulls
    values from `os.environ`, so adding a new tag like
    `microvm_image:${AWS_LAMBDA_FUNCTION_ARN}` to the template doesn't
    require a code change here. `DD_HOSTNAME` is the one exception —
    we always override it with the per-clone microVmId regardless of
    the container env."""
    try:
        with open(DD_CONFIG_TEMPLATE, "r") as f:
            template_text = f.read()
        placeholders = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", template_text))
        substitutions = {k: os.environ.get(k, "") for k in placeholders}
        substitutions["DD_HOSTNAME"] = hostname
        rendered = string.Template(template_text).substitute(**substitutions)
        with open(DD_CONFIG_RENDERED, "w") as f:
            f.write(rendered)
        if os.path.exists(DD_AGENT_AUTH_TOKEN):
            os.unlink(DD_AGENT_AUTH_TOKEN)
        logger.info(
            f"Rendered datadog.yaml [ts={_now_ts()}, hostname={hostname}, "
            f"placeholders={sorted(placeholders)}]"
        )
    except FileNotFoundError:
        logger.warning("datadog.yaml.template not found; skipping render (local mode?)")


def _supervisor_start_agent() -> None:
    """Start the datadog-agent program via supervisorctl.

    Timeout note: `supervisorctl start` waits for the program to reach
    RUNNING state (i.e., survive its startsecs window), which for the
    agent can take longer than the 10 s we originally used —
    particularly under /suspend's stop+start pre-warm path where the
    previous agent process has just finished shutting down. 30 s gives
    plenty of headroom while still bounding failure cases."""
    try:
        result = subprocess.run(
            ["supervisorctl", "-c", SUPERVISORD_CONFIG, "start", "datadog-agent"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"Started datadog-agent [ts={_now_ts()}] {result.stdout.strip()}")
        else:
            logger.warning(
                f"supervisorctl start datadog-agent rc={result.returncode}: "
                f"{result.stderr.strip()}"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Could not start datadog-agent via supervisorctl: {e}")


def _user_app_ready() -> bool:
    """Ask supervisord whether the user-app program is in the RUNNING state.

    Process-based readiness — language- and framework-agnostic. Only the
    literal string 'RUNNING' in supervisorctl's status output counts as
    ready; all other states (STARTING, BACKOFF, FATAL, EXITED, STOPPED,
    STOPPING, UNKNOWN) mean keep polling.

    Supervisorctl's exit code is unreliable here (returns 0 for a known
    program regardless of its state, non-zero only for unknown programs),
    so we parse the stdout line."""
    try:
        result = subprocess.run(
            ["supervisorctl", "-c", SUPERVISORD_CONFIG,
             "status", USER_APP_PROGRAM_NAME],
            capture_output=True, text=True, timeout=5,
        )
        # Output line looks like:
        #   user-app                         RUNNING   pid 14, uptime 0:00:04
        #   user-app                         STARTING
        return " RUNNING " in result.stdout or result.stdout.rstrip().endswith(" RUNNING")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Could not query supervisord for user-app state: {e}")
        return False


def _agent_graceful_stop() -> None:
    """Graceful shutdown via the agent's own IPC socket. This triggers the
    agent's internal flush path (pending traces, buffered metric points,
    in-flight log batches) before the process exits — cleaner than a
    supervisorctl stop, which would SIGTERM the process mid-flush."""
    try:
        result = subprocess.run(
            [DD_AGENT_BINARY, "stop"],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode == 0:
            logger.info(f"datadog-agent stopped gracefully [ts={_now_ts()}]")
        else:
            logger.warning(
                f"agent stop rc={result.returncode}: {result.stderr.strip()}"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Could not gracefully stop datadog-agent: {e}")


# --- Hook endpoints --------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    logger.info(f"Health check [ts={_now_ts()}, microVmId={micro_vm_id}]")
    return jsonify({"status": "healthy", "microVmId": micro_vm_id})


@app.route(f"{BASE_PATH}/ready", methods=["POST"])
def ready():
    """Platform polls this during IMAGE BUILD (before the snapshot is taken).
    Gated on supervisord reporting the user-app program as RUNNING —
    purely process-based, independent of the user app's language or
    framework. 200 when running, 403 otherwise so the platform keeps
    polling. This prevents the snapshot from being taken before the user
    app's process is alive.

    The Agent MUST NOT be started here — it'd get baked into the snapshot."""
    if _user_app_ready():
        logger.info(f"Ready hook [ts={_now_ts()}] user-app RUNNING → 200")
        return "", 200
    logger.info(f"Ready hook [ts={_now_ts()}] user-app not RUNNING → 403")
    return "", 403


@app.route(f"{BASE_PATH}/launch", methods=["POST"])
def launch():
    """CLONED resume type — fires after a fresh snapshot clone. This is
    where per-clone Datadog identity gets installed."""
    global micro_vm_id
    data = request.get_json(silent=True) or {}
    micro_vm_id = data.get("microVmId") or f"unknown-{_now_ts()}"
    mesh_ipv6 = data.get("meshIpv6Address")

    logger.info(
        f"Launch hook [ts={_now_ts()}, microVmId={micro_vm_id}, meshIpv6={mesh_ipv6}]"
    )
    _render_datadog_yaml(hostname=micro_vm_id)
    _supervisor_start_agent()
    _emit_hook_event("launch", extra_text=f"microVmId={micro_vm_id} meshIpv6={mesh_ipv6}")
    return "", 200


@app.route(f"{BASE_PATH}/resume", methods=["POST"])
def resume():
    """PAUSED resume type — same VM waking from SUSPENDED. The agent
    was pre-warmed at the end of /suspend (stop → flush → restart), so
    the Firecracker freeze captured an already-running agent; on
    unfreeze the agent just picks back up. No agent action needed
    here — this path is intentionally cheap so /resume doesn't add
    user-visible latency. The agent's TCP connections to DD intake
    will re-establish automatically on next flush."""
    logger.info(f"Resume hook [ts={_now_ts()}, microVmId={micro_vm_id}]")
    if micro_vm_id is None:
        # /launch hasn't fired yet — agent was never properly configured.
        # Skip event emission to avoid polluting DD with pre-launch tags.
        logger.warning("Resume fired before /launch — skipping event emission")
        return "", 200
    _emit_hook_event("resume")
    return "", 200


@app.route(f"{BASE_PATH}/suspend", methods=["POST"])
def suspend():
    """120s timeout. Do all the expensive agent work HERE rather than
    on /resume, because /suspend is user-invisible (the VM is already
    idle) while /resume is on the hot path when traffic returns.

    Three steps, all before returning 200:
      1. Gracefully stop the agent via `datadog-agent stop` — drives
         its internal flush path (pending metric points, in-flight log
         batches, trace payloads) through to DD intake before exit.
         Critical: otherwise data is lost if the platform takes us
         from SUSPENDED → TERMINATING without firing /terminate.
      2. supervisorctl start — bring up a fresh agent immediately.
      3. Return 200. The platform freezes the VM with the fresh agent
         already running; on /resume, it's already there.

    supervisord's autorestart=unexpected + exitcodes=0 means step 1's
    clean exit doesn't trigger supervisor's own auto-relaunch — step 2
    is the only path that brings the agent back. Total /suspend budget
    ~10s against the 120s timeout.

    IMPORTANT: if /launch hasn't fired yet (micro_vm_id is None), we
    skip ALL agent operations. The platform has been observed to fire
    /suspend during image-build snapshot verification BEFORE /launch
    delivers the microVmId — starting the agent here would bake the
    pre-launch placeholder hostname into the agent's in-memory config
    and subsequent /launch re-renders would have no effect (the agent
    only reads datadog.yaml at process start)."""
    logger.info(f"Suspend hook [ts={_now_ts()}, microVmId={micro_vm_id}]")
    if micro_vm_id is None:
        logger.warning(
            "Suspend fired before /launch — skipping agent stop+restart "
            "to avoid starting the agent with pre-launch config"
        )
        return "", 200
    _emit_hook_event("suspend")
    statsd.flush()
    _agent_graceful_stop()
    _supervisor_start_agent()
    return "", 200


@app.route(f"{BASE_PATH}/terminate", methods=["POST"])
def terminate():
    """Emit final event, then drive the agent's graceful shutdown path via
    its own IPC socket. The agent has ~60s (the /terminate timeout) to
    flush everything pending before the platform tears us down.

    If /launch never fired, the agent was never started — skip the stop."""
    logger.info(f"Terminate hook [ts={_now_ts()}, microVmId={micro_vm_id}]")
    if micro_vm_id is None:
        logger.warning("Terminate fired before /launch — nothing to flush")
        return "", 200
    _emit_hook_event("terminate")
    statsd.flush()
    _agent_graceful_stop()
    return "", 200


if __name__ == "__main__":
    logger.info(f"Starting hook-server on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
