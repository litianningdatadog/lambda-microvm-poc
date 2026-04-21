# MicroVM Lifecycle Sidecar — Design Spec

**Date:** 2026-04-09  
**Status:** Approved  
**Scope:** Standalone Rust binary that handles all 5 Lambda MicroVM lifecycle hooks and emits DataDog events, metrics, and logs on each transition. Designed to be extensible to buffered/batched system-level observability (CPU, memory, network, FD counts, OOM events).

---

## Problem

Lambda MicroVM requires applications to implement 5 lifecycle hook endpoints on port 9000. This is a non-trivial adoption cost:

- Developers must run a second HTTP server alongside their business app
- They must implement 5 specific paths at a fixed base URL
- They must handle snapshot-safety concerns (e.g. re-seeding unique IDs on `launch`)
- There is no built-in observability for lifecycle transitions

This spec defines a language-agnostic sidecar that removes all of this from the user's app.

---

## Goals

1. Handle all 5 lifecycle hooks so user apps do not have to
2. Emit DataDog events, metrics, and logs on each lifecycle transition
3. Be a single static ARM64 binary — zero runtime dependency, drop into any image
4. Be configurable via a YAML file with env var overrides
5. Ship interchangeable launch templates (shell script, supervisord, Dockerfile snippet)

## Non-Goals

- Calling back into the user's app on lifecycle events (no outbound webhook to user app)
- Supporting architectures other than ARM64 (Lambda MicroVM preview constraint)
- Retrying failed DataDog calls — DD emission is best-effort; a DD failure must never block a hook response (except the bounded pre-flight flush on `suspend` and `terminate`, which has an explicit rationale below)

---

## Architecture

The sidecar has two independent observability paths with different delivery guarantees:

- **Immediate path** — lifecycle hook events. Sent directly to DataDog as each hook fires, with no buffering. Tied to critical VM state transitions; any delay risks losing events on suspend/terminate.
- **Buffered path** — platform observability (system metrics, OOM events). Polled on a schedule, accumulated in a bounded `mpsc` channel, and flushed in batches. High-frequency signals that benefit from DataDog's native batch APIs (`/api/v2/series` accepts up to 500 series; `/api/v2/logs` accepts up to 1000 entries per request).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  MicroVM Container                                                           │
│                                                                              │
│  ┌──────────────┐   ┌──────────────────────────────────────────────────┐    │
│  │  User App     │   │  microvm-sidecar (static Rust binary)            │    │
│  │  any language │   │                                                  │    │
│  │  port 8080    │   │  ┌─────────────────────────────────────────┐    │    │
│  └──────────────┘   │  │  axum HTTP server  (port 9000)           │    │    │
│                      │  └────────────────────┬────────────────────┘    │    │
│  ┌──────────────┐   │                        │ POST /ready             │    │
│  │  Lambda       │──►│                        │ POST /launch            │    │
│  │  MicroVM      │   │                        │ POST /resume            │    │
│  │  Platform     │   │                        │ POST /suspend           │    │
│  └──────────────┘   │                        │ POST /terminate         │    │
│                      │  ┌─────────────────────▼────────────────────┐   │    │
│                      │  │  LifecycleEmitter                         │   │    │
│                      │  │  (IMMEDIATE PATH — no buffer)             │   │    │
│                      │  │  tokio::spawn × 3 per hook                │   │    │
│                      │  │  JoinHandles tracked for flush            │   │    │
│                      │  └─────────────────────┬────────────────────┘   │    │
│                      │                         │                        │    │
│                      │  ┌──────────────────────│────────────────────┐  │    │
│                      │  │  tokio scheduler      │                    │  │    │
│                      │  │  (background task)    │                    │  │    │
│                      │  │  ┌────────────────┐   │                    │  │    │
│                      │  │  │ CpuCollector   │   │                    │  │    │
│                      │  │  │ MemCollector   ├───┤  mpsc::send        │  │    │
│                      │  │  │ NetCollector   │   │  (BUFFERED PATH)   │  │    │
│                      │  │  │ FdCollector    │   │                    │  │    │
│                      │  │  │ OomCollector   │   │                    │  │    │
│                      │  │  └───────┬────────┘   │                    │  │    │
│                      │  └──────────│────────────│────────────────────┘  │    │
│                      │             │            │                        │    │
│                      │             ▼            │                        │    │
│                      │  ┌──────────────────┐    │                        │    │
│                      │  │  Signal Buffer    │◄───┘                       │    │
│                      │  │  mpsc channel     │                            │    │
│                      │  │  (bounded cap)    │                            │    │
│                      │  └────────┬─────────┘                            │    │
│                      │           │                                       │    │
│                      │  ┌────────▼─────────┐                            │    │
│                      │  │  FlushTask        │                            │    │
│                      │  │  batch_size ≥ N   │                            │    │
│                      │  │  OR elapsed ≥ T   │                            │    │
│                      │  └────────┬─────────┘                            │    │
│                      │           │ (batched)                             │    │
│                      │           ▼                                       │    │
│                      │  ┌─────────────────────────────────────────┐     │    │
│                      │  │  shared reqwest::Client  (AppState)      │◄────┘    │
│                      │  └─────────────────────────────────────────┘          │
│                      └──────────────────────────────────────────────────┐    │
└─────────────────────────────────────────────────────────────────────────│────┘
                                                                           │
                          ┌────────────────────────────────────────────────▼──┐
                          │  DataDog Backend                                    │
                          │                                                     │
                          │  IMMEDIATE (per hook, unbatched):                  │
                          │    POST /api/v1/events   — lifecycle events         │
                          │    POST /api/v2/series   — lifecycle counters       │
                          │    POST /api/v2/logs     — lifecycle log lines      │
                          │                                                     │
                          │  BUFFERED (batched, up to 500/1000 per request):   │
                          │    POST /api/v2/series   — system metrics           │
                          │    POST /api/v2/logs     — system events (OOM etc) │
                          └─────────────────────────────────────────────────────┘
