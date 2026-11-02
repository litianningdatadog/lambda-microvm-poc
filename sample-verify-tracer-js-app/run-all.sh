#!/usr/bin/env bash
# Real AWS snapshot clones -> real suspend/resume -> cleanup -> required backend checks.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (( $# )); then
    case "$1" in
        -h|--help)
            cat <<'HELP'
Usage: ./run-all.sh
Deploys a pinned dd-trace 6.15.0 image and launches up to 6 bounded candidates,
selecting the first pair with the SAME prepared before.runtimeId (selection.json).
Verifies only that pair, suspends/resumes the first selected clone through AWS,
terminates every candidate, captures CloudWatch, and requires Datadog feature APM
hits/errors and matching structured result logs. No match is inconclusive/failure.
No local hook simulation runs. Missing evidence and shared-baseline checks fail.

Required: DD_API_KEY (the same Datadog org queried by dd-auth).
Tools: aws, aws-vault, dd-auth, make, python3, npm, tee.
Overrides:
  SERVERLESS_INIT_BIN  Local ARM64 binary (default /Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64)
  RESULTS_DIR          New evidence directory (default results/<UTC timestamp>-<pid>)
  REGION               us-east-2
  AWS_VAULT_PROFILE    sso-serverless-sandbox-account-admin
  BUILD_ROLE_ARN       arn:aws:iam::425362996713:role/microvm-build-role
  EXECUTION_ROLE_ARN   Defaults to BUILD_ROLE_ARN
  IMAGE_NAME/S3_BUCKET sample-verify-tracer-js-app (bucket defaults to image name)
  BASE_IMAGE_ARN       Regional AWS-managed al2023-1 image
  DD_SITE              datadoghq.com
  DD_AUTH_DOMAIN       dddev.datadoghq.com (must match deployment DD_API_KEY org)
  BUILD_TIMEOUT        1800 seconds
  CHECK_DELAY          180 seconds for backend ingestion

Dry inspection (no credentials/network/deploy):
  python3 cloud.py --help
  python3 check.py --help
Offline existing AWS evidence (not a backend/snapshot simulation pass):
  python3 check.py <results> --guest-only --output <results>/offline-evaluation.json
Backend re-evaluation:
  dd-auth --domain <same-org-domain> -- python3 check.py <results>
HELP
            exit 0 ;;
        *) printf 'Usage: %s [--help]\n' "$0" >&2; exit 2 ;;
    esac
fi
cd "$SCRIPT_DIR"
: "${DD_API_KEY:?DD_API_KEY is required for AWS deployment}"
export REGION="${REGION:-us-east-2}"
export AWS_VAULT_PROFILE="${AWS_VAULT_PROFILE:-sso-serverless-sandbox-account-admin}"
export DD_SITE="${DD_SITE:-datadoghq.com}"
export SERVERLESS_INIT_BIN="${SERVERLESS_INIT_BIN:-/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64}"
CHECK_DELAY="${CHECK_DELAY:-180}"
[[ "$CHECK_DELAY" =~ ^[0-9]+$ ]] || { printf 'CHECK_DELAY must be a nonnegative integer\n' >&2; exit 1; }
for tool in aws aws-vault dd-auth make python3 npm tee; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done
R="${RESULTS_DIR:-results/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
mkdir -p "$(dirname "$R")"
mkdir "$R" || exit 1
R="$(cd "$R" && pwd)"
printf 'Results -> %s\n' "$R"
step() {
    local name="$1" result
    shift
    printf '\n=== %s %s\n' "$(date -u +%FT%TZ)" "$name"
    "$@" 2>&1 | tee "$R/$name.txt"
    result=$?
    printf '%s exit=%s\n' "$name" "$result" >> "$R/steps.txt"
    return "$result"
}
finish() {
    local result=$?
    trap - EXIT
    # cloud.run also has a finally; retry here after failure/interruption.
    if [[ -d "$R/candidates" || -f "$R/vm1.json" || -f "$R/vm2.json" ]]; then
        step 90-cleanup python3 cloud.py terminate "$R" || result=1
    fi
    {
        if (( result == 0 )); then printf 'PASS'; else printf 'FAIL'; fi
        printf ' AWS JavaScript tracer verification (%s)\n' "$(date -u +%FT%TZ)"
        if [[ -f "$R/steps.txt" ]]; then cat "$R/steps.txt"; fi
        if [[ -f "$R/50-evaluate.txt" ]]; then cat "$R/50-evaluate.txt"; fi
        printf 'Evidence: %s\n' "$R"
    } | tee "$R/summary.txt"
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

step 00-deploy python3 cloud.py deploy "$R" || exit "$?"
step 10-run python3 cloud.py run "$R"
run_status=$?
step 20-terminate python3 cloud.py terminate "$R"
cleanup_status=$?
printf 'Waiting %ss for Datadog ingestion...\n' "$CHECK_DELAY"
sleep "$CHECK_DELAY"
step 40-cloudwatch python3 cloud.py logs "$R"
logs_status=$?
# Still evaluate failed/missing guest evidence: absence is never a silent skip.
step 50-evaluate dd-auth --domain "${DD_AUTH_DOMAIN:-dddev.datadoghq.com}" -- \
    python3 "$SCRIPT_DIR/check.py" "$R"
evaluate_status=$?
(( run_status == 0 && cleanup_status == 0 && logs_status == 0 && evaluate_status == 0 ))
