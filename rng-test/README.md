# MicroVM RNG test (`rng-test`)

Validation harness for **dd-trace-js [PR #8426](https://github.com/DataDog/dd-trace-js/pull/8426)** —
keeping trace/span IDs unique across AWS Lambda MicroVM snapshot clones.

## 1. Purpose

A Lambda MicroVM resumes many instances from **one Firecracker memory snapshot**
taken at image-build time. dd-trace's `id.js` pre-fills an 8192-entry buffer with
random bytes and walks it with a counter — both live in process heap, so they are
**frozen into the snapshot**. Every clone then replays the **same** trace/span IDs
until the buffer wraps, colliding across instances.

This harness answers two questions, on real MicroVMs:

1. **Correctness** — does each ID-generation strategy produce *distinct* IDs across
   two clones of one snapshot? It samples three strategies side by side:
   - `batched` — dd-trace's current default (the bug; positive control, must collide).
   - `perCallOpenSSL` — per-call `crypto.randomFillSync` (Node's bundled OpenSSL DRBG).
   - `perCallKernel` — per-call read from `/dev/urandom` (the kernel CSPRNG).
   Each response also returns `buildSamples` (values frozen *at* the snapshot) as a
   per-snapshot fingerprint: the cross-clone comparison is only valid when those
   match across clones (i.e. the clones really share one snapshot).
2. **Performance** — how fast is each strategy *inside* the MicroVM (`/bench`)?

The app is dd-trace-independent: it exercises the underlying Node/OpenSSL/kernel
primitives that `id.js` depends on.

### Endpoints (app on `:8080`, lifecycle hooks on `:9000`)

| Route | Purpose |
|---|---|
| `GET /ids` | one sample of `buildSamples` + `runSamples` for the three strategies |
| `GET /bench?iterations=N&trials=T` | in-MicroVM ns/ID for the three strategies |
| `GET /health` | liveness |
| `POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}` | lifecycle hooks (no reseed — we measure raw behavior) |

## 2. Deploy & run (copy-paste)

Prereqs: `awsv` (AWS CLI v2 wrapper), `jq`, the parent `deploy-microvm.sh` + service model.
Replace the `DD_API_KEY` placeholder with your own key — it is baked into the image by
`deploy-microvm.sh` but **not used by this test**.

```bash
# --- config ---
export DD_API_KEY=<your-dd-api-key>        # placeholder — do not commit a real key
export S3_BUCKET=microvm-rng-test
export REGION=us-east-2

# --- build + deploy (run from lambda-microvm-poc/) ---
( cd rng-test && make build )              # fast pre-deploy gate: node --check
./deploy-microvm.sh rng-test               # builds image + launches clone A; prints image ARN + token file

# --- (a) correctness: launch clone B from the SAME image, capture both, compare ---
rng-test/clone-b.sh                        # auto-pairs with the newest rng-test token file

# --- (b) performance: profile the strategies inside the MicroVM ---
source ./.microvm-token.rng-test-<ts>      # from the deploy output (MICROVM_ID / _ENDPOINT)
TOK=$(awsv aws lambda-microvms create-microvm-auth-token --region "$REGION" \
  --microvm-identifier "$MICROVM_ID" --expiration-in-minutes 30 \
  --allowed-ports '[{"port":8080}]' --query 'authToken."X-aws-proxy-auth"' --output text)
curl -s "https://$MICROVM_ENDPOINT/bench?iterations=5000000&trials=7" \
  -H "X-aws-proxy-auth: $TOK" -H "X-aws-proxy-port: 8080" | jq .nsPerId
#   re-run the curl 2-3x; if the VM had idled into SUSPENDED, discard the first
#   (resume) result. No redeploy needed to re-bench — only if server.js changes.
```

### Cleanup (avoid idle cost)

```bash
# terminate any running rng-test clone(s), then delete the image(s)
awsv aws lambda-microvms terminate-microvm  --region "$REGION" --microvm-identifier <microvm-id>
awsv aws lambda-microvms delete-microvm-image --region "$REGION" \
  --image-identifier arn:aws:lambda:$REGION:<account>:microvm-image:rng-test-<ts>
```

## 3. Sample results

### Correctness — two clones of one snapshot (`compare.sh` output)

```
== buildSamples (frozen at snapshot) -- shared-snapshot precondition ==
  batched         IDENTICAL
  perCallOpenSSL  IDENTICAL
  perCallKernel   IDENTICAL          # clones really share one snapshot -> comparison valid

== runSamples (generated post-resume) -- behavior under test ==
  batched         IDENTICAL          # the bug: frozen buffer replays the same IDs
  perCallOpenSSL  DIFFER             # reseeds on resume (Lambda base image)
  perCallKernel   DIFFER             # reseeds per clone, base-image-independent
```

`batched: IDENTICAL` confirms the collision is real; both per-call strategies produce
unique IDs per clone.

### Performance — `GET /bench` inside the MicroVM (Node 18.20.8, Firecracker arm64)

Three independent runs at `iterations=5000000, trials=7` — variance < 0.5%:

| strategy | run 1 | run 2 | run 3 | ns/ID |
|---|---|---|---|---|
| `batched` (default, non-MicroVM) | 310.09 | 311.05 | 311.45 | **~311** |
| `perCallOpenSSL` (`randomFillSync`) | 1544.71 | 1540.10 | 1543.30 | **~1543** |
| `perCallKernel` (`/dev/urandom`) | 851.64 | 852.08 | 851.78 | **~852** |

## Why reading from the kernel beats calling OpenSSL

Both per-call strategies fix the collision, but `perCallKernel` (`/dev/urandom`) is the
better choice on **both** axes:

1. **Correctness is unconditional.** The fix requires that each post-resume ID depends on
   entropy the platform refreshes per clone. The **kernel CSPRNG is reseeded on every
   snapshot resume (VMGenID) regardless of the base image**. `randomFillSync` draws from
   Node's *bundled* OpenSSL DRBG; that DRBG only picks up the reseed on the **Lambda-managed
   base image** (whose OpenSSL is patched to reseed on resume). On a custom base image,
   per-call `randomFillSync` can keep replaying the frozen DRBG state — i.e. the bug returns.
   Reading the kernel removes that dependency.

2. **It is faster in the MicroVM.** Counter-intuitively (a kernel read is a syscall, OpenSSL
   is userspace), on AL2023's Node 18 the OpenSSL per-call path carries ~1543 ns of overhead
   per 8-byte draw, while a `/dev/urandom` read is ~852 ns — **~45% faster**. The batched
   default (~311 ns) is faster still but is the unsafe path, so it is kept only outside
   MicroVMs; the per-call cost is paid **only** in MicroVM mode.

   (The relative speed is Node-version dependent — newer Node may narrow the gap — so the
   *durable* reason for the kernel source is base-image-independent correctness; the speed
   win is a bonus on the measured runtime.)

dd-trace therefore uses `/dev/urandom` in MicroVM mode, falling back to `randomFillSync`
only if `/dev/urandom` cannot be opened (non-Linux / locked-down sandbox).