```

The sidecar and user app are **fully decoupled**: no IPC, no shared state, no calls between them.

---

## Dataflow

### Path 1 — Immediate (lifecycle hooks)

```
Platform POSTs /launch (or any hook)
        │
        ▼
axum handler
        │
        ├── parse payload → update MicroVmState
        ├── log locally (tracing → stdout)
        │
        ├── tokio::spawn → POST /api/v1/events  ──► DataDog  (≤5s timeout)
        ├── tokio::spawn → POST /api/v2/series  ──► DataDog  (≤5s timeout)
        ├── tokio::spawn → POST /api/v2/logs    ──► DataDog  (≤5s timeout)
        │         (all 3 concurrent; JoinHandles appended to task_handles)
        │
        ├── [suspend/terminate only]
        │     drain task_handles → join_all(timeout=10s) → return 200
        │
        └── return 200 OK  (empty body)
```

Signals flow from hook receipt to DataDog API calls in a single hop, with no intermediate queue. The only "hold" is the pre-flight flush on `suspend`/`terminate`, which is bounded at 10s.

### Path 2 — Buffered (platform observability)

```
tokio scheduler (background task, started at sidecar boot)
        │
        ├── for each registered Collector:
        │     sleep(collector.interval())
        │     signals: Vec<DdSignal> = collector.collect()
        │     mpsc::Sender::send(signals)  ──► Signal Buffer (bounded channel)
        │                                       └── if full: drop + log warning
        │
Signal Buffer (mpsc::Receiver end, consumed by FlushTask)
        │
FlushTask (separate background tokio task)
        │
        ├── accumulate into:  batch_metrics: Vec<DdMetric>
        │                     batch_logs:    Vec<DdLog>
        │
        ├── flush trigger: batch_size ≥ MAX_BATCH_SIZE
        │              OR  elapsed    ≥ FLUSH_INTERVAL
        │
        └── on flush:
              POST /api/v2/series  { series: [batch_metrics] }  ──► DataDog
              POST /api/v2/logs    [ batch_logs ]                ──► DataDog
              clear batch buffers, reset elapsed timer
```

System metrics do not generate DD Events (DD Events are for notable occurrences). OOM events are an exception — they are emitted as DD Events on the **immediate path** despite being sourced from a collector, because OOM is a notable occurrence that should not be delayed by the flush interval.

### Suspend/terminate interaction with the buffered path

On `suspend` and `terminate`, the FlushTask is signalled to perform a final flush before the hook returns:

```
suspend hook received
        │
        ├── LifecycleEmitter: spawn DD tasks + join_all(10s)
        │
        ├── signal FlushTask via oneshot channel: "flush now"
        │     FlushTask drains buffer, sends final batch (≤10s)
        │
        └── return 200 OK
