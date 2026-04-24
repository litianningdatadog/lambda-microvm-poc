#!/usr/bin/env bash
# =============================================================================
# MicroVM + Datadog Agent entrypoint.
#
# This runs once at container start (and again on every MicroVM clone that
# starts from the Firecracker snapshot). It does three things:
#
#   1. Wipes /etc/datadog-agent/auth_token  — safety net in case the RPM
#      post-install hook wrote one. Each clone must regenerate its own.
#
#   2. Renders /etc/datadog-agent/datadog.yaml from the template using
#      whatever env vars are available at container-boot. DD_HOSTNAME is
#      set to a placeholder here; the platform's hook_server.py re-renders
#      it with the real microVmId BEFORE starting the agent.
#
#   3. Execs supervisord as PID 1.  Supervisor starts user-app and
#      hook-server immediately (autostart=true) and keeps datadog-agent
#      dormant (autostart=false) until /launch starts it.
# =============================================================================
set -euo pipefail

# Pick up DD_API_KEY etc. that deploy-microvm.sh shipped into the image
# at zip time. No-op for local `docker run` (no such file); pass via
# `-e DD_API_KEY=...` instead in that case.
# shellcheck disable=SC1091
[[ -f /opt/platform/.dd-env ]] && source /opt/platform/.dd-env

rm -f /etc/datadog-agent/auth_token

# Initial datadog.yaml render — just produces a syntactically-valid file.
# The agent is dormant until /launch; hook_server.py re-renders the same
# template at /launch with the real microVmId before starting the agent.
#
# CRITICAL: DD_HOSTNAME is passed INLINE to envsubst (not exported),
# because DD_* env vars take precedence over datadog.yaml config-file
# values in the agent's hostname-resolution priority. If we exported
# DD_HOSTNAME="pre-launch" here, supervisord would inherit it, the
# datadog-agent subprocess would inherit it at /launch, and the agent
# would use "pre-launch" as its hostname regardless of what
# hook_server.py wrote to datadog.yaml.
#
# DD_API_KEY, DD_SITE, DD_SERVICE, AWS_LAMBDA_FUNCTION_ARN are already
# set in the container env via Dockerfile ENV lines and .dd-env sourced
# above, so they don't need local exports here — envsubst reads them
# straight from the inherited env.
DD_HOSTNAME="pre-launch" \
  envsubst '${DD_API_KEY} ${DD_SITE} ${DD_HOSTNAME} ${DD_SERVICE} ${AWS_LAMBDA_FUNCTION_ARN}' \
    < /etc/datadog-agent/datadog.yaml.template \
    > /etc/datadog-agent/datadog.yaml
chown dd-agent:dd-agent /etc/datadog-agent/datadog.yaml
chmod 640 /etc/datadog-agent/datadog.yaml

exec supervisord -c /etc/supervisord.conf
