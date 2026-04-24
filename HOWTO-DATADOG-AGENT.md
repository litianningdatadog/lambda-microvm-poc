# Integrating the full Datadog Agent with Lambda MicroVM

Companion doc to `sample-flask-app-datadog-agent-poc/`. Describes the
design pattern, the files that make it work, the non-standard adapters
it relies on, and how to retarget it to user apps in other languages.

## What this integration delivers

**Goal:** rich Datadog observability (APM, logs, DogStatsD, host-infra
metrics, lifecycle-event markers) inside a Firecracker MicroVM, with
**zero code change required of the user application.**

**Non-goals:** serverless-init, serverless-compat, and any sidecar
binary are explicitly out of scope. The only Datadog component used is
the full `datadog-agent` RPM.

**What reaches Datadog once the MicroVM is running:**

| Signal | How | Notes |
|--------|-----|-------|
| APM traces | `ddtrace-run` wraps the user app's start command at supervisord level | Zero user code change for most languages (Python, Node, Java, Ruby, .NET). Go is the exception — it requires a source-level import. |
| Logs | supervisord writes user-app stdout/stderr to `/var/log/app/app.log`; agent tails the file via a `conf.d` integration | Tag: `service:user-app` / `source:python` (language-dependent) |
| DogStatsD metrics + events | Hook-server emits lifecycle events on port 8125; user app can emit custom metrics if it wants | Hook-server use is automatic; user use is opt-in |
| Host-infra (CPU/mem/disk) | Agent's core checks, autostart | Arguably noisy for ≤8h VMs — can be disabled in `datadog.yaml.template` |
| Hostname = microVmId | Agent started at `/launch` time with `DD_HOSTNAME` set to the platform-supplied microVmId | Every MicroVM shows up as its own host in Datadog's infra map |

## Solution architecture

Everything lives in a single container image. Three layers conceptually:

```
┌──────────────────────────────────────────────────────┐
│  PLATFORM LAYER                                      │
│  (would be a prebuilt base image in production)     │
│                                                      │
│    datadog-agent  (RPM, ARM64)                       │
│    supervisord    (PID 1)                            │
│    hook_server.py — owns port 9000, bootstraps the   │
│                     agent at /launch, emits DD       │
│                     lifecycle events                 │
│    datadog.yaml.template                             │
│    user-app-logs.yaml                                │
│    entrypoint.sh                                     │
└──────────────────────────────────────────────────────┘
                        ▲
                        │ (in a real deploy: FROM microvm-dd-base)
                        │
┌──────────────────────────────────────────────────────┐
│  USER-APP LAYER                                      │
│                                                      │
│    requirements.txt  — user's runtime deps           │
│    app.py            — user's code; ZERO DD imports  │
└──────────────────────────────────────────────────────┘
                        ▲
                        │ zip + upload
                        │
┌──────────────────────────────────────────────────────┐
│  DEPLOY LAYER                                        │
│                                                      │
│    deploy-microvm.sh (at repo root) — reads          │
│      DD_API_KEY from shell, serializes into          │
│      .dd-env inside the zip, uploads, creates        │
│      MicroVM image, launches, prints auth token      │
└──────────────────────────────────────────────────────┘
```

### Runtime PID tree inside a launched MicroVM

```
PID 1  supervisord
├── user-app          autostart=true
│     command:  ddtrace-run python3.11 /app/app.py
│     stdout/stderr → /var/log/app/app.log
│                      │
│                      └──(tailed by) datadog-agent's user-app.d integration
│
├── hook-server       autostart=true
│     command:  python3.11 /opt/platform/hook_server.py
│     On /ready:    200 once supervisord reports user-app as RUNNING
│     On /launch:   render datadog.yaml with DD_HOSTNAME=microVmId →
│                   supervisorctl start datadog-agent → emit DD event
│     On /terminate: emit event → `datadog-agent stop` (graceful flush)
│
└── datadog-agent     autostart=false     ← deferred start, not baked
      ├── trace-agent  (APM, 8126/tcp)
      └── …             (process/system-probe disabled)
      │
      │  Agent lifecycle across MicroVM states:
      │   /launch     → supervisorctl start (first boot)
      │   /suspend    → datadog-agent stop (flush) → supervisorctl start
      │                 (pre-warm fresh agent so /resume is free)
      │   /resume     → no-op (pre-warmed agent unfreezes with the VM)
      │   /terminate  → datadog-agent stop (final flush)
```

