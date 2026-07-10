#!/usr/bin/env zsh
set +x
# =============================================================================
# Lambda MicroVM run-only script — launches a new MicroVM instance from an
# existing MicroVM Image, without rebuilding the image.
#
# Given either a MicroVM Image ARN or the ID of an existing MicroVM (whose
# image ARN/version/execution role are looked up via get-microvm), this
# script:
#   1. Resolves the image ARN/version (+ execution role, when a MicroVM ID
#      was given instead of an ARN)
#   2. Runs a new MicroVM from that image
#   3. Generates an auth token and writes MICROVM_ID / MICROVM_TOKEN /
#      MICROVM_ENDPOINT exports to a sourceable file (can't export directly
#      — we're a subprocess), plus a ready-to-paste curl command
#
# Usage:    ./run-microvm.sh <image-arn-or-microvm-id>
# Examples:
#   ./run-microvm.sh arn:aws:lambda:us-east-2:425362996713:microvm-image:sample-nodejs-app-0421095217
#   ./run-microvm.sh microvm-74c4447e-c05c-31a5-b310-67af45a34d79
#
# Detection: an argument starting with "arn:" is treated as an image ARN;
# anything else is treated as an existing MicroVM ID, and its imageArn /
# imageVersion / executionRoleArn are looked up via get-microvm.
#
# Config via env:
#   REGION                    default: us-east-2
#   IMAGE_VERSION             default: 1.0 (ARN input) or the source
#                                      MicroVM's version (MicroVM-ID input)
#   EXECUTION_ROLE_ARN        default: arn:aws:iam::425362996713:role/microvm-build-role
#                                      (ARN input) or the source MicroVM's
#                                      executionRoleArn (MicroVM-ID input)
#   APP_PORT                  default: 8080  (exported at the end; X-aws-proxy-port value)
#   SHELL_ENABLED             default: true  (attaches SHELL_INGRESS connector)
#   TOKEN_EXPIRATION_MINUTES  default: 30
#
# Requires `awsv` to resolve to AWS CLI v2 (as used throughout CLAUDE.md) and
# `jq` for parsing the multi-part authToken response. We source ~/.zshrc below
# to pick up awsv — which is why this script is `#!/usr/bin/env zsh`, not bash.
#
# The lambda-microvms service is built into AWS CLI v2 (GA) — no
# `aws configure add-model` step needed.
# =============================================================================

INPUT="${1:?Usage: $0 <image-arn-or-microvm-id>}"

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

# Resolve this script's own directory (independent of the caller's cwd) so
# the token file always lands in a predictable place.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

REGION="${REGION:-us-east-2}"
AWS_ARGS=(--region "$REGION")

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# --- 1. Resolve image ARN/version (+ execution role) ------------------------
if [[ "$INPUT" == arn:* ]]; then
  IMAGE_ARN="$INPUT"
  IMAGE_VERSION="${IMAGE_VERSION:-1.0}"
  EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-arn:aws:iam::425362996713:role/microvm-build-role}"
  log "Image ARN given directly: $IMAGE_ARN"
else
  log "[1/3] Looking up image ARN from MicroVM ID: $INPUT"
  SOURCE_JSON=$(awsv aws lambda-microvms get-microvm \
    --microvm-identifier "$INPUT" \
    "${AWS_ARGS[@]}" \
    --output json)
  IMAGE_ARN=$(print -r -- "$SOURCE_JSON" | jq -er '.imageArn')
  IMAGE_VERSION="${IMAGE_VERSION:-$(print -r -- "$SOURCE_JSON" | jq -er '.imageVersion')}"
  EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-$(print -r -- "$SOURCE_JSON" | jq -r '.executionRoleArn // "arn:aws:iam::425362996713:role/microvm-build-role"')}"
  log "      image ARN:     $IMAGE_ARN"
  log "      image version: $IMAGE_VERSION"
fi