```

This ensures no buffered system metrics are lost when the VM is snapshotted or destroyed.

---

## Configuration

`microvm-sidecar.yaml` (checked into repo, non-secret defaults):

```yaml
sidecar:
  port: 9000                        # env: SIDECAR_PORT

datadog:
  api_key: ""                       # env: DD_API_KEY (required, never commit)
  site: "datadoghq.com"             # env: DD_SITE
  service: "my-microvm-app"         # env: DD_SERVICE
  env: "production"                 # env: DD_ENV
  tags:                             # env: DD_TAGS (comma-separated, replaces list)
    - "team:platform"
    - "region:us-east-2"

collectors:
  buffer:
    channel_capacity: 10000         # env: COLLECTORS_CHANNEL_CAPACITY
                                    # mpsc channel size; signals dropped when full
    max_batch_size: 500             # env: COLLECTORS_MAX_BATCH_SIZE
                                    # flush when this many signals accumulated
    flush_interval_seconds: 10      # env: COLLECTORS_FLUSH_INTERVAL_SECONDS
                                    # flush at minimum every N seconds regardless of batch size

  system_metrics:                   # gated by `system-metrics` cargo feature
    enabled: false                  # env: COLLECTORS_SYSTEM_METRICS_ENABLED
    interval_seconds: 30            # env: COLLECTORS_SYSTEM_METRICS_INTERVAL
    cpu: true                       # /proc/stat
    memory: true                    # /proc/meminfo
    network: true                   # /proc/net/dev
    file_descriptors: true          # /proc/<pid>/fd count
    oom_events: true                # /proc/kmsg tail (OOM killer messages → immediate DD Event)
```

**Config file resolution order:** the sidecar searches for the config file in this order:
1. Path from `SIDECAR_CONFIG` env var (if set)
2. `/etc/microvm-sidecar.yaml`
3. `./microvm-sidecar.yaml` (CWD)
4. Built-in defaults (all fields at their zero/default values)

**Env var override order:** after loading the YAML, each env var is checked via `std::env::var` and applied over the corresponding struct field. `DD_API_KEY` is always required at runtime — if absent, DD calls are skipped and a startup warning is logged. **Note:** this warning fires at sidecar startup (before any hook is received), not when `ready` is called. It appears in build-time CloudWatch logs and is **not** embedded in the Firecracker snapshot — the snapshot captures process memory state, not stdout log lines.

**DD API base URL:** all DataDog API calls use `https://api.{DD_SITE}/...` as their base. For example, with `DD_SITE=datadoghq.eu` the Events endpoint becomes `https://api.datadoghq.eu/api/v1/events`. The `DD_SITE` value is substituted at request time, not at startup.

**`DD_TAGS` merge semantics:** `DD_TAGS` **replaces** (not appends to) the `tags` list from the YAML file when set. This avoids ambiguous deduplication logic. If you need both YAML tags and env-var tags, set the full combined list in `DD_TAGS`.

Auto-injected tags (merged with the resolved `tags` list on every signal):
- `hook:<name>` — which hook fired
- `microvm_id:<id>` — populated after `launch` from the received payload; **omitted entirely** on hooks where it is not yet known (e.g. `ready`) to avoid empty-cardinality noise in DataDog
- `hook_result:success|error`

**Note on `DD_API_KEY` delivery at runtime:** inject as an environment variable at `launch-micro-vm` time. Never embed in the config file or Dockerfile.

---

## State: `microvm_id` persistence across hooks

The `launch` hook is the only hook that receives `microvm_id` (and `meshIpv6Address`) in its JSON payload. All subsequent hooks (`resume`, `suspend`, `terminate`) receive no payload. The sidecar must carry `microvm_id` forward for use in DD signal tags.

The sidecar maintains shared axum state containing MicroVM identity, the DD HTTP client, and the channel sender for the buffered path:

```rust
struct AppState {
    micro_vm:     Arc<Mutex<Option<MicroVmState>>>,
    dd_client:    Arc<Mutex<reqwest::Client>>,
    task_handles: Arc<Mutex<Vec<JoinHandle<()>>>>,
    signal_tx:    mpsc::Sender<Vec<DdSignal>>,   // buffered path: system collectors
    flush_tx:     oneshot::Sender<()>,            // signals FlushTask to flush immediately
    config:       Config,
}

struct MicroVmState {
    micro_vm_id:       String,
    mesh_ipv6_address: Option<String>,
}
```

- On `launch`: extract payload, store in `micro_vm`, emit DD signals with `microvm_id` tag
- On all subsequent hooks: read from `micro_vm`; include `microvm_id` tag if present; omit if absent
- On `resume`: replace `dd_client` with a freshly constructed `reqwest::Client` before emitting DD signals (see Special cases — `/resume`)
- On `suspend`/`terminate`: send on `flush_tx` to trigger an immediate FlushTask drain before returning 200

`dd_client` is behind a `Mutex` (not `Arc<reqwest::Client>`) to allow replacement on resume without rebuilding the entire AppState. `signal_tx` is used exclusively by system collectors; lifecycle hook handlers bypass it entirely.

`mesh_ipv6_address` is included in **log payloads only**. It is intentionally excluded from Events and Metrics tags to avoid high-cardinality tag explosion in DataDog (one unique IPv6 address per MicroVM instance).

---

## In-flight task tracking

Task handles are stored in `AppState.task_handles` (defined in the State section above). Every `tokio::spawn` for a DD signal on the **immediate path** pushes its `JoinHandle` into this vec. The buffered path's FlushTask is a long-lived background task managed separately and is not tracked here.

On `suspend` and `terminate`: before returning 200, the handler first spawns its own DD tasks (pushing handles into the vec), then drains the entire vec and `tokio::time::timeout`s a `join_all` at 10s. This means the flush covers both tasks from prior hooks and the current hook's own tasks in one pass.

Tasks that do not complete within the 10s window are dropped (their `JoinHandle`s are abandoned). **Abandoned tasks keep running** in the tokio runtime until the process exits — on `suspend` this means they are frozen mid-flight by the Firecracker snapshot. On VM resume from that snapshot, those half-completed tokio tasks will be in an invalid state (stale TCP connections, corrupted `reqwest` state). The `resume` hook's `reqwest::Client` recreation (see below) addresses this by discarding the old client entirely rather than reusing connection pools from before the snapshot.

The three DD signal tasks per hook (events, metrics, logs) are spawned **concurrently** — all three `tokio::spawn` calls fire before any awaiting — so the worst-case flush duration is one stalled 5s call, not three sequential 5s calls (not 15s). The 10s flush window therefore provides 2x headroom over the single-task worst case while staying well within the platform's 120s suspend budget.

---

## DataDog Signals

For each lifecycle hook, all three signal types are emitted concurrently via `tokio::spawn` (handles stored for flush as described above).

### Events (`POST https://api.{DD_SITE}/api/v1/events`)

> **Note:** DataDog's Events API remains at v1. A v2 events endpoint exists but uses a different schema. The sidecar uses v1 intentionally; Metrics and Logs use v2 per their current stable endpoints.

```
Title: "MicroVM <hook>"
Text:  "MicroVM <microvm_id> transitioned: <hook>"
Tags:  [hook:<hook>, microvm_id:<id>, env:<env>, service:<service>, ...]
```

### Metrics (`POST https://api.{DD_SITE}/api/v2/series`)

Counter incremented by 1 per hook invocation:
```
microvm.hook.ready
microvm.hook.launch
microvm.hook.resume
microvm.hook.suspend
microvm.hook.terminate
```
Tags carry `microvm_id`, `env`, `service` for per-instance aggregation.

### Logs (`POST https://api.{DD_SITE}/api/v2/logs`)

Structured JSON:
```json
{
  "timestamp": "2026-04-09T14:32:01Z",
  "hook": "launch",
  "microvm_id": "ai-a2ceb494",
  "mesh_ipv6": "fd00::1",
  "service": "my-microvm-app",
  "env": "production",
  "status": "ok"
}
```

`mesh_ipv6` appears in logs only (not in Events/Metrics tags — see State section).

---

## Hook Handler Logic

Each hook follows this flow:

```
Receive POST
     │
     ├─► Parse payload (launch only: microVmId, meshIpv6Address)
     │
     ├─► Update shared MicroVmState (launch only)
     │
     ├─► Log locally via tracing (structured JSON to stdout)
     │
     ├─► tokio::spawn DD signal tasks (events + metrics + logs in parallel)
     │    └─► Store JoinHandles in shared handle vec
     │    └─► On DD error: log warning; does NOT affect hook response
     │
     ├─► [suspend / terminate only] Drain handle vec + await with 10s timeout
     │
     └─► Return 200 OK (empty body)
```

**Response body:** all hooks return an empty body with HTTP 200. Per the `lifecycle_hooks_openapi.json` spec, the platform defines a `200` response with no required body schema — an empty response is valid and accepted.

### Timeouts

| Hook | Platform timeout | Own DD call timeout | Pre-flight flush timeout |
|------|-----------------|---------------------|--------------------------|
| ready | 60m | 5s | — |
| launch | 60m | 5s | — |
| resume | 120s | 5s | — |
| suspend | 120s | 5s | 10s (awaits prior + own tasks) |
| terminate | 60s | 5s | 10s (awaits prior + own tasks) |

"Own DD call timeout" is the `reqwest` timeout applied to each individual DD HTTP call. "Pre-flight flush timeout" is the `join_all` timeout applied to the full handle vec drain on `suspend`/`terminate`.

### Special cases

**`/ready`:** called once by the platform after the container starts during the `create-micro-vm-image` workflow — the platform POSTs to this endpoint to signal that the running application has completed startup and may be snapshotted. It is a live HTTP call against a running container, not a Dockerfile `RUN` step. `microvm_id` is not yet available; the tag is omitted from all DD signals. After `ready` returns 200, the platform takes the Firecracker snapshot.

**`/resume`:** called after a VM is restored from a Firecracker snapshot (in-place resume, not launch-from-snapshot). The `reqwest::Client` held in shared state **must be recreated** on resume — the connection pool captured in the snapshot contains stale TCP connections that are invalid after memory restore.

On resume, the handler:
1. Calls `JoinHandle::abort()` on all handles remaining in `task_handles` (abandoned tasks from before suspend), then clears the vec. This drops their `Arc<reqwest::Client>` references immediately, avoiding unbounded memory accumulation across suspend/resume cycles.
2. Replaces `dd_client` with a freshly constructed `reqwest::Client`.
3. Emits DD signals using the new client.

Without step 1, old client Arcs linger in memory for each suspend/resume cycle — a bounded but growing leak on long-lived VMs.

**`/suspend` pre-flight flush:** before returning 200, drains and awaits all in-flight DD `JoinHandle`s (including the ones spawned by the current suspend handler) with a 10s cap. **Rationale:** when the platform snapshots VM memory immediately after the suspend hook returns, any open TCP connections to the DD API are captured mid-state. On VM resume, those connections are invalid and may cause the DD client to emit errors or stall. Flushing before suspend eliminates this. This is the only exception to the fire-and-forget principle, and it is bounded to prevent blocking the platform's 120s suspend budget.

**Concurrency assumption:** the Lambda MicroVM platform serializes lifecycle hook calls — it does not invoke two hooks concurrently. The sidecar relies on this assumption for the drain-then-await correctness of the flush: no new handles will be pushed into the vec between the drain and the `join_all`. If the platform ever sends concurrent hooks, this assumption breaks silently; the spec documents it here so future implementers can add a guard if needed.

**Panic note for suspend:** if a panic occurs inside the suspend handler, axum returns 200 (see Panic handling below), which tells the platform to proceed with snapshotting a VM whose sidecar may be in an inconsistent state. This tradeoff is accepted: the sidecar's internal failures must not prevent the user's VM from suspending. In practice, sidecar panics during suspend are observable via CloudWatch logs.

**`/terminate`:** drains and awaits all in-flight DD `JoinHandle`s (10s cap) before returning 200. Rationale for `terminate` is distinct from `suspend`: the VM is being destroyed (not snapshotted), so snapshot-safety is not the concern. The flush here is solely to ensure observability data is transmitted before the VM exits. The 60s platform timeout provides headroom.

**Panic handling:** the axum error layer is configured to return **200** on unhandled panics (not 500). Rationale: a non-200 from a lifecycle hook signals hook failure to the Lambda MicroVM platform and may abort the lifecycle transition. The sidecar's own bugs must not disrupt the user's VM lifecycle. Panics are logged to stdout (visible in CloudWatch under `/aws/lambda/microvms/<image-name>`).

