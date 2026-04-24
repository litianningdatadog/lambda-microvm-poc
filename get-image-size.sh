#!/usr/bin/env zsh
# =============================================================================
# Lambda MicroVM — query snapshot size for an image ARN.
#
# Given a MicroVM Image ARN, looks up its most recent build and returns the
# Firecracker snapshot size in bytes (plus a human-readable form if `numfmt`
# is available on PATH).
#
# This is a two-step API dance because create-micro-vm-image returns only
# the ARN, not the microVMImageVersion or buildId that
# describe-micro-vm-image-build requires. Step 1 lists builds and picks the
# newest; step 2 drills in.
#
# Usage:    ./get-image-size.sh <image-arn> [<version-filter>]
# Example:
#   ./get-image-size.sh \
#     arn:aws:lambda:us-east-2:425362996713:microvm-image:sample-flask-app-da-0423194114
#
# Optional <version-filter>: restrict to a specific microVMImageVersion.
# Defaults to "all versions, newest build wins."
#
# Config via env (defaults match deploy-microvm.sh):
#   REGION     default: us-east-2
#   ENDPOINT   default: gamma cell01 control-plane URL
#
# Requires `awsv` (AWS CLI v2 wrapper, as used in deploy-microvm.sh) and
# `jq`. `numfmt` is optional; if missing, size is reported in bytes only.
#
# The lambda-microvms service model ships in this repo as
# `lambdamicrovms-2025-09-09.json`. This script registers it with the
# AWS CLI on every invocation (idempotent), so you don't need to run
# `aws configure add-model` yourself before the first use.
# =============================================================================

IMAGE_ARN="${1:?Usage: $0 <image-arn> [<version-filter>]}"
VERSION_FILTER="${2:-}"

# Pull `awsv` out of the user's shell config (same pattern as deploy-microvm.sh).
[[ -f "$HOME/.zshrc" ]] && source "$HOME/.zshrc"

if ! whence -w awsv >/dev/null 2>&1; then
  print -u2 "ERROR: 'awsv' is not defined. Define it in ~/.zshenv, or invoke"
  print -u2 "       this script from an interactive shell where awsv is loaded."
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  print -u2 "ERROR: 'jq' is required (parses the builds list)."
  print -u2 "       Install with: brew install jq"
  exit 1
fi

set -euo pipefail

# Resolve the script's own directory so we can find the service model
# next to it, regardless of where the caller ran this script from.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MODEL_FILE="$SCRIPT_DIR/lambdamicrovms-2025-09-09.json"

if [[ ! -f "$MODEL_FILE" ]]; then
  print -u2 "ERROR: Service model not found at $MODEL_FILE"
  print -u2 "       This script expects lambdamicrovms-2025-09-09.json to live"
  print -u2 "       next to it in the repo root."
  exit 1
fi

# Register the lambda-microvms service model on every invocation.
# `aws configure add-model` is idempotent — it writes the model file
# into ~/.aws/models/lambda-microvms/<api-version>/service-2.json,
# overwriting any prior registration with the same api-version. Doing
# this here means the script works on a fresh clone without requiring
# the operator to follow CLAUDE.md's "CLI Setup" step first.
awsv aws configure add-model \
  --service-model "file://$MODEL_FILE" \
  --service-name lambda-microvms >/dev/null

REGION="${REGION:-us-east-2}"
ENDPOINT="${ENDPOINT:-https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev}"
AWS_ARGS=(--region "$REGION" --endpoint "$ENDPOINT")

# --- Step 1: list builds, pick the newest -----------------------------------
LIST_ARGS=(--micro-vm-image-arn "$IMAGE_ARN")
if [[ -n "$VERSION_FILTER" ]]; then
  LIST_ARGS+=(--micro-vm-image-version "$VERSION_FILTER")
fi

BUILDS_JSON=$(awsv aws lambda-microvms list-micro-vm-image-builds \
  "${LIST_ARGS[@]}" "${AWS_ARGS[@]}" --output json)

# Sort builds by creationTime descending so the newest wins — the service's
# default ordering isn't documented, so don't rely on it.
LATEST=$(print -r -- "$BUILDS_JSON" \
  | jq -c '.builds | sort_by(.creationTime) | reverse | .[0] // null')

if [[ "$LATEST" == "null" || -z "$LATEST" ]]; then
  print -u2 "ERROR: No builds found for $IMAGE_ARN"
  [[ -n "$VERSION_FILTER" ]] && \
    print -u2 "       (filter microVMImageVersion=$VERSION_FILTER)"
  exit 1
fi

VERSION=$(print -r -- "$LATEST" | jq -r '.microVMImageVersion')
BUILD_ID=$(print -r -- "$LATEST" | jq -r '.buildId')
BUILD_STATE=$(print -r -- "$LATEST" | jq -r '.state')   # list payload uses .state
CREATION_TIME=$(print -r -- "$LATEST" | jq -r '.creationTime')

# --- Step 2: describe that build, pull snapshotSizeBytes --------------------
# snapshotSizeBytes is top-level in DescribeMicroVMImageBuildOutput,
# NOT nested under `summary`. (`.summary.*` is the path for
# describe-micro-vm-image without the -build suffix — different operation.)
DESCRIBE_JSON=$(awsv aws lambda-microvms describe-micro-vm-image-build \
  --micro-vm-image-arn "$IMAGE_ARN" \
  --micro-vm-image-version "$VERSION" \
  --build-id "$BUILD_ID" \
  "${AWS_ARGS[@]}" --output json)

SNAPSHOT_BYTES=$(print -r -- "$DESCRIBE_JSON" \
  | jq -r '.snapshotSizeBytes // "null"')

if [[ "$SNAPSHOT_BYTES" == "null" ]]; then
  # BuildState enum is: PENDING → IN_PROGRESS → STAGED → SUCCESSFUL (or FAILED).
  # snapshotSizeBytes is declared optional in the schema, so the service may
  # simply not populate it even for SUCCESSFUL builds in this preview.
  print -u2 "WARNING: snapshotSizeBytes is NOT populated for build $BUILD_ID"
  print -u2 "         (buildState=$BUILD_STATE — the field is optional in the"
  print -u2 "          schema; the MicroVM preview service may not emit it yet)"
  print -u2 ""
  print -u2 "Full DescribeMicroVMImageBuild response (so you can see what IS"
  print -u2 "returned):"
  print -u2 "$DESCRIBE_JSON" | jq . >&2 2>/dev/null || print -u2 "$DESCRIBE_JSON"
  exit 2
fi

# --- Format + print ---------------------------------------------------------
HUMAN=""
if command -v numfmt >/dev/null 2>&1; then
  HUMAN=" ($(numfmt --to=iec-i --suffix=B "$SNAPSHOT_BYTES"))"
fi

print "Image ARN:     $IMAGE_ARN"
print "Version:       $VERSION"
print "Build ID:      $BUILD_ID"
print "Build state:   $BUILD_STATE"
print "Creation:      $CREATION_TIME"
print "Snapshot size: $SNAPSHOT_BYTES bytes$HUMAN"
