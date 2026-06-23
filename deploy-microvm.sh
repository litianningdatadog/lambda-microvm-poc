#!/usr/bin/env zsh
set +x
# =============================================================================
# Lambda MicroVM one-shot deploy script.
#
# Given an app directory, this script:
#   1. Zips the directory (Dockerfile at the zip root)
#   2. Uploads the zip to S3
#   3. Creates a MicroVM Image (image name = directory basename)
#   4. Polls every POLL_INTERVAL seconds until state == CREATED
#   5. Launches a MicroVM from the new image
#   6. Generates an auth token and writes MICROVM_ID / MICROVM_TOKEN exports
#      to a sourceable file (can't export directly — we're a subprocess)
#
# Usage:    ./deploy-microvm.sh <app-dir>
# Example:  ./deploy-microvm.sh sample-flask-app-serverless-init-poc
#
# S3 layout:
#   Bucket = <app-dir basename>     (created on demand; name must be
#                                    globally unique and S3-compliant)
#   Key    = YYYYMMDD_HHMMSS.zip    (timestamped per run, so prior builds
#                                    stay in the bucket for rollback)
#
# Config via env (defaults match CLAUDE.md's example account/region):
#   S3_BUCKET        default: <app-name>  (override to pin to a shared bucket)
#   BUILD_ROLE_ARN   default: arn:aws:iam::425362996713:role/microvm-build-role
#   BASE_IMAGE_ARN   default: the AL2023 base image
#   REGION           default: us-east-2
#   ENDPOINT         default: gamma cell01 control-plane URL
#   POLL_INTERVAL    default: 10 (seconds)
#   POLL_TIMEOUT     default: 1800 (seconds, 30 min)
#   APP_PORT         default: 8080  (exported at the end; X-aws-proxy-port value)
#   SHELL_ENABLED    default: true  (attaches SHELL_INGRESS connector at run time)
#   EXECUTION_ROLE_ARN   default: the microvm-build-role (confirmed working in
#                                  prod despite the schema saying the role must
#                                  trust lambda.amazonaws.com)
#
# Requires `awsv` to resolve to AWS CLI v2 (as used throughout CLAUDE.md) and
# `jq` for parsing the multi-part authToken response. We source ~/.zshrc below
# to pick up awsv — which is why this script is `#!/usr/bin/env zsh`, not bash.
#
# The GA lambda-microvms service model ships in this repo as
# `lambdamicrovms-ga.api.json` (sourced from botocore). This script registers
# it with the AWS CLI on every invocation (idempotent), so a fresh clone works
# without first running the `aws configure add-model` step.
# =============================================================================

APP_DIR="${1:?Usage: $0 <app-dir>}"

# Resolve to absolute BEFORE sourcing ~/.zshrc — the rc file may `cd`
# elsewhere (nvm auto-switch, direnv hooks, etc.), which would break
# later relative-path lookups.
APP_DIR="$(cd "$APP_DIR" && pwd)"

# Pick up the `awsv` function/alias from the user's zsh config.
# Caveat: if ~/.zshrc short-circuits for non-interactive shells
# (`[[ $- != *i* ]] && return`), awsv won't be defined here. In that case,
# move the awsv definition into ~/.zshenv, which loads unconditionally.
[[ -f "$HOME/.zshrc" ]] && source "$HOME/.zshrc"

if ! whence -w awsv >/dev/null 2>&1; then
  print -u2 "ERROR: 'awsv' is not defined after sourcing ~/.zshrc."
  print -u2 "       Define it in ~/.zshenv (or invoke this script from an"
  print -u2 "       interactive shell where awsv is already loaded)."
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  print -u2 "ERROR: 'jq' is required (parses the multi-part authToken)."
  print -u2 "       Install with: brew install jq"
  exit 1
fi

set -euo pipefail

# Resolve this script's own directory (independent of the caller's cwd)
# so we can find the lambda-microvms service model next to it in the repo.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MODEL_FILE="$SCRIPT_DIR/lambdamicrovms-ga.api.json"

if [[ ! -f "$MODEL_FILE" ]]; then
  print -u2 "ERROR: Service model not found at $MODEL_FILE"
  print -u2 "       This script expects lambdamicrovms-ga.api.json to live"
  print -u2 "       next to it in the repo root."
  exit 1
fi

