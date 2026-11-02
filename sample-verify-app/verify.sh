#!/usr/bin/env zsh
# =============================================================================
# Drives the serverless-init suspend/resume/terminate verification runs.
# See README.md for what each run answers and what to look for.
#
# Usage:
#   ./verify.sh deploy long         image sample-verify-app-long    (runtime hook timeouts 5s)
#   ./verify.sh deploy default      image sample-verify-app-default (AWS default 1s timeouts)
#   ./verify.sh deploy fwd          image sample-verify-app-fwd     (long timeouts, hooks forwarded to app.py)
#   ./verify.sh deploy delivery     forwarded hooks + controlled Agent backend relays
#   ./verify.sh run1                long:    suspend → resume → suspend → terminated while SUSPENDED
#   ./verify.sh run2                long:    terminate while RUNNING
#   ./verify.sh run3                default: suspend → resume → terminate while RUNNING
#   ./verify.sh run4                long:    suspend → resume → suspend again within ~3s → terminate while SUSPENDED
#   ./verify.sh run5                fwd:     suspend → resume → terminate while RUNNING, every hook forwarded
#   ./verify.sh run6                fwd:     suspend → terminate while SUSPENDED, every hook forwarded
#   ./verify.sh run7 [--output FILE] automatic delivery, recovery, three cycles, final marker
#   ./verify.sh logs  <long|default|fwd> [since]   serverless-init lifecycle/flush/send-failure/trace-shutdown/CSA-flush lines, app hook lines
#   ./verify.sh clock <long|default|fwd> [since]   app clock lines (wall / mono / uptime)
#   ./verify.sh spans <long|default|fwd> [since]   mvmtest.burst spans created, summed per 10s stats bucket
#   ./verify.sh check <run1|...|run6> [vm-id]   PASS/FAIL per criterion (default: latest VM of that run)
#
# Every action is timestamped (UTC) into ./events.log.
#
# Config via env (defaults match deploy-microvm.sh):
#   DD_API_KEY (required for deploy), DD_SITE, REGION, BUILD_ROLE_ARN,
#   EXECUTION_ROLE_ARN, BASE_IMAGE_ARN, S3_BUCKET,
#   SERVERLESS_INIT_BIN (local arm64 serverless-init binary copied into the image;
#                        default: datadog-agent/serverless-init-linux-arm64)
#   SPANS_PER_SEC (deploy: extra mvmtest.burst spans per second; default 0 = off)
#   STATS_COMPUTATION (deploy: true = tracer-computed APM stats; default false)
#   STATS_INTERVAL (deploy: tracer stats bucket/flush seconds; default 10)
#   DD_LOG_PAYLOADS (deploy: log serialized metrics/sketches for diagnosis; default false)
#   DD_AUTH_DOMAIN (check: dd-auth org to query; default dddev.datadoghq.com)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
APP_NAME=sample-verify-app
REGION="${REGION:-us-east-2}"
DD_SITE="${DD_SITE:-datadoghq.com}"
BUILD_ROLE_ARN="${BUILD_ROLE_ARN:-arn:aws:iam::425362996713:role/microvm-build-role}"
EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN:-$BUILD_ROLE_ARN}"
BASE_IMAGE_ARN="${BASE_IMAGE_ARN:-arn:aws:lambda:${REGION}:aws:microvm-image:al2023-1}"
S3_BUCKET="${S3_BUCKET:-$APP_NAME}"
SERVERLESS_INIT_BIN="${SERVERLESS_INIT_BIN:-/Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64}"
EVENTS_LOG="$SCRIPT_DIR/events.log"
AWS_ARGS=(--region "$REGION")

awsv() { AWS_PAGER="" aws-vault exec sso-serverless-sandbox-account-admin -- "$@"; }
log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
note() { print -r -- "$(date -u +%FT%TZ) $*" | tee -a "$EVENTS_LOG"; }

image_name() { print -r -- "$APP_NAME-$1"; }

hooks_json() {
  if [[ "$1" == delivery ]]; then
    hooks_json fwd | jq '.microvmHooks |= with_entries(if .key | endswith("TimeoutInSeconds") then .value = 30 else . end)'
    return
  fi
  # fwd: forward (1s) + flush (1.5s) fits the 5s timeouts of the long image.
  if [[ "$1" == long || "$1" == fwd ]]; then
    cat <<'EOF'
{"port": 9000,
 "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 60,
                       "validate": "ENABLED", "validateTimeoutInSeconds": 30},
 "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 5,
                  "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
                  "suspend": "ENABLED", "suspendTimeoutInSeconds": 5,
                  "terminate": "ENABLED", "terminateTimeoutInSeconds": 5}}