---

## Collector Abstraction

The `Collector` trait is for **polled** observability sources only (system metrics). Lifecycle hooks have their own path and do not implement this trait.

```rust
#[async_trait]
trait Collector: Send + Sync {
    fn name(&self) -> &'static str;
    fn interval(&self) -> Duration;
    async fn collect(&self, micro_vm: &Option<MicroVmState>) -> Vec<DdSignal>;
}
```

### DdSignal enum (shared between both paths)

```rust
enum DdSignal {
    Event(DdEvent),   // notable occurrence → /api/v1/events (immediate for OOM)
    Metric(DdMetric), // counter/gauge      → /api/v2/series (batched for system)
    Log(DdLog),       // structured line    → /api/v2/logs   (batched for system)
}
```

### Routing rules by signal type and source

| Source | Signal type | Delivery |
|--------|-------------|----------|
| Lifecycle hook | Event | Immediate (tokio::spawn) |
| Lifecycle hook | Metric | Immediate (tokio::spawn) |
| Lifecycle hook | Log | Immediate (tokio::spawn) |
| System collector | Metric | Buffered (mpsc → FlushTask) |
| System collector | Log | Buffered (mpsc → FlushTask) |
| OOM collector | Event | Immediate (tokio::spawn, bypasses buffer) |

OOM events bypass the buffer because they are notable occurrences that should appear in the DD Events stream immediately, not delayed by the flush interval.

### Built-in collectors (all gated by `system-metrics` feature flag)

| Collector | Source | Signal types | DD metric names |
|-----------|--------|--------------|-----------------|
| `CpuCollector` | `/proc/stat` | Metric | `microvm.cpu.usage_pct` |
| `MemCollector` | `/proc/meminfo` | Metric | `microvm.mem.used_bytes`, `microvm.mem.available_bytes` |
| `NetCollector` | `/proc/net/dev` | Metric | `microvm.net.rx_bytes`, `microvm.net.tx_bytes` |
| `FdCollector` | `/proc/<pid>/fd` | Metric | `microvm.fd.count` |
| `OomCollector` | `/proc/kmsg` tail | Event | (DD Event title: "OOM killer fired") |

All metrics carry the standard tag set: `microvm_id`, `env`, `service`, plus `collector:<name>`.

---

## Project Structure

```
microvm-sidecar/
├── Cargo.toml                        # features: system-metrics (default = off)
├── src/
│   ├── main.rs                       # entrypoint: config, AppState, spawn scheduler+FlushTask, axum router
│   ├── hooks/
│   │   ├── mod.rs                    # axum handlers for all 5 hooks (immediate path)
│   │   └── payload.rs                # LaunchRequest struct + serde deserialization
│   ├── emitter.rs                    # LifecycleEmitter: builds DdSignals, spawns DD tasks
│   ├── collector/
│   │   ├── mod.rs                    # Collector trait + tokio scheduler loop
│   │   └── system/                   # gated by `system-metrics` feature
│   │       ├── mod.rs                # registers all system collectors
│   │       ├── cpu.rs                # /proc/stat
│   │       ├── memory.rs             # /proc/meminfo
│   │       ├── network.rs            # /proc/net/dev
│   │       ├── fd.rs                 # /proc/<pid>/fd count
│   │       └── oom.rs                # /proc/kmsg tail → immediate DD Event
│   ├── flush.rs                      # FlushTask: mpsc receiver, batch accumulator, DD dispatch
│   ├── datadog/
│   │   ├── mod.rs                    # DD API calls: send_immediate, send_batch
│   │   └── signals.rs                # DdSignal enum, DdEvent/DdMetric/DdLog builders
│   └── config/
│       └── mod.rs                    # serde_yml loading + std::env::var overrides
├── microvm-sidecar.yaml              # default config template
├── Makefile
└── deploy-templates/
    ├── start.sh
    ├── supervisord.conf
    └── Dockerfile.snippet
```

### Key crates