The **keystone** of the design is `autostart=false` on `datadog-agent`.
The Firecracker snapshot is taken after `/ready` returns 200; anything
running at that moment gets its in-memory state (hostname, auth_token,
UUIDs) baked into the snapshot and shared across every clone.
Deferring agent start to `/launch` ensures each clone gets fresh
identity.

The **second keystone** is the flush-and-pre-warm on `/suspend`. Before
returning 200, `/suspend` runs `datadog-agent stop` (which drives the
agent's internal flush path — metric points, log batches, trace
payloads — to DD intake) and then `supervisorctl start` (bringing up a
fresh agent). The platform then freezes a VM that already has a
running, post-flush agent inside it, so `/resume` is a cheap no-op.
This avoids both data loss (if the platform takes the VM from
SUSPENDED → TERMINATING without firing `/terminate`) AND resume-path
latency (the expensive restart work happens on `/suspend`, which is
user-invisible).

## Files introduced

### Platform layer (POC-specific files inside `sample-flask-app-datadog-agent-poc/`)

| File | Purpose | Key content |
|------|---------|-------------|
| `Dockerfile` | Builds the MicroVM image. Single-stage build; platform-section and user-section delimited by comment banners. | FROM AL2023 ARM64; dnf install Python/procps-ng/etc.; configure Datadog yum repo; dnf install datadog-agent; pip install supervisor/flask/datadog/ddtrace; COPY platform files; COPY user-app files; ENTRYPOINT entrypoint.sh |
| `hook_server.py` | Platform-owned process that answers the 5 MicroVM lifecycle hooks on port 9000. | Flask app with `/ready` (gated on `supervisorctl status user-app == RUNNING`), `/launch` (render datadog.yaml + start agent), `/resume`/`/suspend`/`/terminate` (emit DogStatsD events; terminate does graceful `agent stop`) |
| `supervisord.conf` | PID-1 process supervisor. Three programs: `user-app` (autostart), `hook-server` (autostart), `datadog-agent` (autostart=false, autorestart=unexpected, exitcodes=0). | Redirects user-app stdout/stderr to `/var/log/app/app.log`; wraps user app with `ddtrace-run` |
| `datadog.yaml.template` | Config for `/etc/datadog-agent/datadog.yaml`. Rendered at `/launch` time with `${DD_API_KEY}`, `${DD_SITE}`, `${DD_HOSTNAME}` substituted. | Disables process-agent + system-probe (meaningless in MicroVM); sets `hostname_force_config_as_canonical: true`; enables logs + APM + DogStatsD |
| `user-app-logs.yaml` | Agent log integration (deployed to `/etc/datadog-agent/conf.d/user-app.d/conf.yaml`). | `type: file`, path `/var/log/app/app.log`, `service: user-app`, `source: python`, `start_position: end` |
| `entrypoint.sh` | Container entrypoint. Sources `/opt/platform/.dd-env` (if present), wipes any baked-in `auth_token`, renders initial `datadog.yaml` placeholder, execs supervisord. | `exec supervisord -c /etc/supervisord.conf` |

### User-app layer (everything a real user would write)

| File | Purpose | Content |
|------|---------|---------|
| `app.py` | The user's Flask hello-world. **Contains zero DD imports and no lifecycle-hook code.** | Listens on port 8080; `/` returns JSON; `/health` returns status |
| `requirements.txt` | User's Python deps. | `flask>=2.0.0` — nothing DD-related |

### Deploy layer (repo-root tooling)

| File | Purpose | Relevant change for DD integration |
|------|---------|-----------------------------------|
| `deploy-microvm.sh` | One-shot: zip → S3 upload → create MicroVM image → poll CREATED → launch → generate auth token. | Reads `DD_API_KEY` from shell at zip-time; writes a `.dd-env` file into the app dir; included in the zip; cleaned up via EXIT trap. Warns if `DD_API_KEY` is unset. |

### Auto-generated, not checked in