# Register the lambda-microvms service model on every invocation.
# `aws configure add-model` is idempotent — it overwrites
# ~/.aws/models/lambda-microvms/<api-version>/service-2.json with the
# repo's copy. Doing this here means the script works on a fresh clone
# without requiring the operator to follow CLAUDE.md's "CLI Setup"
# step first.
awsv aws configure add-model \
  --service-model "file://$MODEL_FILE" \
  --service-name lambda-microvms >/dev/null

APP_NAME="$(basename "$APP_DIR")"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

S3_BUCKET="${S3_BUCKET:-$APP_NAME}"
# Image names must be unique within the account AND ≲ 30 chars empirically
# (the documented max is 64, but the service rejects longer names with a
# generic InvalidRequestException). Re-using a name collides with the
# previous deploy — CLAUDE.md notes "Updating a MicroVM Image is not
# supported; create a new one instead."
#
# Default layout: <app-prefix>-MMDDHHMMSS   (30 chars, unique per second)
#   - 19 chars leading of APP_NAME
#   - '-' separator
#   - 10-char MMDDHHMMSS timestamp (second-precision, unique within a year)
IMAGE_NAME="${IMAGE_NAME:-${APP_NAME:0:19}-$(date +%m%d%H%M%S)}"
REGION="${REGION:-us-east-2}"
BUILD_ROLE_ARN="${BUILD_ROLE_ARN:-arn:aws:iam::425362996713:role/microvm-build-role}"
BASE_IMAGE_ARN="${BASE_IMAGE_ARN:-arn:aws:lambda:${REGION}:aws:microvm-image:al2023-1}"
ENDPOINT="${ENDPOINT:-https://cell01.${REGION}.gamma.fe.kepler-analytics.aws.dev}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"
# App port inside the MicroVM to route to via the proxy (X-aws-proxy-port
# header / lambda-microvms.port.<PORT> subprotocol). Default 8080 matches
# the user-app port (`EXPOSE 8080` in the sample Dockerfiles). Set to 9000
# to hit the lifecycle-hook / serverless-init server instead.
APP_PORT="${APP_PORT:-8080}"
# Attaches the SHELL_INGRESS network connector at run time, enabling
# interactive shell access via `create-microvm-shell-auth-token` (GA replaced
# the old operationalConfig.shellEnabled flag). Default on for this dev kit.
SHELL_ENABLED="${SHELL_ENABLED:-true}"
# Role assumed by the MicroVM at runtime (RunMicrovmRequest.executionRoleArn).
# The 2026-03-07 schema says this role "must trust lambda.amazonaws.com", but
# reusing microvm-build-role has been verified working in prod, so we default
# to the same ARN as BUILD_ROLE_ARN. Override per-launch via env.
EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-$BUILD_ROLE_ARN}"

# Local zip sits next to the app dir; keep $APP_NAME in the filename so
# deploys of different apps don't collide in a shared parent directory.
ZIP_PATH="$(dirname "$APP_DIR")/$APP_NAME.$TIMESTAMP.zip"

# S3 key is just the timestamp — the bucket name already carries $APP_NAME.
S3_KEY="$TIMESTAMP.zip"
S3_URI="s3://$S3_BUCKET/$S3_KEY"

# Shared CLI args so the commands never drift.
# --endpoint points to gamma (pre-production)
# AWS_ARGS=(--region "$REGION" --endpoint "$ENDPOINT")
# without --endpoint points to production
AWS_ARGS=(--region "$REGION")

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

log "App name:   $APP_NAME"
log "Image name: $IMAGE_NAME   (${#IMAGE_NAME} chars)"
log "App dir:    $APP_DIR"
log "Zip path:   $ZIP_PATH"
log "S3 target:  $S3_URI"
log "Shell:      SHELL_INGRESS=$SHELL_ENABLED"
log "Exec role:  $EXECUTION_ROLE_ARN"
log "Hooks:      all ENABLED; platform-default timeouts"

# --- 1. Zip ----------------------------------------------------------------
log "[1/5] Creating zip"
rm -f "$ZIP_PATH"

( cd "$APP_DIR" && zip -qr "$ZIP_PATH" . -x '*.DS_Store' 'claude-notifications.jsonl' )
log "      zip size: $(du -h "$ZIP_PATH" | awk '{print $1}')"

# --- 2. Upload to S3 -------------------------------------------------------
log "[2/5] Ensuring bucket s3://$S3_BUCKET exists, then uploading"
if awsv aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" 2>/dev/null; then
  log "      bucket exists"
