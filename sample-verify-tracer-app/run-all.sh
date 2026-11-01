#!/usr/bin/env bash
# Deploy to AWS -> run a real snapshot clone -> terminate -> wait -> evaluate in Datadog.
# DD_API_KEY is required for deployment. dd-auth supplies API/app keys for queries.
# See README.md for regional, role, artifact, and authentication overrides.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
for argument in "$@"; do
    case "$argument" in
        --check-runtime-metrics) ;;
        *) printf 'Usage: %s [--check-runtime-metrics]\n' "$0" >&2; exit 2 ;;
    esac
done
: "${DD_API_KEY:?DD_API_KEY is required for AWS deployment}"
export REGION="${REGION:-us-east-2}"
export AWS_VAULT_PROFILE="${AWS_VAULT_PROFILE:-sso-serverless-sandbox-account-admin}"
export DD_SITE="${DD_SITE:-datadoghq.com}"
CHECK_DELAY="${CHECK_DELAY:-180}"
[[ "$CHECK_DELAY" =~ ^[0-9]+$ ]] || { printf 'CHECK_DELAY must be a nonnegative integer\n' >&2; exit 1; }
for tool in aws aws-vault dd-auth make python3 zip tee; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done
R="${RESULTS_DIR:-results/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
mkdir -p "$(dirname "$R")"
mkdir "$R" || exit 1
R="$(cd "$R" && pwd)"
printf 'Results -> %s\n' "$R"
terminated=0
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
    if [[ -f "$R/vm.json" ]] && (( ! terminated )); then
        step 90-cleanup python3 cloud.py terminate "$R" || result=1
    fi
    {
        if (( result == 0 )); then printf 'PASS'; else printf 'FAIL'; fi
        printf ' AWS tracer verification (%s)\n' "$(date -u +%FT%TZ)"
        cat "$R/steps.txt"
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
step 20-terminate python3 cloud.py terminate "$R" || exit "$?"
terminated=1
printf 'Waiting %ss for Datadog ingestion...\n' "$CHECK_DELAY"
sleep "$CHECK_DELAY"
step 40-cloudwatch python3 cloud.py logs "$R"
logs_status=$?
# Run evaluation even if execution failed; missing evidence must produce FAIL.
step 50-evaluate dd-auth --domain "${DD_AUTH_DOMAIN:-dddev.datadoghq.com}" -- \
    python3 "$SCRIPT_DIR/check.py" "$R" "$@"
evaluate_status=$?
(( run_status == 0 && logs_status == 0 && evaluate_status == 0 ))