| File | Purpose |
|------|---------|
| `.dd-env` (inside the POC dir, temporarily) | `export DD_API_KEY=…` — produced by `deploy-microvm.sh` at zip time, baked into the image via `COPY .dd-env* /opt/platform/`, sourced by `entrypoint.sh`. Deleted from the host repo after zip + upload (EXIT trap). |

## How each observability requirement is satisfied

| Requirement | Mechanism | Where |
|-------------|-----------|-------|
| **Snapshot only when user app is up** | `/ready` asks supervisord whether the `user-app` program is in `RUNNING` state; returns 200 if yes, 403 otherwise. Language-agnostic. | `hook_server.py: _user_app_ready()` |
| **Unique `microVmId` identity** | `/launch` captures microVmId from payload → writes it into `datadog.yaml` as `hostname:` → starts agent. Agent resolves hostname once at startup; deferred start ensures per-clone identity. | `hook_server.py: launch()` |
| **Capture stdout/stderr** | supervisord redirects user-app streams to `/var/log/app/app.log`; agent tails via `conf.d`. | `supervisord.conf` + `user-app-logs.yaml` |
| **Lifecycle events in DD** | DogStatsD event + `microvm.lifecycle.hook` counter, tagged `microvm_id` + `hook`. | `hook_server.py: _emit_hook_event()` |
| **APM auto-instrumentation** | `ddtrace-run python3.11 /app/app.py` as the user-app command line. | `supervisord.conf [program:user-app]` |
| **Graceful flush on terminate AND suspend** | `datadog-agent stop` talks to the agent's IPC socket (`/var/run/datadog/agent_ipc.socket`) and drives the internal flush path. Called on `/suspend` (drains buffers then pre-warms a fresh agent so `/resume` is a cheap no-op) and on `/terminate` (final drain before VM teardown). | `hook_server.py: _agent_graceful_stop()`, `_supervisor_start_agent()`, `suspend()`, `terminate()` |
| **DD_API_KEY injection into the guest** | `deploy-microvm.sh` writes `.dd-env` from shell env at zip time; image includes it; entrypoint sources it. | `deploy-microvm.sh` + `entrypoint.sh` + Dockerfile wildcard COPY |

## Restrictions and non-standard practices

### What the upstream Datadog Agent does NOT natively support

The full agent was designed for *one long-lived agent per host*. Every
adapter below exists to bridge that model to MicroVM's snapshot-cloned,
short-lived, lifecycle-hook-driven shape:

| Adapter | What it works around | Support posture |
|---------|---------------------|-----------------|
| `autostart=false` on `datadog-agent` + `/launch`-time trigger | No native "start agent after signal X" pattern | Not documented by DD |
| Runtime re-rendering of `/etc/datadog-agent/datadog.yaml` | Agent resolves hostname once at startup; no SIGHUP / IPC re-read | Not documented |
| `rm -f /etc/datadog-agent/auth_token` on every boot | Auth token would otherwise bake into the Firecracker snapshot | Not documented; undefined behavior if DD changes how the token is written |
| File-based stdout capture (supervisord → file → agent tail) | Log source types don't include FIFO, `/proc/<pid>/fd/1`, or direct stdin | Documented (file source is official) but the whole "funnel stdout to a file for tailing" pattern isn't idiomatic DD guidance |
| `datadog-agent stop` via IPC socket on `/terminate` | There is no `agent flush` subcommand; `stop` is the closest graceful-shutdown primitive | Documented subcommand, but using it as a "flush before VM dies" is not a documented use case |
| supervisord as PID 1 | Official `datadog/agent:7` image uses s6-overlay, not supervisord | Supervisord works; it's just not what DD tests against |

**Consequence:** every one of these adapters may need re-validation when
the agent upgrades. DD doesn't maintain a compatibility contract for
this integration shape. For production use, the officially-supported
alternative is `serverless-init` (which lives in the same `datadog-agent`
repo at `cmd/serverless-init/`).

### Operational restrictions inherited from the MicroVM preview

- **ARM64 (aarch64) only** — the yum repo baseurl is hardcoded to
  `/stable/7/aarch64/`. Builds targeting x86_64 will fail loudly with a
  "package not available" error (intentional).