EOF
  else
    cat <<'EOF'
{"port": 9000,
 "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 60,
                       "validate": "ENABLED", "validateTimeoutInSeconds": 30},
 "microvmHooks": {"run": "ENABLED", "resume": "ENABLED",
                  "suspend": "ENABLED", "terminate": "ENABLED"}}
EOF
  fi
}

# DD_TRACE_STATS_COMPUTATION_ENABLED=false (default) keeps APM stats computed by the
# agent's concentrator, which is what the terminate check (H2) exercises.
# STATS_COMPUTATION=true makes the tracer compute them and send them to the
# agent's ClientStatsAggregator instead. STATS_INTERVAL sets the tracer's stats
# bucket size and flush interval in seconds (ddtrace default 10).
# With SPANS_PER_SEC > 0 the tracer flushes every 100ms instead of 1s, so trace
# payloads keep arriving during the few ms between serverless-init's flushes (R2).
# fwd: serverless-init forwards ready/run/resume/suspend/terminate to app.py on
# HOOK_PORT (R3); validate stays with serverless-init. /ready must be forwarded:
# serverless-init's own /ready answers 200 once the child process exists, so
# the snapshot can catch python mid-import, and the forwarded /run right after
# the restore then finds nothing on HOOK_PORT (503, VM terminated). Forwarded,
# /ready waits until app.py accepts connections.
env_json() {
  local spans="${SPANS_PER_SEC:-0}" writer=1 fwd=false delivery=false
  [[ "$spans" != 0 ]] && writer=0.1
  [[ "$1" == fwd || "$1" == delivery ]] && fwd=true
  [[ "$1" == delivery ]] && delivery=true
  jq -n --arg key "$DD_API_KEY" --arg site "$DD_SITE" --arg version "$1" \
    --arg spans "$spans" --arg writer "$writer" --arg stats "${STATS_COMPUTATION:-false}" \
    --arg log_payloads "${DD_LOG_PAYLOADS:-false}" \
    --arg stats_interval "${STATS_INTERVAL:-10}" --argjson fwd "$fwd" --argjson delivery "$delivery" '{
    DD_API_KEY: $key, DD_SITE: $site, DD_ENV: "devmicrovm", DD_VERSION: $version,
    DD_LOGS_ENABLED: "true", DD_LOG_LEVEL: "debug",
    DD_LOG_PAYLOADS: $log_payloads,
    DD_ENHANCED_METRICS_ENABLED: "true",
    DD_TRACE_AGENT_URL: "http://localhost:8126",
    DD_TRACE_STATS_COMPUTATION_ENABLED: $stats,
    _DD_TRACE_STATS_WRITER_INTERVAL: $stats_interval,
    DD_TRACE_WRITER_INTERVAL_SECONDS: $writer,
    LOG_PAD_BYTES: "1024",
    SPANS_PER_SEC: $spans
  } + (if $fwd then {
    HOOK_PORT: "8081",
    DD_AWS_MICROVM_USER_APP_PORT: "8081",
    DD_AWS_MICROVM_ENABLE_READY: "true",
    DD_AWS_MICROVM_ENABLE_RUN: "true",
    DD_AWS_MICROVM_ENABLE_RESUME: "true",
    DD_AWS_MICROVM_ENABLE_SUSPEND: "true",
    DD_AWS_MICROVM_ENABLE_TERMINATE: "true"
  } else {} end) + (if $delivery then {
    DELIVERY_ENABLED: "true",
    DD_DD_URL: "http://127.0.0.1:18127",
    DD_LOGS_CONFIG_LOGS_DD_URL: "http://127.0.0.1:18128",
    DD_LOGS_CONFIG_USE_HTTP: "true",
    DD_LOGS_CONFIG_LOGS_NO_SSL: "true"
  } else {} end)'
}

