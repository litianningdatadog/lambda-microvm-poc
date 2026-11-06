#!/usr/bin/env bash
# AWS verification of the MicroVM MVP: merged MicroVM PRs plus #21429, without
# #20956 (telemetry discard) or #20957 (runtime-metric baselines).
# Wraps run-all.sh: same deploy -> run -> terminate -> Datadog evaluation flow.
#
# The wheel must be built from a tracer revision without #20956/#20957, e.g.
# #21429's branch tip. The guest then skips those PRs' probes, and the evaluator
# fails if any of their checks appear.
#
# Config (in addition to run-all.sh's): MVP_WHEEL, MVP_SOURCE_COMMIT, IMAGE_NAME.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export WHEEL="${MVP_WHEEL:-/Users/tianning.li/.cache/pr21429-wheel-mvp/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl}"
[[ -f "$WHEEL" ]] || { printf 'Missing MVP wheel: %s\n' "$WHEEL" >&2; exit 1; }
export VERIFY_SCOPE=mvp
# A separate image keeps the full-stack image and its versions untouched.
export IMAGE_NAME="${IMAGE_NAME:-sample-verify-tracer-app-mvp}"
R="${RESULTS_DIR:-results/mvp-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
export RESULTS_DIR="$R"

./run-all.sh "$@"
status=$?
# Record what was tested next to the evidence; run-all.sh owns the directory.
if [[ -d "$R" ]]; then
    {
        printf 'scope: mvp (excludes #20956, #20957)\n'
        printf 'tracer source commit: %s\n' "${MVP_SOURCE_COMMIT:-04f1660893858ffe6707fa3e8a223075fdc51d48}"
        shasum -a 256 "$WHEEL"
    } > "$R/mvp-artifacts.txt"
fi
exit "$status"