- **us-east-2 only** — inherited from the MicroVM preview itself.
- **No VPC egress** by default; the default `INTERNET_EGRESS` connector
  is required so the agent can reach `*.datadoghq.com`.
- **Image size** — ~400 MB for the agent RPM + Python + supervisord + pip
  deps. Well within the 32 GB zip artifact limit, but longer snapshot
  clone times than a minimal image.
- **Secret posture** — `.dd-env` baked into the image = key lives in the
  Firecracker snapshot shared across all clones launched from that
  image. Acceptable for private preview; not for shared production.
- **DD_API_KEY must be in the deploying shell** — `deploy-microvm.sh`
  will emit a warning and proceed without injecting the key otherwise.
  The agent will then start but fail auth silently; metrics/logs/events
  simply don't appear in DD.
- **Boot-to-`/launch` log gap** — `user-app-logs.yaml` uses
  `start_position: end`, so user-app log lines between container boot
  and `/launch` are not shipped. Flip to `beginning` at the cost of
  re-shipping the entire file on every suspend/resume agent restart.
- **Agent state during SUSPEND** — `/suspend` does a flush-and-pre-warm
  before returning 200: `datadog-agent stop` (flushes buffers to DD
  intake) followed by `supervisorctl start` (brings up a fresh agent).
  The VM is frozen with the fresh agent already running, so `/resume`
  is a no-op. Total `/suspend` budget: ~5–10 s against a 120 s
  timeout. `/resume` stays fast (no agent work on the hot path).

## Durability posture & known data-loss gap

Not every observability signal in this integration has the same delivery
guarantee. The DogStatsD path is fire-and-forget UDP and *will* drop
packets in specific windows; the logs and traces paths are durable
across agent restarts. Documenting this so it's known rather than
folklore:

### Per-signal durability

| Signal | Transport | Durable across agent-down windows? | Notes |
|--------|-----------|------------------------------------|-------|
| User-app logs | file tail (`/var/log/app/app.log`) | ✅ Yes | Agent position registry at `/opt/datadog-agent/run/registry.json` survives restart. Worst case: file rotates past the last-read position during a long outage. |
| APM traces | TCP to `127.0.0.1:8126` | ⚠️ Partial | `ddtrace` client has a ring buffer (~100 spans). Overflow during extended downtime drops oldest spans. Brief (~5 s) /suspend pre-warm: safe. |
| Host-infra metrics (CPU / mem / disk) | Agent-internal | ✅ Correct-by-absence | Checks don't run while agent is down; no bad data generated. Gaps in timeseries, not wrong values. |
| **Lifecycle events** (`microvm.launch`, etc.) | **DogStatsD UDP (`127.0.0.1:8125`)** | **❌ No** | Kernel returns ECONNREFUSED when agent isn't listening; packet silently dropped. No retry, no buffer. |
| User-emitted DogStatsD custom metrics (if added) | Same | ❌ No | Same vulnerability as lifecycle events. |

### Windows where DogStatsD packets are dropped

1. **Pre-`/launch`.** Agent is dormant by design (`autostart=false`).
   Mitigated in `hook_server.py` by gating `_emit_hook_event()` on
   `micro_vm_id is not None` — we don't emit pre-launch events in the
   first place.
2. **During `/suspend`'s pre-warm (`_agent_graceful_stop()` →
   `_supervisor_start_agent()`).** ~5 s window where the old agent has
   exited and the new one hasn't finished listening. Lifecycle events
   emitted inside `/suspend` complete *before* this window (we emit
   first, flush, then stop+start), so the current lifecycle events are
   safe; but any user-app DogStatsD packets in this ~5 s gap are lost.
3. **Post-`/terminate`.** Agent is stopped, VM is being torn down.
   Anything emitted after the final `_agent_graceful_stop()` call is
   dropped. Low-risk in practice since `hook_server.py` returns from
   `/terminate` immediately after emitting and stopping.
4. **Agent failed to start.** If `supervisorctl start datadog-agent`
   times out or the agent crashes, every subsequent DogStatsD emission
   is dropped until someone restarts the agent. Most diagnostic signal
   for this: library's `Error submitting packet: ECONNREFUSED` WARNING
   in hook-server logs.

### Proposals considered

Documented here so they're traceable later without re-deriving:

