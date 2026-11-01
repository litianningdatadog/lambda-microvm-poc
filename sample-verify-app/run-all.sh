#!/usr/bin/env zsh
# =============================================================================
# Runs the full verification suite for #58958 (/terminate) and #58960 (/suspend)
# and saves every deploy, run, check, CloudWatch and span output under
# results/<UTC timestamp>/, ending with summary.txt.
#
# Usage:
#   ./run-all.sh
#
# Requires DD_API_KEY (baked into the images; its org must match DD_AUTH_DOMAIN,
# default dddev.datadoghq.com), dd-auth, aws-vault, jq, uv. Takes about 100 minutes
# with the default three run4 attempts.
# A failing step doesn't stop the suite; its output is still saved.
# Config via env: SERVERLESS_INIT_BIN, CHECK_DELAY (seconds before each check,
# default 180, so Datadog has finished ingesting). RUN4_ATTEMPTS defaults to 3
# so the short resume → suspend window is exercised repeatedly; set it to 1 to
# run the original single attempt. run5 + run6 add about 10 minutes.
# Resume an interrupted suite into its results dir, skipping steps numbered below
# START_AT: RESULTS_DIR=results/<ts> START_AT=44 ./run-all.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
: "${DD_API_KEY:?DD_API_KEY is required}"
CHECK_DELAY="${CHECK_DELAY:-180}"
START_AT="${START_AT:-0}"
RUN4_ATTEMPTS="${RUN4_ATTEMPTS:-3}"
BIN="${SERVERLESS_INIT_BIN:-/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64}"
[[ -f "$BIN" ]] || { print -u2 "ERROR: serverless-init binary not found: $BIN"; exit 1; }
[[ "$RUN4_ATTEMPTS" == <-> && "$RUN4_ATTEMPTS" -ge 1 ]] || {
  print -u2 "ERROR: RUN4_ATTEMPTS must be a positive integer (got $RUN4_ATTEMPTS)"
  exit 1
}

R="${RESULTS_DIR:-results/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$R"
print "results -> $SCRIPT_DIR/$R (from step $START_AT)"
print "binary   -> $BIN (sha256 $(shasum -a 256 "$BIN" | cut -c1-12)) from step $START_AT" | tee -a "$R/binary.txt"

# skipped <file>: true if the step's number (the part before the first -) is below START_AT.
skipped() { (( ${1%%-*} < START_AT )); }

# step <file> <command...>: run, print and save combined output.
step() {
  local f="$1"; shift
  skipped "$f" && return
  print "\n=== $(date -u +%H:%M:%SZ) $f: $*"
  "$@" 2>&1 | tee "$R/$f.txt"
}

# run_and_check <prefix> <run> <logs variant> <logs since> [spans since]
# logs/spans look back <since> from now, so they run before the check: a slow
# check would otherwise push their window past the VM.
run_and_check() {
  skipped "$1" && return
  step "$1-$2"            ./verify.sh "$2"
  print "waiting ${CHECK_DELAY}s for Datadog ingestion..."
  sleep "$CHECK_DELAY"
  step "$1-$2-cloudwatch" ./verify.sh logs "$3" "$4"
  [[ -n "${5:-}" ]] && step "$1-$2-spans" ./verify.sh spans "$3" "$5"
  step "$1-$2-check"      ./verify.sh check "$2"
}

# Client-side stats: the tracer computes stats and the agent's ClientStatsAggregator
# (CSA) holds them before sending. The tracer's own buffer (its current and previous
# bucket) never reaches the agent on a final hook, so `trace` FAILs there by design.
# These cases are judged instead by CS_MIN_CSA / CS_MAX_MISSING per run.
typeset -A CS_MIN_CSA CS_MAX_MISSING

# cs_case <prefix> <run> <logs since> <min CSA buckets at last hook> <max missing ticks>
cs_case() {
  CS_MIN_CSA[$1-$2]=$4
  CS_MAX_MISSING[$1-$2]=$5
  run_and_check "$1" "$2" long "$3"
}

# missing_ticks <check file>: printed tick spans absent from trace.mvmtest.tick.hits,
# summed over the trace check's FAIL buckets.
missing_ticks() {
  awk '$1 ~ /^(PASS|FAIL|INFO|KNOWN)$/ && $2 ~ /^[a-z]+$/ {sec = $2; next}
       sec == "trace" && / FAIL/ {split($2, g, "="); split($3, w, "="); n += w[2] - g[2]}
       END {print n + 0}' "$1"
}