| Concern | Crate | Notes |
|---------|-------|-------|
| HTTP server | `axum` | tokio-native, typed extractors |
| Async runtime | `tokio` | full feature set: mpsc, oneshot, time, spawn |
| HTTP client (DD) | `reqwest` | async, TLS built-in |
| Async trait | `async-trait` | required for `Collector` trait with async fn |
| Config / YAML | `serde` + `serde_yml` | `serde_yaml` is deprecated upstream; use maintained fork `serde_yml` |
| Env var overrides | `std::env::var` | per-field manual override; `envy` does not support nested struct mapping |
| Structured logging | `tracing` + `tracing-subscriber` | JSON format to stdout |

---

## Build

```makefile
TARGET=aarch64-unknown-linux-musl

# build-docker: cross-compiles for ARM64 musl using the `cross` tool.
# Requires: cargo install cross, and Docker running.
# This is the recommended build path for developers on x86 Linux or Mac.
build-docker:
	cargo install cross --quiet
	cross build --release --target $(TARGET)
	cp target/$(TARGET)/release/microvm-sidecar .

# build: native ARM64 musl build. Only works on an ARM64 Linux host with
# the musl toolchain installed (e.g. `apt install musl-tools` on Ubuntu,
# or `brew install FiloSottile/musl-cross/musl-cross` on macOS).
build:
	rustup target add $(TARGET)
	cargo build --release --target $(TARGET)
	cp target/$(TARGET)/release/microvm-sidecar .

# build-local: debug build for the host architecture (no ARM64/musl).
# Use for fast iteration during development; not suitable for deployment.
build-local:
	cargo build --release
```

The release binary is statically linked (musl libc), has no runtime dependencies, and can be `COPY`ed into any base image including `scratch`. **`build-docker` is the recommended target** for most developers since `cross` handles the ARM64 musl toolchain inside Docker automatically.

---

## Launch Templates

All three templates are interchangeable — the sidecar binary itself is identical in all cases.

### Option A — Shell script (simplest)

```sh
#!/bin/sh
# DD_API_KEY and other secrets must be injected as env vars at container launch time
# (e.g. via --execution-role-arn or `docker run -e DD_API_KEY=...`)
# Do NOT hardcode secrets here.
/usr/local/bin/microvm-sidecar &
SIDECAR_PID=$!

# Wait for the sidecar's port to be ready before exec-ing the user app.
# This prevents a race where the platform sends /ready before the sidecar
# has bound its TCP socket.
for i in $(seq 1 50); do
  nc -z localhost 9000 2>/dev/null && break
  sleep 0.1
done

exec "$@"
```

```dockerfile
COPY microvm-sidecar /usr/local/bin/microvm-sidecar
COPY microvm-sidecar.yaml /etc/microvm-sidecar.yaml
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh", "python", "app.py"]
```

`exec "$@"` makes the user's app PID 1 — SIGTERM is delivered correctly on container shutdown.

### Option B — supervisord (robust, auto-restart)

```ini
[program:sidecar]
command=/usr/local/bin/microvm-sidecar
autostart=true
autorestart=true
stdout_logfile=/dev/stdout

[program:app]
command=python app.py
autostart=true
stdout_logfile=/dev/stdout
```

Adds ~5MB to the image. Use when crash recovery of the sidecar is required.

**Port-readiness note:** supervisord starts both programs in parallel and has no native port-readiness mechanism (`startretries` retries on process crash, not on socket bind). To avoid the same race as Option A, configure the app program with a wrapper that polls the sidecar port before starting:

```ini
[program:app]
command=/bin/sh -c 'until nc -z localhost 9000; do sleep 0.1; done; exec python app.py'
autostart=true
stdout_logfile=/dev/stdout
```

### Option C — User-managed

```dockerfile
COPY microvm-sidecar /usr/local/bin/microvm-sidecar
COPY microvm-sidecar.yaml /etc/microvm-sidecar.yaml
# User launches the sidecar however they want
```

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| DD API unreachable | Log warning, return 200 to platform |
| DD API returns 4xx/5xx | Log warning with status code, return 200 |
| Config file missing (all search paths) | Start with built-in defaults; warn if `DD_API_KEY` unset |
| `DD_API_KEY` unset | Log warning on startup; DD calls skipped; hooks still return 200 |
| Flush timeout on `suspend`/`terminate` | Log warning that flush was incomplete; return 200 |
| Sidecar panic | axum catches, logs to stdout (CloudWatch visible), returns **200** to platform |