| Proposal | Shape | Pros | Cons |
|----------|-------|------|------|
| **A. Gate emissions on known-running state** | Module-level `_agent_should_be_running` bool; toggle in start/stop helpers; skip `statsd.event/increment` when False | Prevents silent UDP drops; surfaces "data lost" via `logger.info` instead of library WARNING | Doesn't *save* lost events, just acknowledges them |
| **B. Emit lifecycle events as structured logs, not DogStatsD** | `_emit_hook_event()` writes JSON to `/var/log/platform/platform-events.log`; agent tails as log source `source:microvm-platform` | Durable (log-tail has position registry); single product surface | Changes UX: events land in Logs Explorer, not Events Explorer; needs file-rotation config |
| **C. Dual-emit (DogStatsD + log file) with dedup-by-`event_id`** | Both paths, each record carries the same UUID; backend dedups | Both surfaces populated; durable fallback if DogStatsD drops | Doubles DD ingestion cost; schema drift risk across the two paths; DD has no native "dedupe by field" feature — consumers must dedupe manually; two paths to debug when something's missing |
| **D. Replace DogStatsD with direct HTTP intake** (`api.datadoghq.com/api/v1/events`) | `hook_server.py` POSTs events directly via `requests`/`urllib`, with retry + backoff | Single reliable path; no agent-uptime dependency; matches the pattern `aws.lambda.enhanced.invocations` uses | Own the retry/backoff; DD API key handling moves out of the agent; event shape/limits governed by DD's REST API rather than DogStatsD client |

### Recommendation (deferred)

**For this POC, do nothing and name the gap.** Lifecycle events are
low-frequency, low-blast-radius signals; losing some during rare
agent-down windows doesn't invalidate the integration's overall value.
The remaining signals (logs, traces, host-infra) are not vulnerable.

**Re-visit when any of these triggers fires:**
- A business requirement appears for billing-grade lifecycle-event
  delivery → **Option B** (not C — don't split product surfaces unless
  absolutely needed) or **Option D** (if you want to keep Events
  Explorer UX).
- User apps start adopting DogStatsD for critical custom metrics and
  see drops → push responsibility back to the user app (client-side
  buffering, or emit as structured logs). Don't try to solve UDP
  delivery at the platform layer.
- Frequent `ECONNREFUSED` WARNINGs appear in steady state (not just
  during `/suspend` pre-warm) → that's a bug signal, not a durability
  problem; the agent probably isn't running when it should be.
  Investigate `supervisorctl status datadog-agent`, not the transport.

**Left deliberately unfixed:**
- The `datadog.dogstatsd` library's WARNING logs on packet drop are
  intentionally un-silenced — they're a real signal that the agent is
  unreachable. Silencing them trades diagnostic visibility for cleaner
  logs, and we decided the visibility is worth keeping.
- No log-level or sampling control added; DogStatsD chatter during
  /suspend pre-warm stays noisy.

## Adapting for different user-app languages

The platform layer is language-agnostic by design. Retargeting to a
different user-app language touches **at most 3 files** and never
touches `hook_server.py`, `entrypoint.sh`, or `datadog.yaml.template`.

### The three retargetable files

1. `supervisord.conf` — the `[program:user-app]` block's `command=` line
2. `Dockerfile` — the USER-APP LAYER section (runtime install + COPY)
3. `user-app-logs.yaml` — the `source:` field (parser-hint tag)

### Python (the reference implementation)

```ini
# supervisord.conf
[program:user-app]
command=ddtrace-run python3.11 /app/app.py
```

```dockerfile
# Dockerfile user-app layer
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3.11 install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py
```

```yaml
# user-app-logs.yaml
source: python
```

### Node.js

```ini
# supervisord.conf
[program:user-app]
command=node --require dd-trace/init /app/server.js
```

```dockerfile
# Dockerfile user-app layer
RUN dnf install -y nodejs npm && dnf clean all
WORKDIR /app
COPY package.json package-lock.json /app/
RUN npm ci --prefix /app
RUN npm install --prefix /app dd-trace      # Node APM library
COPY server.js /app/server.js
```

```yaml
# user-app-logs.yaml
source: nodejs
# Remove the python-timestamp multi-line rule; Node console.log has no
# standard timestamp prefix.
```