cmd_deploy() {
  local variant="${1:?deploy <long|default|fwd|delivery>}"
  [[ "$variant" == long || "$variant" == default || "$variant" == fwd || "$variant" == delivery ]] || { log "ERROR: variant must be long, default, fwd or delivery"; exit 1; }
  : "${DD_API_KEY:?DD_API_KEY is required for deploy}"
  [[ -f "$SERVERLESS_INIT_BIN" ]] || { log "ERROR: serverless-init binary not found: $SERVERLESS_INIT_BIN"; exit 1; }

  local name ts zip s3_uri existing image_json image_arn image_version state bin_sha
  name="$(image_name "$variant")"
  ts="$(date +%Y%m%d_%H%M%S)"
  zip="$(dirname "$SCRIPT_DIR")/$name.$ts.zip"
  s3_uri="s3://$S3_BUCKET/$variant/$ts.zip"

  cp "$SERVERLESS_INIT_BIN" "$SCRIPT_DIR/serverless-init-linux-arm64"
  bin_sha="$(shasum -a 256 "$SCRIPT_DIR/serverless-init-linux-arm64" | cut -c1-12)"
  log "      serverless-init: $SERVERLESS_INIT_BIN (sha256 $bin_sha)"

  log "[1/4] Zipping → $zip"
  ( cd "$SCRIPT_DIR" && zip -q "$zip" Dockerfile app.py delivery.py requirements.txt serverless-init-linux-arm64 )

  log "[2/4] Uploading → $s3_uri"
  awsv aws s3api head-bucket --bucket "$S3_BUCKET" "${AWS_ARGS[@]}" 2>/dev/null \
    || awsv aws s3 mb "s3://$S3_BUCKET" "${AWS_ARGS[@]}"
  awsv aws s3 cp "$zip" "$s3_uri" "${AWS_ARGS[@]}"

  local image_args=(
    --code-artifact "uri=$s3_uri"
    --base-image-arn "$BASE_IMAGE_ARN"
    --build-role-arn "$BUILD_ROLE_ARN"
    --hooks "$(hooks_json "$variant")"
    --environment-variables "$(env_json "$variant")"
  )
  existing=$(awsv aws lambda-microvms list-microvm-images --name-filter "$name" --no-paginate "${AWS_ARGS[@]}" \
    --query "items[?name=='$name'] | [0].imageArn" --output text)
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    log "[3/4] Updating image $name (new version)"
    image_json=$(awsv aws lambda-microvms update-microvm-image --image-identifier "$existing" \
      "${image_args[@]}" "${AWS_ARGS[@]}" --output json)
  else
    log "[3/4] Creating image $name"
    image_json=$(awsv aws lambda-microvms create-microvm-image --name "$name" \
      "${image_args[@]}" "${AWS_ARGS[@]}" --output json)
  fi
  image_arn=$(print -r -- "$image_json" | jq -r '.imageArn')
  image_version=$(print -r -- "$image_json" | jq -r '.imageVersion')

  log "[4/4] Waiting for $image_arn version $image_version"
  while :; do
    state=$(awsv aws lambda-microvms get-microvm-image-version --image-identifier "$image_arn" \
      --image-version "$image_version" "${AWS_ARGS[@]}" --query state --output text 2>/dev/null || print UNKNOWN)
    log "      state=$state"
    case "$state" in
      SUCCESSFUL) break ;;
      FAILED) log "ERROR: build failed; see CloudWatch /aws/lambda-microvms/$name"; exit 1 ;;
    esac
    sleep 10
  done
  printf 'IMAGE_ARN=%q\nIMAGE_VERSION=%q\n' "$image_arn" "$image_version" > "$SCRIPT_DIR/.image-$variant"
  note "deploy $variant $image_arn:$image_version serverless-init=$SERVERLESS_INIT_BIN sha256=$bin_sha spans_per_sec=${SPANS_PER_SEC:-0} stats_computation=${STATS_COMPUTATION:-false} stats_interval=${STATS_INTERVAL:-10} distribution_control=true"
}

# --- run helpers ------------------------------------------------------------
load_image() {
  [[ -f "$SCRIPT_DIR/.image-$1" ]] || { log "ERROR: no .image-$1 — run ./verify.sh deploy $1 first"; exit 1; }
  source "$SCRIPT_DIR/.image-$1"
}

# run_vm <variant> [idle-policy-json] → prints microvmId
run_vm() {
  load_image "$1"
  local idle=()
  [[ -n "${2:-}" ]] && idle=(--idle-policy "$2")
  awsv aws lambda-microvms run-microvm --image-identifier "$IMAGE_ARN" --image-version "$IMAGE_VERSION" \
    --execution-role-arn "$EXECUTION_ROLE_ARN" "${idle[@]}" "${AWS_ARGS[@]}" \
    --query microvmId --output text
}

