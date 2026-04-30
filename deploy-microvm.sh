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
#   SHELL_ENABLED    default: true  (operationalConfig.shellEnabled on launch)
#   EXECUTION_ROLE_ARN   default: the microvm-build-role (confirmed working in
#                                  prod despite the schema saying the role must
#                                  trust lambda.amazonaws.com)
#
# Requires `awsv` to resolve to AWS CLI v2 (as used throughout CLAUDE.md) and
# `jq` for parsing the multi-part authToken response. We source ~/.zshrc below
# to pick up awsv — which is why this script is `#!/usr/bin/env zsh`, not bash.
#
# The lambda-microvms service model ships in this repo as
# `lambdamicrovms-2025-09-09.json`. This script registers it with the
# AWS CLI on every invocation (idempotent), so a fresh clone works
# without first running the `aws configure add-model` step from
# CLAUDE.md's "CLI Setup" section.
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
MODEL_FILE="$SCRIPT_DIR/lambdamicrovms-2025-09-09.json"

if [[ ! -f "$MODEL_FILE" ]]; then
  print -u2 "ERROR: Service model not found at $MODEL_FILE"
  print -u2 "       This script expects lambdamicrovms-2025-09-09.json to live"
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
BUILD_ROLE_ARN="${BUILD_ROLE_ARN:-arn:aws:iam::425362996713:role/microvm-build-role}"
BASE_IMAGE_ARN="${BASE_IMAGE_ARN:-arn:aws:lambda:::microvm-image:lambda-microvms-al2023-1}"
REGION="${REGION:-us-east-2}"
ENDPOINT="${ENDPOINT:-https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev}"
INGRESS_CONNECTOR="${INGRESS_CONNECTOR:-arn:aws:lambda:::network-connector:aws-network-connector:ALL_INGRESS}"
EGRESS_CONNECTOR="${EGRESS_CONNECTOR:-arn:aws:lambda:::network-connector:aws-network-connector:INTERNET_EGRESS}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"
# App port inside the MicroVM to route to via the proxy (X-aws-proxy-port
# header / lambda-microvms.port.<PORT> subprotocol). Default 8080 matches
# the user-app port (`EXPOSE 8080` in the sample Dockerfiles). Set to 9000
# to hit the lifecycle-hook / serverless-init server instead.
APP_PORT="${APP_PORT:-8080}"
# Enables `ctr task exec` shell access on the MicroVM host for debugging
# (see CLAUDE.md "Operating MicroVMs"). This is a launch-time setting —
# the image snapshot is unaffected, so flipping it only takes effect on
# the *next* launch. Default on for this preview dev kit; set to false
# to deploy without shell.
SHELL_ENABLED="${SHELL_ENABLED:-true}"
# Role assumed by the MicroVM at runtime (LaunchMicroVMRequest.executionRoleArn).
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
AWS_ARGS=(--region "$REGION" --endpoint "$ENDPOINT")

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

log "App name:   $APP_NAME"
log "Image name: $IMAGE_NAME   (${#IMAGE_NAME} chars)"
log "App dir:    $APP_DIR"
log "Zip path:   $ZIP_PATH"
log "S3 target:  $S3_URI"
log "Shell:      shellEnabled=$SHELL_ENABLED"
log "Exec role:  $EXECUTION_ROLE_ARN"

# --- 1. Zip ----------------------------------------------------------------
log "[1/5] Creating zip"
rm -f "$ZIP_PATH"

# If DD_API_KEY is set in the shell, ship it into the zip as .dd-env
# (entrypoint.sh sources this on container start). The MicroVM build
# pipeline has no access to your host env, so this is how the key
# actually reaches the guest. The file is cleaned up via EXIT trap so
# it doesn't leak as plaintext into the repo after the script finishes.
DD_ENV_FILE="$APP_DIR/.dd-env"
if [[ -n "${DD_API_KEY:-}" ]]; then
  ( umask 077
    printf 'export DD_API_KEY=%q\n' "$DD_API_KEY" > "$DD_ENV_FILE"
  )
  trap "rm -f '$DD_ENV_FILE'" EXIT
  log "      shipping .dd-env with DD_API_KEY (${#DD_API_KEY} chars)"
else
  log "      WARN: DD_API_KEY not set in shell — agent will fail auth"