Environment: `DD_TRACE_ENABLED=true` (default) works as-is.

### Java

```ini
# supervisord.conf
[program:user-app]
command=java -javaagent:/app/dd-java-agent.jar -jar /app/app.jar
```

```dockerfile
# Dockerfile user-app layer
RUN dnf install -y java-17-amazon-corretto-headless && dnf clean all
WORKDIR /app
# Fetch dd-java-agent.jar at build time (ARM64 jar — JVM agent is arch-neutral)
RUN curl -L -o /app/dd-java-agent.jar \
    "https://dtdg.co/latest-java-tracer"
COPY app.jar /app/app.jar
```

```yaml
# user-app-logs.yaml
source: java
log_processing_rules:
  - type: multi_line
    name: java_stack_trace
    # Java log lines typically start with a timestamp or log level:
    pattern: '^(\d{4}-\d{2}-\d{2}|INFO|WARN|ERROR|DEBUG|TRACE)'
```

### Go — the one language that needs user code change

Go's `dd-trace-go` cannot be injected at the command line. It requires
a source-level import:

```go
import _ "gopkg.in/DataDog/dd-trace-go.v1/ddtrace/tracer"

func main() {
    tracer.Start()
    defer tracer.Stop()
    // … user code …
}
```

So **the "zero user code change" property does not hold for APM with
Go.** Everything else (logs, host-infra, lifecycle events) still works.

```ini
# supervisord.conf
[program:user-app]
command=/app/user-app
```

```dockerfile
# Dockerfile user-app layer — Go binary is self-contained (CGO off)
WORKDIR /app
COPY user-app /app/user-app
RUN chmod +x /app/user-app
```

```yaml
# user-app-logs.yaml
source: go
```

### .NET, Ruby, PHP — env-var-driven APM (no command change)

For .NET in particular, the tracer attaches via environment variables
only; no command change needed:

```dockerfile
# Install Datadog .NET tracer
RUN curl -L -o /tmp/dd.rpm "https://dtdg.co/latest-dotnet-tracer-rpm" \
 && dnf install -y /tmp/dd.rpm && rm /tmp/dd.rpm

# Environment toggles the tracer at runtime
ENV CORECLR_ENABLE_PROFILING=1
ENV CORECLR_PROFILER={846F5F1C-F9AE-4B07-969E-05C26BC060D8}
ENV CORECLR_PROFILER_PATH=/opt/datadog/Datadog.Trace.ClrProfiler.Native.so
ENV DD_DOTNET_TRACER_HOME=/opt/datadog
```

```ini
# supervisord.conf
[program:user-app]
command=/app/dotnet-app
```

Ruby follows a similar pattern: `bundle exec` with `DD_TRACE_ENABLED=1`
and `require 'ddtrace/auto_instrument'` at the top of the entrypoint
(one-line source change — arguably still zero "user app" change if the
platform owns the entrypoint).

## Factoring for multi-language support

If the platform image needs to host multiple user-app languages without
forking `supervisord.conf` per language:

- Make the `[program:user-app]` `command=` configurable via an env var
  (e.g. `USER_APP_CMD`), set by the user in their own small Dockerfile
  layer on top of the base image.
- Keep language-specific runtime installs (`dnf install nodejs`, JDK,
  etc.) in the user's Dockerfile layer too; the base image only needs
  Python 3.11 for `hook_server.py` + supervisord.
- `user-app-logs.yaml` can stay generic (`source: user-app`) — users
  override by writing their own `conf.d/user-app.d/conf.yaml` if they
  want a language-specific parser hint.

## Related files in the repo

- `sample-flask-app-datadog-agent-poc/README.md` — operational how-to for
  this specific POC (build/run/deploy commands)
- `HOWTO.md` — general Lambda MicroVM lifecycle state machine; complements
  this doc by explaining *when* each hook fires
- `CLAUDE.md` — project overview and repo layout
- `deploy-microvm.sh` — deploy tooling; contains the `.dd-env` injection
  logic described above
- `sample-flask-app/` — the bare-bones hook-server reference without any
  DD integration
- `sample-flask-app-using-serverless-comp-poc/` — the
  `datadog-serverless-compat` alternative (out of scope for this doc but
  the same problem solved differently)