vm_state() {
  awsv aws lambda-microvms get-microvm --microvm-identifier "$1" "${AWS_ARGS[@]}" \
    --query '[state, stateReason]' --output text 2>/dev/null || print NOT_FOUND
}

# wait_state <id> <STATE> <label> [poll-seconds]: poll every 5s by default; TERMINATED also accepts NOT_FOUND.
# get-microvm is eventually consistent — CloudWatch lifecycle lines are the precise times.
# Exits 1 if the VM ends up TERMINATED while waiting for another state, so a VM
# the platform killed (e.g. a hook returned 503) doesn't hang the run. NOT_FOUND
# isn't fatal here: a just-created VM can briefly be NOT_FOUND.
wait_state() {
  local s
  while :; do
    s="$(vm_state "$1")"
    [[ "$s" == "$2"* ]] && break
    [[ "$2" == TERMINATED && "$s" == NOT_FOUND ]] && break
    if [[ "$2" != TERMINATED && "$s" == TERMINATED* ]]; then
      note "$3 $1 ERROR: wanted $2, got $s"
      exit 1
    fi
    sleep "${4:-5}"
  done
  note "$3 $1 reached $s"
}

suspend_vm()   { awsv aws lambda-microvms suspend-microvm   --microvm-identifier "$1" "${AWS_ARGS[@]}" >/dev/null; note "$2 $1 suspend-microvm called"; }
resume_vm()    { awsv aws lambda-microvms resume-microvm    --microvm-identifier "$1" "${AWS_ARGS[@]}" >/dev/null; note "$2 $1 resume-microvm called"; }
terminate_vm() { awsv aws lambda-microvms terminate-microvm --microvm-identifier "$1" "${AWS_ARGS[@]}" >/dev/null; note "$2 $1 terminate-microvm called"; }

# --- runs -------------------------------------------------------------------
cmd_run1() {
  local id
  id=$(run_vm long '{"maxIdleDurationSeconds":60,"suspendedDurationSeconds":120,"autoResumeEnabled":false}')
  note "run1 $id run-microvm called (image long)"
  wait_state "$id" SUSPENDED run1        # idle suspend #1
  sleep 30
  resume_vm "$id" run1
  wait_state "$id" RUNNING run1
  wait_state "$id" SUSPENDED run1        # idle suspend #2
  wait_state "$id" TERMINATED run1       # after suspendedDurationSeconds while SUSPENDED (Q1)
  log "run1 done: ./verify.sh logs long 20m"
}

cmd_run2() {
  local id
  id=$(run_vm long)
  note "run2 $id run-microvm called (image long, no idle policy)"
  wait_state "$id" RUNNING run2
  sleep 60
  terminate_vm "$id" run2
  wait_state "$id" TERMINATED run2
  log "run2 done: ./verify.sh logs long 10m"
}

cmd_run3() {
  local id
  id=$(run_vm default '{"maxIdleDurationSeconds":60,"suspendedDurationSeconds":300,"autoResumeEnabled":false}')
  note "run3 $id run-microvm called (image default)"
  wait_state "$id" SUSPENDED run3
  sleep 30
  resume_vm "$id" run3
  wait_state "$id" RUNNING run3
  sleep 10
  terminate_vm "$id" run3
  wait_state "$id" TERMINATED run3
  log "run3 done: ./verify.sh logs default 15m"
}

# Suspend right after a resume, so /suspend's forced trace flush may be the
# first send after the platform killed the outbound connections. The VM is then
# terminated while suspended, so /suspend's flush is the last chance to send.
cmd_run4() {
  load_image long
  awsv uv run --script "$SCRIPT_DIR/run4.py" \
    --image "$IMAGE_ARN" --version "$IMAGE_VERSION" \
    --role "$EXECUTION_ROLE_ARN" --region "$REGION" --events-log "$EVENTS_LOG"
  log "run4 done: ./verify.sh logs long 10m"
}

# Every hook forwarded to app.py (R3). The resumed /suspend must keep the trace
# agent running (flushForSuspend); /terminate must flush after the app answered
# and stop it (flushAndStop).
cmd_run5() {
  local id
  id=$(run_vm fwd)
  note "run5 $id run-microvm called (image fwd, no idle policy)"
  wait_state "$id" RUNNING run5
  sleep 30
  suspend_vm "$id" run5
  wait_state "$id" SUSPENDED run5
  sleep 30
  resume_vm "$id" run5
  wait_state "$id" RUNNING run5
  sleep 30                               # trace hits after the resume prove /suspend didn't stop the agent
  terminate_vm "$id" run5
  wait_state "$id" TERMINATED run5
  log "run5 done: ./verify.sh logs fwd 10m"
}