# csa_flushed <cloudwatch file>: CSA buckets flushed during the last hook's flush.
csa_flushed() {
  awk '/— flushing telemetry/ {f = 1; n = 0}
       f && /css aggregator: flushing bucket/ {n++}
       /flush complete|flush timed out/ {f = 0}
       END {print n + 0}' "$1"
}

unset SPANS_PER_SEC STATS_COMPUTATION STATS_INTERVAL
step 00-deploy-long    ./verify.sh deploy long
step 00-deploy-default ./verify.sh deploy default

run_and_check 10 run2 long    10m    # terminate while RUNNING (#58958)
run_and_check 20 run1 long    15m    # terminated while SUSPENDED (#58960)
run_and_check 30 run3 default 10m    # AWS default 1s hook timeouts
# Repeat R1: the platform/control-plane timing can otherwise leave a much
# larger resume → suspend gap than the scenario is meant to exercise. check.py
# records and enforces the <=3s hook-to-hook target for each attempt.
for attempt in $(seq 1 "$RUN4_ATTEMPTS"); do
  run_and_check "40-${attempt}" run4 long 10m
done
step 44-deploy-fwd     ./verify.sh deploy fwd
run_and_check 45 run5 fwd     10m    # every hook forwarded to app.py (R3)
run_and_check 46 run6 fwd     10m    # forwarded /suspend as the last hook (R3, #58960)

# R2: 200 spans/s, trace stats bucket overwrite check on both paths
SPANS_PER_SEC=200 step 50-deploy-long-burst ./verify.sh deploy long
run_and_check 51-r2 run2 long 10m 10m
run_and_check 52-r2 run1 long 15m 15m

# Client-side stats, default 10s tracer buckets. Tracers send buckets ~20s old, so
# the CSA flushes them within 2s and is usually empty at the hook. The tracer holds
# up to ~30s, which is lost either way.
STATS_COMPUTATION=true step 60-deploy-long-cs ./verify.sh deploy long
cs_case 61-cs run2 10m 0 30   # /terminate: Stop() flushes the CSA
cs_case 62-cs run1 15m 0 30   # final /suspend: ForceFlushSync flushes the CSA

# Client-side stats, 2s tracer buckets: payloads arrive fresh and the CSA holds up
# to ~16s. The final /suspend must flush it; only the tracer's ~4-6s stays missing.
STATS_COMPUTATION=true STATS_INTERVAL=2 step 70-deploy-long-cs2s ./verify.sh deploy long
cs_case 71-cs2s run1 15m 1 6

# Leave .image-long on the plain (agent-side stats) image for later runs.
step 90-deploy-long-restore ./verify.sh deploy long

{
  print "suite finished $(date -u +%FT%TZ)"
  cat "$R/binary.txt"
  for f in "$R"/*-check.txt(N); do
    k="${${f:t:r}%-check}"
    raw_verdict="$(grep -E '^run[0-9]: ' "$f" || print 'NO VERDICT (see file)')"
    if (( ${+CS_MIN_CSA[$k]} )); then
      csa=$(csa_flushed "$R/$k-cloudwatch.txt") missing=$(missing_ticks "$f")
      other_failures=$(awk '$1 == "FAIL" && $2 != "trace" {print $2}' "$f" | paste -sd, -)
      verdict=PASS
      (( csa >= CS_MIN_CSA[$k] && missing <= CS_MAX_MISSING[$k] )) || verdict=FAIL
      [[ -z "$other_failures" && "$raw_verdict" != 'NO VERDICT'* ]] || verdict=FAIL
      print "${f:t:r}: client stats $verdict  CSA buckets at last hook=$csa (min ${CS_MIN_CSA[$k]}), missing ticks=$missing (max ${CS_MAX_MISSING[$k]})"
      [[ -n "$other_failures" ]] && print "  other failing checks: $other_failures"
      print "  raw comparison: $raw_verdict (only trace failures may be expected from tracer-held buckets)"
    else
      print "${f:t:r}: $raw_verdict"
    fi
  done
} | tee "$R/summary.txt"
