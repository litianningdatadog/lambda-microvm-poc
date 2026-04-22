# sample-flask-app-serverless-init-poc

A Flask-based Lambda MicroVM guest app wrapped by Datadog's `serverless-init`
(ARM64) running as **PID 1**. Exists to demonstrate how lifecycle-hook
handlers and a user app can share a single process while still streaming
stdout/stderr into the Datadog log pipeline.

Sibling POC: `sample-flask-app-using-serverless-comp-poc/` — same idea using
`datadog-serverless-compat` instead. Both are parallel references for the
"init-wrapper as PID 1" pattern.

## Architecture

```
container PID 1  →  /serverless-init            (captures child stdout/stderr,
                         │                       forwards signals, ships logs
                         │                       to Datadog)
                         ↓ exec
         PID N     →  python3.11 app.py         (Flask; lifecycle hooks + /execute)
```

`serverless-init` activates *init mode* only when `AWS_LAMBDA_FUNCTION_ARN`
contains `"microvm"`. The Dockerfile fakes that ARN so init mode engages for
local Docker runs; in a real MicroVM the runtime sets it automatically — remove
the `ENV AWS_LAMBDA_FUNCTION_ARN=...` line before deploying.

## Endpoints

All endpoints are served by the single Flask process on **port 8080**. Port
9000 is `EXPOSE`d in the Dockerfile and published by the Makefile but not yet
bound to anything — reserved for a future split between hook-server and user-app.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook (signal startup complete during image build) |
| POST | `/aws/lambda-microvms/runtime/beta/v1/launch` | Launch hook (receives `microVmId`, `meshIpv6Address`) |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook (post-suspend) |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| GET  | `/health` | Health check |
| POST | `/execute` | Eval arbitrary Python (`{"code": "..."}`) — demo only |

## Prerequisites

- Docker with ARM64 (`linux/arm64`) support — Apple Silicon or `buildx`.
- `serverless-init-linux-arm64` binary checked in at the repo root of this
  directory. Re-download from
  https://github.com/DataDog/serverless-init if missing.

## Build & run (Makefile)

All docker operations are wrapped by the Makefile. Default image / container
name: `flask-serverless-init-poc`.

```bash
make build                     # docker build
make run                       # alias for `make runfg`
make runfg                     # foreground, TTY attached (Ctrl-C stops)
make runbg                     # detached; use `make readlog` to follow
make runwithlog                # foreground + tee to ./run.log
make readlog                   # docker logs -f on the named container
make stop                      # force-remove the named container
```

Targets auto-clean any stale container with the same name before starting, so
`make runbg` followed by `make runwithlog` no longer errors out.

### Overridable variables

| Var | Default | Effect |
|---|---|---|
| `DD_API_KEY` | `dummy-api-key` | Passed via `-e`; real key enables log shipping. |
| `DD_LOG_LEVEL` | `debug` | Overrides `ENV DD_LOG_LEVEL` from the Dockerfile without rebuilding. |
| `LOG_FILE` | `run.log` | Transcript path used by `runwithlog`. |

Examples:

```bash
# Real key, quieter init agent
DD_API_KEY=$MY_DD_KEY make runfg DD_LOG_LEVEL=warn

# Capture a repro transcript with a specific filename
make runwithlog LOG_FILE=repro-$(date +%Y%m%d).log
```

## Running without Docker

`serverless-init` is the reason this POC exists — running bare Python skips
it entirely and defeats the point. Kept here only as a fallback for editing
`app.py` in isolation:

```bash
pip install -r requirements.txt
python app.py
```

Flask will start on `http://127.0.0.1:8080`; no logs reach Datadog.

## Testing

With the container running (either `make runfg` in another terminal or
`make runbg`):

```bash
# Health check
curl http://localhost:8080/health

# Launch hook (sets microVmId inside the app)
curl -X POST http://localhost:8080/aws/lambda-microvms/runtime/beta/v1/launch \
  -H 'Content-Type: application/json' \
  -d '{"microVmId": "vm-local", "meshIpv6Address": "fe80::1"}'

# Resume / suspend / terminate hooks
curl -X POST http://localhost:8080/aws/lambda-microvms/runtime/beta/v1/resume
curl -X POST http://localhost:8080/aws/lambda-microvms/runtime/beta/v1/suspend
curl -X POST http://localhost:8080/aws/lambda-microvms/runtime/beta/v1/terminate

# /execute — evaluates arbitrary Python (demo only; never expose publicly)
curl -X POST http://localhost:8080/execute \
  -H 'Content-Type: application/json' \
  -d '{"code": "print(1 + 1)"}'
```

## Viewing logs

Three layers of log output, all useful:

1. **Docker stdout** — `make readlog` (detached) or the attached terminal
   (foreground). Interleaves `serverless-init` debug lines and Flask log lines.
2. **Persisted transcript** — `make runwithlog` tees everything to `run.log`
   while still streaming to the terminal.
3. **Datadog Live Tail** — with a real `DD_API_KEY`, every stdout/stderr line
   from the Flask child is shipped by `serverless-init`'s logs agent. Filter
   by `service:serverless-init-poc-id` (the `DD_SERVICE` value baked into the
   Dockerfile at line 41).

To reduce init-agent noise when grepping, Flask lines follow the format
`YYYY-MM-DD HH:MM:SS,ms - LEVEL - message`:

```bash
make readlog | grep -E ' - (INFO|WARNING|ERROR) - '
```

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | AL2023 + Python 3.11 + `serverless-init` as ENTRYPOINT |
| `Makefile` | Build / run / log targets (see above) |
| `app.py` | Flask app: 5 lifecycle hooks + `/health` + `/execute` |
| `requirements.txt` | Python deps (Flask) |
| `serverless-init-linux-arm64` | Statically-linked ARM64 init binary (not in git — see Prerequisites) |