# Forwarded /suspend as the last hook (R3, #58960 over the forwarder path): the
# VM is terminated while SUSPENDED, so nothing after the suspend flush is sent.
cmd_run6() {
  local id
  id=$(run_vm fwd)
  note "run6 $id run-microvm called (image fwd, no idle policy)"
  wait_state "$id" RUNNING run6
  sleep 30
  suspend_vm "$id" run6
  wait_state "$id" SUSPENDED run6
  terminate_vm "$id" run6                # terminated while SUSPENDED: no /terminate
  wait_state "$id" TERMINATED run6
  log "run6 done: ./verify.sh logs fwd 10m"
}

# One AWS session also owns the backend polling and incremental JSON evidence.
cmd_run7() {
  load_image delivery
  dd-auth --domain "${DD_AUTH_DOMAIN:-dddev.datadoghq.com}" --force-app-key -- \
    aws-vault exec sso-serverless-sandbox-account-admin -- \
    uv run --script "$SCRIPT_DIR/run_delivery.py" \
    --image "$IMAGE_ARN" --version "$IMAGE_VERSION" --role "$EXECUTION_ROLE_ARN" \
    --region "$REGION" --events-log "$EVENTS_LOG" "$@"
}

cmd_logs() {
  awsv aws logs tail "/aws/lambda-microvms/$(image_name "${1:?logs <long|default|fwd> [since]}")" \
    --since "${2:-20m}" --format short "${AWS_ARGS[@]}" \
    --filter-pattern '?"MicroVM lifecycle" ?"Could not send payload" ?"Exiting concentrator" ?"Exiting..." ?"did not stop in time" ?"HTTPReceiver" ?"css aggregator" ?apphook ?sketch ?Sketch ?ERROR ?WARN'
}

cmd_clock() {
  awsv aws logs tail "/aws/lambda-microvms/$(image_name "${1:?clock <long|default|fwd> [since]}")" \
    --since "${2:-20m}" --format short "${AWS_ARGS[@]}" \
    --filter-pattern '{ $.msg = "clock" }'
}

# Ground truth for R2: spans created per 10s bucket (CloudWatch = what was printed).
cmd_spans() {
  awsv aws logs filter-log-events --log-group-name "/aws/lambda-microvms/$(image_name "${1:?spans <long|default|fwd> [since]}")" \
    --start-time "$(( ($(date +%s) - $(to_seconds "${2:-20m}")) * 1000 ))" \
    --filter-pattern '{ $.msg = "spans" }' "${AWS_ARGS[@]}" --output json \
  | jq -r '[.events[].message | fromjson? | select(.msg == "spans")] | group_by(.sec - .sec % 10)[]
           | "\(.[0].sec - .[0].sec % 10 | strftime("%H:%M:%S"))Z  spans=\(map(.n) | add)"'
}
to_seconds() { case "$1" in *m) print $(( ${1%m} * 60 )) ;; *h) print $(( ${1%h} * 3600 )) ;; *) print "$1" ;; esac; }

# The default PAT returned empty enhanced-metric queries in dddev while an
# application key returned the same VM's expected count. Use application-key
# auth for this verification; keys stay in the child process environment.
cmd_check() {
  print "checking Datadog org=${DD_AUTH_DOMAIN:-dddev.datadoghq.com} API=${DD_API_URL:-https://api.datadoghq.com} auth=application-key"
  dd-auth --domain "${DD_AUTH_DOMAIN:-dddev.datadoghq.com}" --force-app-key -- \
    env REGION="$REGION" AWS_VAULT_PROFILE=sso-serverless-sandbox-account-admin \
    python3 "$SCRIPT_DIR/check.py" "$@"
}

case "${1:-}" in
  deploy) cmd_deploy "${2:-}" ;;
  run1)   cmd_run1 ;;
  run2)   cmd_run2 ;;
  run3)   cmd_run3 ;;
  run4)   cmd_run4 ;;
  run5)   cmd_run5 ;;
  run6)   cmd_run6 ;;
  run7)   cmd_run7 "${@:2}" ;;
  logs)   cmd_logs "${2:-}" "${3:-}" ;;
  clock)  cmd_clock "${2:-}" "${3:-}" ;;
  spans)  cmd_spans "${2:-}" "${3:-}" ;;
  check)  cmd_check "${@:2}" ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
