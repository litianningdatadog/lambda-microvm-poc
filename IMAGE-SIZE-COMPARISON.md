# Lambda MicroVM sample apps — image size comparison

Measured **2026-04-24** via [`./measure-image-size.sh`](./measure-image-size.sh)
against `linux/arm64` builds (native on Apple Silicon). Rebuild this
file by running that script against each sample directory and pasting
the output into the headline table below.

## Headline numbers

| App | `.Size` (compressed, on-disk) | Uncompressed rootfs (snapshot proxy) | Ratio |
|-----|-----:|-----:|-----:|
| **`sample-flask-app`** (bare, no observability) | 51,432,099 B · **50 MiB** | 184,143,290 B · **176 MiB** | 3.6× |
| **`sample-flask-app-using-serverless-comp-poc`** | 131,071,500 B · **125 MiB** | 535,250,990 B · **511 MiB** | 4.1× |
| **`sample-flask-app-serverless-init-poc`** | 143,443,748 B · **137 MiB** | 575,000,990 B · **549 MiB** | 4.0× |
| **`sample-flask-app-datadog-agent-poc`** (full agent) | 357,134,776 B · **341 MiB** | 1,256,124,490 B · **1.20 GiB** | 3.5× |

## Delta vs. bare Flask baseline

Cost of adding each observability layer on top of the minimal Flask app:

| Variant | Δ compressed | Δ uncompressed |
|---------|-------------:|---------------:|
| `serverless-comp` (PID-1 binary) | **+75 MiB** | **+335 MiB** |
| `serverless-init` (PID-1 binary) | **+87 MiB** | **+373 MiB** |
| full `datadog-agent` | **+291 MiB** | **+1.03 GiB** |

## Compressed-vs-uncompressed interpretation

Two numbers reflect two different questions:

- **Compressed** (`docker image inspect .Size`): the Docker daemon's
  on-disk storage footprint. Closer to what's transferred over the
  network when an image is pulled.
- **Uncompressed**: the summed layer additions from `docker history`.
  Closer to what the Firecracker snapshot will serialize — because the
  snapshot captures the **extracted running filesystem** plus guest
  memory, not the compressed blob.

For answering *"how big will my MicroVM snapshot be, and how long will
it take to clone?"*, the **uncompressed column is the better proxy**.
The 3.5–4.1× compression ratio is consistent across all four, which
matches the content mix (Python bytecode, Debian/AL2023 binaries,
shared libraries — all moderately compressible).

## Key takeaways

1. **The two serverless variants are within noise of each other.**
   `serverless-init` and `serverless-comp` sit at 125–137 MiB compressed
   / 511–549 MiB uncompressed. They trade off on capability
   (`serverless-init` has richer lifecycle hooks; `serverless-comp` is
   purpose-built for init-mode with stdout capture), not on image
   weight.

2. **The full-agent variant is roughly 2.5× either serverless variant.**
   On uncompressed footprint, the DD-agent POC (1.20 GiB) is ~2.2×
   `serverless-init` (549 MiB). The extra bulk is dominated by the
   `datadog-agent` RPM (~827 MB uncompressed by itself), which ships
   checks, autodiscovery, Python/Go runtimes, and sub-agents that the
   serverless-* variants deliberately strip.

3. **A minimal Flask app is ~3× smaller than the lightest observability
   variant.** 50 MiB compressed baseline grows to 125 MiB
   (`serverless-comp`) — a 75 MiB tax to get APM/logs/lifecycle hooks.
   Reasonable for what you get.

4. **Clone latency implications.** MicroVM snapshot clone time scales
   roughly with snapshot size. A 1.20 GiB snapshot for the full-agent
   POC will clone noticeably slower than a 176 MiB bare app, with the
   serverless variants sitting in between. Exact numbers depend on the
   MicroVM platform's backing storage, but the ordering is stable.

## Platform observations

- **The `sample-flask-app-using-serverless-comp-poc` image** was built
  2026-04-16 (it's been in the buildx cache for ~1 week). The 125 MiB /
  511 MiB numbers reflect whatever `datadog-serverless-compat` binary
  version was pinned at that build time.
- **All four images** are ARM64-native; measurements on Apple Silicon
  are close to server-side Firecracker snapshot sizes (minus
  kernel/initramfs that the MicroVM layer adds post-build).
- **The server-side `snapshotSizeBytes` API field** that would normally
  give the authoritative per-image snapshot size is not currently
  populated by the preview service. See
  [`PREVIEW-CAVEATS.md`](./PREVIEW-CAVEATS.md) for the tracked gap; the
  uncompressed numbers above are the closest local proxy until the
  service catches up.

## Reproducing this comparison

```bash
for dir in sample-flask-app \
           sample-flask-app-using-serverless-comp-poc \
           sample-flask-app-serverless-init-poc \
           sample-flask-app-datadog-agent-poc; do
  ./measure-image-size.sh "$dir"
done
```

Each invocation auto-removes the built image on exit (see the cleanup
trap in `measure-image-size.sh`), so running all four in sequence
leaves zero residue in the local Docker daemon.

## Related

- [`measure-image-size.sh`](./measure-image-size.sh) — the script used
  to generate these numbers
- [`get-image-size.sh`](./get-image-size.sh) — the (currently broken)
  server-side equivalent via the MicroVM API
- [`PREVIEW-CAVEATS.md`](./PREVIEW-CAVEATS.md) — why
  `snapshotSizeBytes` isn't usable as the authoritative number yet
- [`HOWTO-DATADOG-AGENT.md`](./HOWTO-DATADOG-AGENT.md) — deeper
  discussion of the full-agent POC's image-size trade-offs
