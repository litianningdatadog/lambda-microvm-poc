#!/usr/bin/env zsh
set +x
# =============================================================================
# Launch a SECOND clone from the image the most recent deploy created, capture
# /ids from BOTH clones, and compare. Run this AFTER:
#
#   ./deploy-microvm.sh rng-test          (creates the image + clone A)
#   rng-test/clone-b.sh                   (this script: clone B + compare)
#
# Two run-microvm calls against ONE image version = two Firecracker clones of
# one snapshot -- the valid cross-clone scenario. (Running deploy twice would
# create two DIFFERENT images = two snapshots, which is NOT a valid test.)
#
# Usage:
#   ./clone-b.sh                 # auto-find newest .microvm-token.rng-test-*
#   ./clone-b.sh <tokenfile>     # use a specific clone-A token file
#
# Env: REGION (default us-east-2), APP_PORT (8080), EXECUTION_ROLE_ARN,
#      IMAGE_ARN (override reconstruction from the token-file name).
# =============================================================================

[[ -f "$HOME/.zshrc" ]] && source "$HOME/.zshrc"
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"   # rng-test/
PARENT_DIR="$(dirname -- "$SCRIPT_DIR")"            # lambda-microvm-doc/
REGION="${REGION:-us-east-2}"
APP_PORT="${APP_PORT:-8080}"
EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-arn:aws:iam::425362996713:role/microvm-build-role}"
AWS_ARGS=(--region "$REGION")

command -v jq >/dev/null 2>&1 || { print -u2 "ERROR: jq required"; exit 1; }
whence -w awsv >/dev/null 2>&1 || { print -u2 "ERROR: awsv not defined (source ~/.zshrc or define in ~/.zshenv)"; exit 1; }

# Register the lambda-microvms service model (idempotent), same as the deploy.
MODEL_FILE="$PARENT_DIR/lambdamicrovms-ga.api.json"
[[ -f "$MODEL_FILE" ]] && awsv aws configure add-model \
  --service-model "file://$MODEL_FILE" --service-name lambda-microvms >/dev/null

# --- Locate clone-A token file (written by deploy in the parent dir) ---------
if [[ -n "${1:-}" ]]; then
  TOKEN_FILE="$1"
else
  cand=("$PARENT_DIR"/.microvm-token.rng-test-*(N.om))   # N=nullglob .=files om=mtime-desc
  TOKEN_FILE="${cand[1]:-}"
fi
[[ -n "$TOKEN_FILE" && -f "$TOKEN_FILE" ]] || {
  print -u2 "ERROR: no clone-A token file found. Run ./deploy-microvm.sh rng-test first,"
  print -u2 "       or pass the token file path as \$1."
  exit 1
}
print -u2 "clone A token file: $TOKEN_FILE"
source "$TOKEN_FILE"   # MICROVM_ID, MICROVM_TOKEN, MICROVM_ENDPOINT, APP_PORT

# --- Image ARN: env override, else reconstruct from the token-file name ------
if [[ -z "${IMAGE_ARN:-}" ]]; then
  IMAGE_NAME="${TOKEN_FILE##*/.microvm-token.}"
  ACCT="$(awsv aws sts get-caller-identity --query Account --output text)"
  IMAGE_ARN="arn:aws:lambda:$REGION:$ACCT:microvm-image:$IMAGE_NAME"
fi
print -u2 "image: $IMAGE_ARN"

# --- Helpers -----------------------------------------------------------------
mint_token () {  # $1=microvmId -> prints X-aws-proxy-auth
  awsv aws lambda-microvms create-microvm-auth-token \
    --microvm-identifier "$1" --expiration-in-minutes 30 \
    --allowed-ports '[{"port":'"$APP_PORT"'}]' \
    "${AWS_ARGS[@]}" --query 'authToken."X-aws-proxy-auth"' --output text
}
capture () {  # $1=endpoint $2=token $3=outfile
  local ep="$1" tok="$2" out="$3" i
  for i in {1..40}; do
    if curl -fsS "https://$ep/ids" \
         -H "X-aws-proxy-auth: $tok" -H "X-aws-proxy-port: $APP_PORT" -o "$out" 2>/dev/null; then
      print -u2 "captured $out"
      return 0
    fi
    sleep 3
  done
  print -u2 "ERROR: failed to reach $ep"
  return 1
}

# --- Clone A: re-mint a fresh token (deploy's may have expired) and capture --
print -u2 "capturing clone A ($MICROVM_ID)"
TOK_A="$(mint_token "$MICROVM_ID")"
capture "$MICROVM_ENDPOINT" "$TOK_A" "$SCRIPT_DIR/A.json"

# --- Clone B: a second run from the SAME image, then capture -----------------
print -u2 "launching clone B from $IMAGE_ARN"
B_JSON="$(awsv aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" --image-version 1.0 \
  --execution-role-arn "$EXECUTION_ROLE_ARN" \
  --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,"suspendedDurationSeconds":300}' \
  "${AWS_ARGS[@]}" --output json)"
VM_B="$(print -r -- "$B_JSON" | jq -r '.microvmId')"
EP_B="$(print -r -- "$B_JSON" | jq -r '.endpoint')"
print -u2 "clone B: $VM_B @ $EP_B"
TOK_B="$(mint_token "$VM_B")"
capture "$EP_B" "$TOK_B" "$SCRIPT_DIR/B.json"

# --- Verdict -----------------------------------------------------------------
print -u2 ""
"$SCRIPT_DIR/compare.sh" "$SCRIPT_DIR/A.json" "$SCRIPT_DIR/B.json"

print -u2 ""
print -u2 "cleanup clone B when done:"
print -u2 "  awsv aws lambda-microvms terminate-microvm --region $REGION --microvm-identifier $VM_B"
