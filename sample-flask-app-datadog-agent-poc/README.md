# sample-flask-app-datadog-agent-poc

A feasibility POC for running the **full `datadog-agent`** inside a
Lambda MicroVM **with zero changes required to the user's application
code.** Explicitly not using `serverless-init`, `serverless-compat`, or
any sidecar binary — the only Datadog component is the full Agent RPM.

## Layering — what's "platform" vs what's "user"

```
┌──────────────────────────────────────────────────────┐
│  PLATFORM LAYER  (would be a base image in prod)    │
│                                                      │
│    datadog-agent (RPM)                               │
│    supervisord                                       │
│    hook_server.py      — owns port 9000, bootstraps  │
│                          the agent at /launch, emits │
│                          DD lifecycle events         │
│    datadog.yaml.template                             │
│    user-app-logs.yaml  — tails /var/log/app/app.log  │
│    entrypoint.sh                                     │
└──────────────────────────────────────────────────────┘
                        ▲
                        │ FROM microvm-dd-base:latest
                        │
┌──────────────────────────────────────────────────────┐
│  USER LAYER  (everything a real user writes)         │
│                                                      │
│    requirements.txt    — only their own deps         │
│    app.py              — Flask hello-world,          │
│                          zero DD awareness           │
└──────────────────────────────────────────────────────┘
```

In a real deployment the platform layer would be prebuilt and pushed as
`microvm-dd-base:latest` (or similar). A user's own Dockerfile would
then be:

```dockerfile
FROM microvm-dd-base:latest
COPY requirements.txt /app/
RUN pip3.11 install -r /app/requirements.txt
COPY app.py /app/
```

For this POC, both layers live in a single `Dockerfile` with clear
section comments.

## How each requirement is satisfied

| Requirement | Mechanism | Lives in… |
|-------------|-----------|-----------|
| **0. Snapshot only when user app is running** | `hook_server.py`'s `/ready` handler asks supervisord whether the `user-app` program is in the `RUNNING` state; returns **200** if yes, **403** otherwise. The platform keeps polling until 200. **Process-based only — makes no assumption about the user app's language, framework, or whether it exposes an HTTP endpoint.** Program name is hardcoded in `hook_server.py` and must match the `[program:user-app]` block in `supervisord.conf`. | Platform |
| **1. Unique `microVmId` identity** | `hook_server.py`'s `/launch` handler writes `microVmId` into `datadog.yaml` as `hostname:`, then starts the agent. The agent resolves hostname once at startup; by deferring its start to `/launch`, each clone gets fresh identity. | Platform |
| **2. Capture stdout/stderr from the user app** | `supervisord` redirects user-app stdout/stderr to `/var/log/app/app.log`; the agent tails that file via `user-app-logs.yaml` and ships lines as DD logs. | Platform |
| **3. Lifecycle events in DD** | `hook_server.py` emits a DogStatsD event and `microvm.lifecycle.hook` counter from each of the 5 hooks, tagged `microvm_id`. | Platform |
| **4. Rich features with zero app change** | `ddtrace-run python3.11 /app/app.py` in `supervisord.conf` gives Python APM auto-instrumentation. DogStatsD listener on `localhost:8125` for future custom metrics. All host-infra checks available. | Platform |

## Architecture (PID tree inside the running MicroVM)

