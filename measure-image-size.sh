#!/usr/bin/env zsh
# =============================================================================
# Lambda MicroVM — build a project's Docker image locally and report its size.
#
# Workaround companion to `get-image-size.sh`: the server-side
# `snapshotSizeBytes` field is currently un-populated by the preview
# service (see PREVIEW-CAVEATS.md). Building the same Dockerfile locally
# and inspecting the resulting image is the closest proxy you can get
# today — it reflects the summed rootfs layer size after extraction,
# which is what the MicroVM build pipeline also starts from before adding
# kernel + memory-at-snapshot-time overhead.
#
# Usage:    ./measure-image-size.sh <app-dir> [<tag>]
# Example:  ./measure-image-size.sh sample-flask-app-datadog-agent-poc
#
# Positional args:
#   <app-dir>  REQUIRED. Directory containing the Dockerfile to measure.
#              Must be an existing directory with a Dockerfile at its root.
#   <tag>      OPTIONAL. Docker image tag to use.
#              Default: <app-dir-basename>:local-measure
#
# Requires:
#   docker   — with buildx support and Linux/arm64 cross-platform enabled.
#              On Intel Macs this means QEMU emulation (set up via Docker
#              Desktop's "Use containerd for pulling and storing images"
#              option, or `docker run --privileged --rm tonistiigi/binfmt
#              --install arm64`). Native on Apple Silicon.
#   jq       — used to parse docker image inspect output robustly.
#   numfmt   — optional; if missing, size is reported in bytes only.
# =============================================================================

APP_DIR="${1:?Usage: $0 <app-dir> [<tag>]}"

if [[ ! -d "$APP_DIR" ]]; then
  print -u2 "ERROR: $APP_DIR is not a directory."
  exit 1
fi

APP_DIR="$(cd "$APP_DIR" && pwd)"
APP_NAME="$(basename "$APP_DIR")"
TAG="${2:-${APP_NAME}:local-measure}"

if [[ ! -f "$APP_DIR/Dockerfile" ]]; then
  print -u2 "ERROR: No Dockerfile at $APP_DIR/Dockerfile"
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  print -u2 "ERROR: docker is required"
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  print -u2 "ERROR: jq is required (parses docker inspect output)"
  print -u2 "       Install with: brew install jq"
  exit 1
fi

set -euo pipefail

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Auto-remove the built image on exit (success or failure). The script's
# whole purpose is measurement — the image has no downstream use, and
# leaving 350+MiB sitting in the local daemon after every run wastes
# disk quickly during iteration. `|| true` absorbs the "image doesn't
# exist yet" case when we exit before the build finished.
cleanup_image() {
  if docker image inspect "$TAG" >/dev/null 2>&1; then
    log "[cleanup] Removing local image $TAG"
    docker image rm "$TAG" >/dev/null 2>&1 || true
  fi
}
trap cleanup_image EXIT

log "App dir:   $APP_DIR"
log "Image tag: $TAG"
log "Platform:  linux/arm64"
log ""

# --- Build ------------------------------------------------------------------
# `buildx build --load` writes the finished image to the local daemon so
# `docker image inspect` can find it. Without --load, buildx leaves the
# result in the buildkit cache only, and `inspect` fails.
log "[1/2] Building (this can take several minutes on the first run, and"
log "      longer if linux/arm64 is emulated via QEMU on a non-ARM host)"

build_failed() {
  print -u2 ""
  print -u2 "ERROR: docker build failed — see the output above for the root cause."
  print -u2 ""
  print -u2 "If the failure is a 403 Forbidden on public.ecr.aws/amazonlinux/…,"
  print -u2 "buildx's image resolver needs an anonymous public-ECR login (even"
  print -u2 "though plain 'docker pull' works fine against the same URL). Run:"
  print -u2 ""
  print -u2 "    awsv aws ecr-public get-login-password --region us-east-1 \\"
  print -u2 "      | docker login --username AWS --password-stdin public.ecr.aws"
  print -u2 ""
  print -u2 "Then re-invoke this script:"
  print -u2 ""
  print -u2 "    $0 $APP_NAME"
  print -u2 ""
  exit 1
}

if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform=linux/arm64 \
    --load \
    -t "$TAG" \
    "$APP_DIR" \
    || build_failed
else
  log "      (buildx not available; falling back to 'docker build')"
  docker build \
    --platform=linux/arm64 \
    -t "$TAG" \
    "$APP_DIR" \
    || build_failed
fi

# --- Measure ----------------------------------------------------------------
log "[2/2] Inspecting image"

INSPECT_JSON=$(docker image inspect "$TAG")
SIZE_BYTES=$(print -r -- "$INSPECT_JSON" | jq -r '.[0].Size')
IMAGE_ID=$(print -r -- "$INSPECT_JSON" | jq -r '.[0].Id')
CREATED=$(print -r -- "$INSPECT_JSON" | jq -r '.[0].Created')
ARCH=$(print -r -- "$INSPECT_JSON" | jq -r '.[0].Architecture')
OS=$(print -r -- "$INSPECT_JSON" | jq -r '.[0].Os')

