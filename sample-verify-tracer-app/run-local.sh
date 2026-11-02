#!/usr/bin/env bash
# Build -> start a fresh guest -> execute -> evaluate -> collect logs -> stop.
# Like sample-verify-app/run-all.sh, keep numbered logs and a timestamped summary.
# This first scenario runs locally in Docker; it does not deploy to AWS.
# Optional local smoke test. run-all.sh owns AWS deployment and Datadog evaluation.
# Config: WHEEL, SERVERLESS_INIT_BIN, IMAGE, PORT, RESULTS_DIR.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export IMAGE="${IMAGE:-sample-verify-tracer-app}"
export PORT="${PORT:-18080}"
export WHEEL="${WHEEL:-/Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl}"
export SERVERLESS_INIT_BIN="${SERVERLESS_INIT_BIN:-/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64}"

for command in docker make python3 shasum tee; do
    command -v "$command" >/dev/null || { printf 'Missing command: %s\n' "$command" >&2; exit 1; }
done
for artifact in "$WHEEL" "$SERVERLESS_INIT_BIN"; do
    [[ -f "$artifact" ]] || { printf 'Missing artifact: %s\n' "$artifact" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { printf 'Docker is not available\n' >&2; exit 1; }
if docker container inspect "$IMAGE" >/dev/null 2>&1; then
    printf 'Container %s already exists; choose another IMAGE or stop it first.\n' "$IMAGE" >&2
    exit 1
fi

R="${RESULTS_DIR:-results/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
# Refuse to reuse evidence from a previous run.
mkdir -p "$(dirname "$R")"
mkdir "$R" || exit 1
R="$(cd "$R" && pwd)"
printf 'Results -> %s\n' "$R"
{
    printf 'Started: %s\nImage: %s\nPort: %s\n' "$(date -u +%FT%TZ)" "$IMAGE" "$PORT"
    shasum -a 256 "$WHEEL" "$SERVERLESS_INIT_BIN"
} | tee "$R/artifacts.txt"

started=0
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
    if (( started )); then
        step 40-container-logs docker logs "$IMAGE" || result=1
        step 50-stop make stop || result=1
    fi
    {
        if (( result == 0 )); then printf 'PASS'; else printf 'FAIL'; fi
        printf ' tracer identity refresh (%s)\n' "$(date -u +%FT%TZ)"
        cat "$R/artifacts.txt" "$R/steps.txt"
        if [[ -f "$R/30-evaluate.txt" ]]; then cat "$R/30-evaluate.txt"; fi
        printf 'Evidence: %s\n' "$R"
    } | tee "$R/summary.txt"
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

step 00-build make build || exit "$?"
step 05-lifecycle-controls make check-lifecycle || exit "$?"
step 10-start make start || exit "$?"
started=1
# Evaluate even when the driver fails, preserving both the execution error and verdict.
step 20-check make check OUTPUT="$R/verification.json"
check_status=$?
step 30-evaluate python3 - "$R/verification.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.is_file():
    sys.exit("FAIL: driver produced no verification.json; see 20-check.txt")
result = json.loads(path.read_text())
checks = result.get("checks", {})
passed = sum(value is True for value in checks.values())
ok = bool(checks) and passed == len(checks) and result.get("passed") is True
print(f"{'PASS' if ok else 'FAIL'}: {passed}/{len(checks)} checks")
print(f"/run seconds: {result.get('run_seconds', 'unavailable')}")
print(f"Runtime ID: {result.get('old_runtime_id')} -> {result.get('runtime_id')}")
if result.get("error"):
    print(result["error"])
for name, value in checks.items():
    if value is not True:
        print(f"FAIL {name}")
sys.exit(0 if ok else 1)
PY
evaluate_status=$?
(( check_status == 0 && evaluate_status == 0 ))