```
PID 1  supervisord
├── user-app         autostart=true   (user's unchanged code)
│     command:  ddtrace-run python3.11 /app/app.py
│     stdout/stderr → /var/log/app/app.log
│                      │
│                      └──(tailed by) datadog-agent's user-app.d integration
│
├── hook-server      autostart=true   (platform; owns port 9000)
│     command:  python3.11 /opt/platform/hook_server.py
│     On /ready:     200 iff `supervisorctl status user-app` = RUNNING
│                    (else 403 so the platform keeps polling)
│     On /launch:    render datadog.yaml with DD_HOSTNAME=microVmId →
│                    supervisorctl start datadog-agent → emit DD event
│     On /suspend:   emit event → statsd.flush() → `datadog-agent stop`
│                    (drains buffers to DD intake) → supervisorctl start
│                    (pre-warm fresh agent so /resume is free)
│     On /resume:    emit event only — the pre-warmed agent unfreezes
│                    with the VM; no agent action on this hot path
│     On /terminate: emit event → `datadog-agent stop` (final flush) →
│                    supervisord sees exitcode=0 and does NOT restart
│
└── datadog-agent    autostart=FALSE (started by hook-server at /launch)
      ├── trace-agent     (APM, 8126/tcp)
      └── (process-agent, system-probe disabled — meaningless in MicroVM)
      │
      │  autorestart=unexpected + exitcodes=0 so that graceful-stop
      │  paths (/suspend, /terminate) don't trigger supervisord to
      │  re-launch the agent — hook-server controls the lifecycle
      │  explicitly.
```

## Why `autostart=false` on the agent is load-bearing

The Firecracker snapshot is taken after `/ready` returns 200. Anything
running at that moment has its in-memory state (hostname, auth_token,
DD Agent UUIDs) baked into the snapshot — every clone then shares it.

By keeping the agent dormant at snapshot time and starting it on
`/launch` (which only fires *after* the clone), each MicroVM gets a
fresh, unique agent identity.

## Why `datadog-agent stop` on `/terminate` (not `supervisorctl stop`)

`supervisorctl stop datadog-agent` sends SIGTERM to the agent process,
which can interrupt pending metric/log/trace flushes. `datadog-agent
stop` talks to the agent via its own IPC socket
(`/var/run/datadog/agent_ipc.socket`) and drives the agent's internal
graceful-shutdown path — pending traces finalize, log batches flush,
metric points drain. Cleaner final state before the MicroVM is torn
down at the 60-second `/terminate` timeout.

## Files

| Path | Layer | Purpose |
|------|-------|---------|
| `Dockerfile` | — | Single-stage build; platform + user sections clearly delimited |
| `hook_server.py` | Platform | Owns port 9000; gates `/ready` on user-app RUNNING; starts agent at `/launch`; flush-and-pre-warms across `/suspend`; final flush on `/terminate` |
| `supervisord.conf` | Platform | PID-1 config: user-app (auto), hook-server (auto), datadog-agent (dormant) |
| `datadog.yaml.template` | Platform | `${DD_HOSTNAME}` filled in from `microVmId` at `/launch` |
| `user-app-logs.yaml` | Platform | Log integration tailing `/var/log/app/app.log` |
| `entrypoint.sh` | Platform | Wipes `auth_token`, renders initial `datadog.yaml`, execs supervisord |
| `app.py` | User | Flask hello-world; no DD imports, no hook handlers |
| `requirements.txt` | User | `flask>=2.0.0` and nothing else |
| `.dd-env` (auto-generated, not checked in) | Platform | `export DD_API_KEY=…` written by `deploy-microvm.sh` at zip time; baked into the image via `COPY .dd-env* /opt/platform/`; sourced by `entrypoint.sh`. Cleaned up from the repo after zip + upload via EXIT trap. |

## Running locally

```bash
docker build --platform=linux/arm64 -t flask-dd-agent-poc .

docker run --rm -it \
  -e DD_API_KEY="$DD_API_KEY" \
  -e DD_SITE=datadoghq.com \
  -p 8080:8080 -p 9000:9000 \
  flask-dd-agent-poc
```

Simulate the lifecycle the platform would drive:

```bash
# 1. /ready — platform polls this during image build. Returns 200 only when
#    supervisord reports the user-app program as RUNNING; 403 otherwise.
#    Language- and framework-agnostic. Agent stays dormant regardless.
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/ready

# 2. /launch — platform fires after clone. Agent starts here.
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/launch \
  -H 'Content-Type: application/json' \
  -d '{"microVmId": "test-vm-001", "meshIpv6Address": "fe80::1"}'

# 3. Hit the user app (agent is now tailing logs + instrumenting traces)
curl http://localhost:8080/

# 4. Lifecycle transitions
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/suspend
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/resume
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/terminate
```

Inside the container, inspect with:

```bash
docker exec -it <container_id> /bin/bash
supervisorctl -c /etc/supervisord.conf status
datadog-agent status
```

## Deploying as a MicroVM

From the repo root, with `DD_API_KEY` exported in your shell:

```bash
export DD_API_KEY=<your-key>
./deploy-microvm.sh sample-flask-app-datadog-agent-poc
```

How the key reaches the guest: `deploy-microvm.sh` reads `DD_API_KEY`
from the calling shell at zip time, writes it to a file named `.dd-env`
inside the app directory (umask 077), and lets `zip` pick it up into
the artifact. The Dockerfile's `COPY .dd-env* /opt/platform/` stages
the file into the image; `entrypoint.sh` sources it before launching
supervisord so the agent's rendered `datadog.yaml` gets the real key.
An EXIT trap in `deploy-microvm.sh` deletes the local `.dd-env` after
upload so the plaintext key doesn't leak into your repo.

If `DD_API_KEY` isn't set in the shell, `deploy-microvm.sh` emits a
warning and proceeds; the deploy succeeds but the agent will silently
fail auth when it tries to reach DD intake.

## Honest evaluation — what this POC demonstrates

**It works, but every adapter is customer-owned.** The full
`datadog-agent` has no native awareness of ephemeral, snapshot-cloned,
lifecycle-hook-driven runtimes — that's `serverless-init`'s scope inside
the same upstream repo. Every piece of the platform layer in this POC
(`autostart=false` + deferred start, runtime re-rendering of
`datadog.yaml`, `auth_token` wipe on boot, file-based stdout redirection,
`datadog-agent stop` via IPC socket on `/terminate`) is glue that DD
does not document or support — they may need re-validation on every
agent release.

**The zero-user-code-change contract is preserved by keeping all of that
glue in the platform layer.** A real user ships only the two files under
the USER LAYER heading above. They get APM, logs, DogStatsD, host-infra
metrics, and lifecycle events on the DD timeline without ever knowing
Datadog is there.

## Known gaps / TODOs

- **Secret posture of `.dd-env`.** The key ends up baked into the
  image and thus into the Firecracker snapshot that gets shared across
  clones launched from that image. Acceptable for preview-phase POC;
  for production, the cleaner path is AWS Secrets Manager + IAM on the
  execution role + runtime fetch from `hook_server.py` at `/launch`.
- **Boot-to-`/launch` log gap.** `user-app-logs.yaml` uses
  `start_position: end`, so user-app log lines emitted between container
  boot and `/launch` aren't shipped. Flip to `beginning` if you need
  them, at the cost of re-shipping the file on every agent restart
  (suspend/resume, terminate).
- **`/suspend` budget.** The flush-and-pre-warm path takes roughly
  5–10 seconds (agent stop up to ~20 s `stopwaitsecs`, then fresh agent
  start). Well within the 120 s `/suspend` timeout, but worth knowing
  if you expect unusually slow flushes (large batch backlogs).
- **`AWS_LAMBDA_FUNCTION_ARN` fakery in the Dockerfile.** Hardcoded for
  local `docker run`; remove the `ENV` line before deploying to a real
  MicroVM environment.
- **Single-binary platform image.** For real use this should be split
  into a prebuilt `microvm-dd-base:latest` base image so the user's own
  Dockerfile is truly 3 lines.
- **DogStatsD lifecycle-event durability.** `hook_server.py` emits
  lifecycle events via DogStatsD (UDP to `localhost:8125`), which is
  fire-and-forget — packets are silently dropped when the agent isn't
  listening (briefly during `/suspend` pre-warm, after `/terminate`,
  or if the agent failed to start). Logs and APM traces use different
  transports and are durable. Four proposals (gate on running-state,
  emit as structured logs, dual-emit with dedup, replace DogStatsD
  with HTTP intake) are documented in
  [`HOWTO-DATADOG-AGENT.md`](../HOWTO-DATADOG-AGENT.md#durability-posture--known-data-loss-gap).
  Deferred — no fix applied yet, since lifecycle events are low
  frequency and the other signal types are not at risk.