HUMAN=""
if command -v numfmt >/dev/null 2>&1; then
  HUMAN="  ($(numfmt --to=iec-i --suffix=B "$SIZE_BYTES"))"
fi

# --- Compute sum of uncompressed layer sizes ------------------------------
# `docker image inspect .Size` reports the COMPRESSED/deduplicated on-disk
# footprint (~3-4× smaller than uncompressed for Python/Debian content).
# That's the right number for "how much Docker-daemon disk is this image
# using," but the WRONG number for "how big will the Firecracker snapshot
# be" — MicroVM snapshots serialize the EXTRACTED rootfs, which is closer
# to the uncompressed total. So report both.
#
# docker history sizes come out as strings like "318MB", "45.7MB", "2.1kB".
# Parse to bytes so we can sum them.
UNCOMPRESSED_BYTES=$(
  docker history --format '{{.Size}}' "$TAG" \
    | awk '{
        val = $0 + 0;
        unit = substr($0, length(val)+1);
        mult = 1;
        if (unit ~ /^kB/)  mult = 1000;
        else if (unit ~ /^MB/) mult = 1000000;
        else if (unit ~ /^GB/) mult = 1000000000;
        total += val * mult;
      }
      END { printf "%.0f\n", total }'
)
UNCOMPRESSED_HUMAN=""
if command -v numfmt >/dev/null 2>&1 && [[ "$UNCOMPRESSED_BYTES" -gt 0 ]]; then
  UNCOMPRESSED_HUMAN="  ($(numfmt --to=iec-i --suffix=B "$UNCOMPRESSED_BYTES"))"
fi

# --- Report -----------------------------------------------------------------
print ""
print "=============================================================="
print "  Local image measurement"
print "=============================================================="
print "  Tag:     $TAG"
print "  ID:      $IMAGE_ID"
print "  OS/Arch: $OS/$ARCH"
print "  Created: $CREATED"
print ""
print "  Size (compressed, on-daemon-disk):"
print "    $SIZE_BYTES bytes$HUMAN"
print "    ^ Docker's 'docker image inspect .Size' — the dedup-aware,"
print "      compressed storage footprint. Close to what gets pulled."
print ""
print "  Size (uncompressed rootfs — better MicroVM-snapshot proxy):"
print "    $UNCOMPRESSED_BYTES bytes$UNCOMPRESSED_HUMAN"
print "    ^ Sum of 'docker history' layer additions. Close to what"
print "      the MicroVM service will serialize into a Firecracker"
print "      snapshot at /ready time (rootfs + memory state)."
print "=============================================================="
print ""

# --- Layer breakdown --------------------------------------------------------
# `docker history` is newest-first by default, which pushes the biggest
# layers (AL2023 base, agent RPM) off the top of the output for any
# Dockerfile that finishes with many small COPY/ENV steps. We re-sort
# by size descending so the biggest culprits land at the top — that's
# what matters for "why is my image so big?" investigations.
print "Layers by size (largest first):"
docker history --format '{{.Size}}\t{{.CreatedBy}}' --no-trunc=false "$TAG" \
  | awk -F'\t' '{
      sz = $1;
      # Parse the human-readable size (e.g. "318MB", "45.7MB", "2.1kB", "0B")
      # into bytes for sorting. Not precise — just good enough for ordering.
      val = sz + 0;
      unit = substr(sz, length(val)+1);
      mult = 1;
      if (unit ~ /^kB/)  mult = 1000;
      else if (unit ~ /^MB/) mult = 1000000;
      else if (unit ~ /^GB/) mult = 1000000000;
      printf "%15.0f\t%s\t%s\n", val * mult, sz, $2;
    }' \
  | sort -rn \
  | awk -F'\t' 'BEGIN { printf "%-10s  %s\n", "SIZE", "CREATED BY"; print "---------- ---------------------------" }
                { printf "%-10s  %s\n", $2, substr($3, 1, 100) }' \
  | head -15
print ""

# --- Caveats ----------------------------------------------------------------
print "Caveats:"
print " * Two sizes are reported because they measure different things:"
print "     - Compressed (.Size)  — what the Docker daemon stores on disk"
print "     - Uncompressed layers — what an extracted rootfs would occupy"
print "   For 'will my MicroVM image be too big?' questions, the"
print "   uncompressed number is the better proxy — Firecracker's"
print "   snapshot serializes the EXTRACTED running filesystem plus the"
print "   guest's memory state, not the compressed blob the daemon holds."
print "   The typical ratio is ~3–4x (higher for Python/Debian content)."
print ""
print " * Both numbers exclude the MicroVM platform's additions:"
print "     - kernel + initramfs the MicroVM layer adds before snapshotting"
print "     - memory-at-snapshot-time state (Firecracker serializes guest"
print "       RAM into the snapshot at /ready time)"
print ""
print " * The server-side \`snapshotSizeBytes\` field is currently not"
print "   populated by the preview service — see PREVIEW-CAVEATS.md for"
print "   the tracked issue and alternative workarounds."
print ""
print " * The built image is removed automatically when this script exits."
print "   To keep it around for debugging, comment out the"
print "   'trap cleanup_image EXIT' line near the top of this script."