else
  log "      bucket missing — creating"
  awsv aws s3 mb "s3://$S3_BUCKET" --region "$REGION"
fi
awsv aws s3 cp "$ZIP_PATH" "$S3_URI" --region "$REGION"

# --- 3. Create MicroVM Image ----------------------------------------------
log "[3/5] Creating MicroVM image '$IMAGE_NAME'"

# Hooks JSON (GA --hooks parameter).
# microvmImageHooks: ready + validate are build-time; timeouts in seconds.
# microvmHooks: run/resume/suspend/terminate are runtime; keep them fast (≤60s).
HOOKS_JSON=$(cat <<'EOF'
{
  "port": 9000,
  "microvmImageHooks": {
    "ready":                   "ENABLED",
    "readyTimeoutInSeconds":   60,
    "validate":                "ENABLED",
    "validateTimeoutInSeconds": 60
  },
  "microvmHooks": {
    "run":                     "ENABLED",
    "runTimeoutInSeconds":     2,
    "resume":                  "ENABLED",
    "resumeTimeoutInSeconds":  2,
    "suspend":                 "ENABLED",
    "suspendTimeoutInSeconds": 5,
    "terminate":               "ENABLED",
    "terminateTimeoutInSeconds": 5
  }
}
EOF
)

# Environment variables baked into the snapshot (separate --environment-variables
# parameter in GA). DD_AWS_MICROVM_USER_APP_PORT is already in each sample-app
# Dockerfile, so it doesn't need to be repeated here.
ENV_VARS_JSON=$(cat <<'EOF'
{
  "DD_SITE":              "datadoghq.com",
  "DD_VERSION":           "1",
  "DD_ENV":               "devmicrovm",
  "DD_LOGS_ENABLED":      "true",
  "DD_LOG_LEVEL":         "debug",
  "DD_TRACE_SAMPLE_RATE": "1.0",
  "DD_LOGS_INJECTION":    "true",
  "DD_TRACE_ENABLED":     "true",
  "DD_TRACE_AGENT_URL":   "http://localhost:8126",
  "DD_TRACE_STARTUP_LOGS": "true",
  "DD_TRACE_DEBUG":       "true"
}
EOF
)
if [[ -n "${DD_API_KEY:-}" ]]; then
  ENV_VARS_JSON=$(printf '%s' "$ENV_VARS_JSON" | jq --arg v "$DD_API_KEY" '.DD_API_KEY = $v')
fi

print $HOOKS_JSON
print $ENV_VARS_JSON

IMAGE_ARN=$(awsv aws lambda-microvms create-microvm-image \
  --code-artifact "uri=$S3_URI" \
  --name "$IMAGE_NAME" \
  --base-image-arn "$BASE_IMAGE_ARN" \
  --build-role-arn "$BUILD_ROLE_ARN" \
  --hooks "$HOOKS_JSON" \
  --environment-variables "$ENV_VARS_JSON" \
  "${AWS_ARGS[@]}" \
  --query 'imageArn' --output text)
log "      image ARN: $IMAGE_ARN"

# --- 4. Poll until CREATED -------------------------------------------------
log "[4/5] Polling image state (every ${POLL_INTERVAL}s, timeout ${POLL_TIMEOUT}s)"
start=$(date +%s)
while :; do
  state=$(awsv aws lambda-microvms get-microvm-image \
    --image-identifier "$IMAGE_ARN" \
    "${AWS_ARGS[@]}" \
    --query 'state' --output text 2>/dev/null) || {
    elapsed=$(( $(date +%s) - start ))
    log "      WARN: get-microvm-image returned an error (transient?); retrying in ${POLL_INTERVAL}s  elapsed=${elapsed}s"
    sleep "$POLL_INTERVAL"
    continue
  }
  elapsed=$(( $(date +%s) - start ))
  log "      state=$state  elapsed=${elapsed}s"
  case "$state" in
    CREATED) break ;;
    FAILED)
      # Version-level build failure — stateReason carries the error code.
      reason=$(awsv aws lambda-microvms get-microvm-image \
        --image-identifier "$IMAGE_ARN" "${AWS_ARGS[@]}" \
        --query 'stateReason' --output text 2>/dev/null || true)
      log "ERROR: image build failed: ${reason:-<no reason returned>}"
      log "      build logs: CloudWatch /aws/lambda-microvms/$IMAGE_NAME"
      exit 1
      ;;
    CREATING) ;;  # still building — keep polling
    *) log "      (unexpected state '$state' — continuing to poll)" ;;
  esac
  if (( elapsed >= POLL_TIMEOUT )); then
    log "ERROR: timed out after ${POLL_TIMEOUT}s (last state=$state)"
    exit 1
  fi
  sleep "$POLL_INTERVAL"