APP_PORT="${APP_PORT:-8080}"
SHELL_ENABLED="${SHELL_ENABLED:-true}"
TOKEN_EXPIRATION_MINUTES="${TOKEN_EXPIRATION_MINUTES:-30}"

log "Exec role:  $EXECUTION_ROLE_ARN"
log "Shell:      SHELL_INGRESS=$SHELL_ENABLED"

# --- 2. Run MicroVM ----------------------------------------------------------
log "[2/3] Running MicroVM from image $IMAGE_ARN (version $IMAGE_VERSION)"
# Network connectors are only needed for shell access. Without any connectors
# the service defaults to HTTP_INGRESS + INTERNET_EGRESS (verified) — all an
# HTTP app needs — and per-port access is governed by the auth token's
# --allowed-ports, not by ingress connectors. For shell access we attach
# SHELL_INGRESS, and must name the app's HTTP_INGRESS alongside it.
connector_args=()
if [[ "$SHELL_ENABLED" == "true" ]]; then
  connector_args=(
    --ingress-network-connectors "[\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS\",\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:SHELL_INGRESS\"]"
    --egress-network-connectors  "[\"arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS\"]"
  )
fi

# run-microvm returns the microvmId AND the per-MicroVM data-plane endpoint in
# one response — no follow-up get-microvm needed. The endpoint is the host to
# curl / open WebSockets against (pattern
# <uuid>.lambda-microvm-gamma.<region>.on.aws), not the control-plane.
RUN_JSON=$(awsv aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --image-version "$IMAGE_VERSION" \
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

# --- 3. Generate auth token and save sourceable env-var file -----------------
log "[3/3] Generating auth token (${TOKEN_EXPIRATION_MINUTES}-min expiry)"
# authToken is a map (TokenParts); the proxy wants the X-aws-proxy-auth part.
# Pull it directly with the CLI's --query rather than post-processing JSON.
MICROVM_TOKEN=$(awsv aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "$MICROVM_ID" \
  --expiration-in-minutes "$TOKEN_EXPIRATION_MINUTES" \
  --allowed-ports '[{"port":'"$APP_PORT"'}]' \
  "${AWS_ARGS[@]}" \
  --query 'authToken."X-aws-proxy-auth"' --output text)

# Sourceable file, keyed by MicroVM id (not image name) so multiple VMs from
# the same image don't clobber each other. `umask 077` before creation +
# `chmod 600` after = owner-only readable — the token is a short-lived
# secret; don't leave it world-readable.
TOKEN_FILE="$SCRIPT_DIR/.microvm-token.$MICROVM_ID"
umask 077
{
  printf '# Generated by run-microvm.sh on %s\n' "$(date)"
  printf '# Image: %s (version %s)   MicroVM: %s\n' "$IMAGE_ARN" "$IMAGE_VERSION" "$MICROVM_ID"
  printf 'export MICROVM_ID=%q\n'       "$MICROVM_ID"
  printf 'export MICROVM_TOKEN=%q\n'    "$MICROVM_TOKEN"
  printf 'export MICROVM_ENDPOINT=%q\n' "$MICROVM_ENDPOINT"
  printf 'export APP_PORT=%q\n'         "$APP_PORT"
} > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

log ""
log "Done."
log "  microvmId:     $MICROVM_ID"
log "  MICROVM_TOKEN: ${MICROVM_TOKEN:0:8}…   (${TOKEN_EXPIRATION_MINUTES}-min expiry; full value in $TOKEN_FILE)"
log "  APP_PORT:  $APP_PORT"
log ""
log "Load env vars into your current shell:"
log "  source $TOKEN_FILE"
log ""
log "Or test the MicroVM with curl:"
log "curl -H \"X-aws-proxy-auth: \$MICROVM_TOKEN\" -H \"X-aws-proxy-port: \$APP_PORT\" https://\$MICROVM_ENDPOINT/health"