fi

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
IMAGE_ARN=$(awsv aws lambda-microvms create-micro-vm-image \
  --code-artifact "uri=$S3_URI" \
  --name "$IMAGE_NAME" \
  --base-micro-vm-image-arn "$BASE_IMAGE_ARN" \
  --build-role-arn "$BUILD_ROLE_ARN" \
  "${AWS_ARGS[@]}" \
  --query 'microVMImageArn' --output text)
log "      image ARN: $IMAGE_ARN"

# --- 4. Poll until CREATED -------------------------------------------------
log "[4/5] Polling image state (every ${POLL_INTERVAL}s, timeout ${POLL_TIMEOUT}s)"
start=$(date +%s)
while :; do
  state=$(awsv aws lambda-microvms describe-micro-vm-image \
    --micro-vm-image-arn "$IMAGE_ARN" \
    "${AWS_ARGS[@]}" \
    --query 'summary.state' --output text)
  elapsed=$(( $(date +%s) - start ))
  log "      state=$state  elapsed=${elapsed}s"
  case "$state" in
    CREATED) break ;;
    CREATION_FAILED)
      reason=$(awsv aws lambda-microvms describe-micro-vm-image \
        --micro-vm-image-arn "$IMAGE_ARN" "${AWS_ARGS[@]}" \
        --query 'summary.failureReason' --output text 2>/dev/null || true)
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

# --- 5. Launch MicroVM -----------------------------------------------------
log "[5/6] Launching MicroVM"
MICROVM_ID=$(awsv aws lambda-microvms launch-micro-vm \
  --micro-vm-image-arn "$IMAGE_ARN" \
  --micro-vm-image-version 1.0 \
  --execution-role-arn "$EXECUTION_ROLE_ARN" \
  --ingress-network-connectors "$INGRESS_CONNECTOR" \
  --egress-network-connectors "$EGRESS_CONNECTOR" \
  --idle-policy autoResumeEnabled=true,maxIdleDurationSeconds=900,suspendedDurationSeconds=300 \
  --operational-config "shellEnabled=$SHELL_ENABLED" \
  "${AWS_ARGS[@]}" \
  --query 'microVMId' --output text)
log "      microVMId: $MICROVM_ID"

# --- 6. Generate auth token and save sourceable env-var file ---------------
log "[6/6] Generating auth token (30-min expiry)"
# authToken is a map<String,String> (TokenParts in the schema). One API call;
# we extract both the full map (for file reference) and a scalar (for the
# MICROVM_TOKEN export). If your token happens to have multiple parts, the
# full map is dumped as a comment in the token file so you can override.
AUTH_JSON=$(awsv aws lambda-microvms generate-micro-vm-auth-token \
  --micro-vm-id "$MICROVM_ID" \
  --expiration-minutes 30 \
  "${AWS_ARGS[@]}" \
  --output json)
AUTH_TOKEN_MAP=$(print -r -- "$AUTH_JSON" | jq -c '.authToken')
MICROVM_TOKEN=$(print -r -- "$AUTH_JSON"   | jq -r '.authToken | to_entries | .[0].value')

# Sourceable file. `umask 077` before creation + `chmod 600` after = owner-only
# readable. The token is a short-lived secret; don't leave it world-readable.
TOKEN_FILE="$(dirname "$APP_DIR")/.microvm-token.$IMAGE_NAME"
umask 077
{
  printf '# Generated by deploy-microvm.sh on %s\n' "$(date)"
  printf '# Image: %s   MicroVM: %s\n' "$IMAGE_NAME" "$MICROVM_ID"
  printf '# Full authToken payload (for reference): %s\n' "$AUTH_TOKEN_MAP"
  printf 'export MICROVM_ID=%q\n'    "$MICROVM_ID"
  printf 'export MICROVM_TOKEN=%q\n' "$MICROVM_TOKEN"
  printf 'export APP_PORT=%q\n'     "$APP_PORT"
} > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

log ""
log "Done."
log "  microVMId:     $MICROVM_ID"
log "  MICROVM_TOKEN: ${MICROVM_TOKEN:0:8}…   (30-min expiry; full value in $TOKEN_FILE)"
log "  APP_PORT:  $APP_PORT"
log ""
log "Load env vars into your current shell:"
log "  source $TOKEN_FILE"
log ""
log "Or test the MicroVM with curl:"
log "curl -H \"X-aws-proxy-auth: \$MICROVM_TOKEN\" -H \"X-aws-proxy-port: \$APP_PORT\" https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev/"