done
log "      image ready."

# --- 5. Run MicroVM --------------------------------------------------------
log "[5/6] Running MicroVM"
# Network connectors are only needed for shell access. Without any connectors
# the service defaults to HTTP_INGRESS + INTERNET_EGRESS (verified) — all an
# HTTP app needs — and per-port access is governed by the auth token's
# --allowed-ports, not by ingress connectors. For shell access we attach
# SHELL_INGRESS, and must name the app's HTTP_INGRESS alongside it.
connector_args=()
if [[ "${SHELL_ENABLED:-true}" == "true" ]]; then
  connector_args=(
    --ingress-network-connectors "[\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS\",\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:SHELL_INGRESS\"]"
    --egress-network-connectors  "[\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS\"]"
  )
fi

# run-microvm returns the microvmId AND the per-MicroVM data-plane endpoint in
# one response — no follow-up get-microvm needed. The endpoint is the host to
# curl / open WebSockets against (pattern
# <uuid>.lambda-microvm-gamma.<region>.on.aws), not the control-plane.
# Run errors (ValidationException / AccessDenied / ResourceNotFound) are
# deterministic, so on failure the CLI prints the real error to stderr and we
# exit rather than retrying.
RUN_JSON=$(awsv aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --image-version 1.0 \
  --execution-role-arn "$EXECUTION_ROLE_ARN" \
  "${connector_args[@]}" \
  --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,"suspendedDurationSeconds":300}' \
  "${AWS_ARGS[@]}" \
  --output json) || {
  log "ERROR: run-microvm failed (see error above)"
  exit 1
}
MICROVM_ID=$(print -r -- "$RUN_JSON"       | jq -r '.microvmId')
MICROVM_ENDPOINT=$(print -r -- "$RUN_JSON" | jq -r '.endpoint')
log "      microvmId: $MICROVM_ID"
log "      endpoint:  $MICROVM_ENDPOINT"

# --- 6. Generate auth token and save sourceable env-var file ---------------
log "[6/6] Generating auth token (30-min expiry)"
# authToken is a map (TokenParts); the proxy wants the X-aws-proxy-auth part.
# Pull it directly with the CLI's --query rather than post-processing JSON.
MICROVM_TOKEN=$(awsv aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "$MICROVM_ID" \
  --expiration-in-minutes 30 \
  --allowed-ports '[{"port":'"$APP_PORT"'}]' \
  "${AWS_ARGS[@]}" \
  --query 'authToken."X-aws-proxy-auth"' --output text)

# Sourceable file. `umask 077` before creation + `chmod 600` after = owner-only
# readable. The token is a short-lived secret; don't leave it world-readable.
TOKEN_FILE="$(dirname "$APP_DIR")/.microvm-token.$IMAGE_NAME"
umask 077
{
  printf '# Generated by deploy-microvm.sh on %s\n' "$(date)"
  printf '# Image: %s   MicroVM: %s\n' "$IMAGE_NAME" "$MICROVM_ID"
  printf 'export MICROVM_ID=%q\n'       "$MICROVM_ID"
  printf 'export MICROVM_TOKEN=%q\n'    "$MICROVM_TOKEN"
  printf 'export MICROVM_ENDPOINT=%q\n' "$MICROVM_ENDPOINT"
  printf 'export APP_PORT=%q\n'         "$APP_PORT"
} > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

log ""
log "Done."
log "  microvmId:     $MICROVM_ID"
log "  MICROVM_TOKEN: ${MICROVM_TOKEN:0:8}…   (30-min expiry; full value in $TOKEN_FILE)"
log "  APP_PORT:  $APP_PORT"
log ""
log "Load env vars into your current shell:"
log "  source $TOKEN_FILE"
log ""
log "Or test the MicroVM with curl:"
log "curl -H \"X-aws-proxy-auth: \$MICROVM_TOKEN\" -H \"X-aws-proxy-port: \$APP_PORT\" https://\$MICROVM_ENDPOINT/health"
