#!/usr/bin/env bash
# Compare /ids output captured from two (or more) MicroVM clones.
# Usage: ./compare.sh A.json B.json [C.json ...]
#
# Compares BOTH groups:
#   buildSamples -- frozen AT the snapshot (the shared-snapshot precondition).
#   runSamples   -- generated post-resume (the actual behavior under test).
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 A.json B.json [C.json ...]" >&2
  exit 2
fi

FIRST="$1"

echo "== env var at process start (should be non-null on every clone) =="
for f in "$@"; do
  printf '  %-10s envAtLoad=%s  envAtRequest=%s\n' "$(basename "$f")" \
    "$(jq -r '.envAtLoad' "$f")" "$(jq -r '.envAtRequest' "$f")"
done
echo

cmp_field () {  # $1=group  $2=key  $3..=files
  local group="$1" key="$2" ref same=1 f
  shift 2
  ref="$(jq -c ".$group.$key" "$FIRST")"
  for f in "$@"; do
    [ "$(jq -c ".$group.$key" "$f")" = "$ref" ] || same=0
  done
  [ "$same" -eq 1 ] && echo IDENTICAL || echo DIFFER
}

echo "== buildSamples (frozen at snapshot) -- shared-snapshot precondition =="
for k in batched perCallOpenSSL perCallKernel; do
  printf '  %-15s %s\n' "$k" "$(cmp_field buildSamples "$k" "$@")"
done
echo
echo "== runSamples (generated post-resume) -- behavior under test =="
for k in batched perCallOpenSSL perCallKernel; do
  printf '  %-15s %s\n' "$k" "$(cmp_field runSamples "$k" "$@")"
done

cat <<'EOF'

-- how to read it --
STEP 1 -- buildSamples.batched (and all buildSamples):
  IDENTICAL -> clones share ONE frozen snapshot. Precondition met; go to step 2.
  DIFFER    -> clones do NOT share a snapshot (cold-start, or different
               per-chipset builds). dd-trace's in-memory collision cannot
               reproduce on this path, so the runSamples rows are MEANINGLESS.
               The scenario must change before the source question can be answered.

STEP 2 (only if buildSamples are IDENTICAL) -- runSamples.perCallOpenSSL:
  IDENTICAL -> randomFillSync did NOT reseed -> PR #8426 source UNSAFE -> /dev/urandom.
  DIFFER    -> randomFillSync reseeded on resume -> 8426 source safe (faster).
  (runSamples.batched should be IDENTICAL here too -- frozen buffer; if not, investigate.)
EOF
