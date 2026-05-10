# Getting Datadog Traces Working in Lambda MicroVMs

This document explains why traces fail to appear in Datadog for each sample app
and what changes were made to fix them.

## Root Causes

Two distinct problems prevent traces from reaching Datadog:

1. **Wrong exporter** — the tracer detects `AWS_LAMBDA_FUNCTION_NAME` and switches
   to a stdout/log-based exporter instead of sending spans over HTTP to the embedded
   trace agent on `localhost:8126`.

2. **No spans created** — the app uses the JDK/stdlib HTTP server, which is not in
   the tracer's auto-instrumentation catalog.

---

## Cross-Cutting Fix: `serverless-init`

All language tracers that check for `/tmp/datadog/mini_agent_ready` (currently
Node.js, potentially others in future versions) need this file to exist before they
initialize, or they fall back to the log exporter.

**File:** `cmd/serverless-init/main.go` in the `datadog-agent` repo

**Change:** Create `/tmp/datadog/mini_agent_ready` inside `setupTraceAgent()`, after
the embedded trace agent has started and bound to port 8126:

```go
// Sentinel file signals to language tracers (e.g. dd-trace-js) that an HTTP
// trace agent is available on localhost:8126. Without it, dd-trace detects
// AWS_LAMBDA_FUNCTION_NAME and switches to the log exporter.
if err := os.MkdirAll("/tmp/datadog", 0o755); err == nil {
    if f, err := os.Create("/tmp/datadog/mini_agent_ready"); err == nil {
        f.Close()
    }
}
```

This must be compiled into a new `serverless-init` binary and copied into each app
directory via `make build`.

---

## Shared Dockerfile Changes (all 5 apps)

Added to every `Dockerfile`:

```dockerfile
ENV DD_TRACE_AGENT_URL=http://localhost:8126
ENV DD_TRACE_STARTUP_LOGS=true
ENV DD_TRACE_DEBUG=true
```

Also fixed a linter-introduced typo (`devmirovm` → `devmicrovm`) and removed
duplicate `DD_ENV` entries.

---

## Per-App Changes

### Node.js (`sample-nodejs-app`)

**Problem:** `dd-trace` reads `/tmp/datadog/mini_agent_ready` at startup. If absent
(and `AWS_LAMBDA_FUNCTION_NAME` is set), it selects the log exporter and bypasses
`localhost:8126` entirely. The relevant logic is in:

```
/dd_tracer/node/node_modules/dd-trace/packages/dd-trace/src/exporter.js
```

```js
const usingAgent = inAWSLambda && (
  fs.existsSync('/opt/extensions/datadog-agent') ||   // Datadog Lambda extension
  fs.existsSync('/tmp/datadog/mini_agent_ready')       // serverless-init sentinel
)
return inAWSLambda && !usingAgent ? require('./exporters/log') : require('./exporters/agent')
```

**Fix:** The `serverless-init` sentinel file fix above (no app code changes needed).

---

### Python (`sample-python-app`)

**Problem 1:** `ddtrace` switches to the log writer in Lambda unless `DD_AGENT_HOST`,
`DD_TRACE_AGENT_URL`, or similar env vars are explicitly set. The check is in
`ddtrace/internal/writer/writer.py`:

```python
def _use_log_writer() -> bool:
    if env.get("DD_TRACE_AGENT_URL"):  # set → use AgentWriter
        return False
    return in_aws_lambda()             # unset → log writer
```

**Fix 1 (Dockerfile):** `ENV DD_TRACE_AGENT_URL=http://localhost:8126`

**Problem 2:** `http.server.HTTPServer` (Python stdlib) is not auto-instrumented by
`ddtrace-run`.

**Fix 2 (`app.py`):** Added a `_span()` context manager using `ddtrace.tracer.trace()`
and wrapped every request handler with it:

```python
from contextlib import nullcontext
try:
    from ddtrace import tracer as dd_tracer
except ImportError:
    dd_tracer = None

def _span(method, path):
    if dd_tracer:
        return dd_tracer.trace("http.request", resource=f"{method} {path}", span_type="web")
    return nullcontext()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _span("GET", self.path):
            ...
    def do_POST(self):
        with _span("POST", self.path):
            ...
```

---

### Go (`sample-go-app`)

**Problem 1:** `dd-trace-go` sets `logToStdout = true` when `AWS_LAMBDA_FUNCTION_NAME`
is detected (`ddtrace/tracer/option.go:573`). Once set, `agentDisabled` becomes true
and `DD_TRACE_AGENT_URL` has no effect.

**Fix 1 (`main.go`):**

```go
tracer.Start(tracer.WithLambdaMode(false))
```

**Problem 2:** Plain `http.NewServeMux()` creates no HTTP spans.

**Fix 2 (`main.go`):** Replace with the instrumented mux from `dd-trace-go`:

```go
import ddhttp "gopkg.in/DataDog/dd-trace-go.v1/contrib/net/http"

mux := ddhttp.NewServeMux()
```

---

### Java (`sample-java-app`)

**Problem:** `com.sun.net.httpserver.HttpServer` (JDK internal) is not in
dd-java-agent's instrumentation catalog — zero spans are created for any request.

Note: unlike the other tracers, dd-java-agent does **not** switch to a log exporter
on Lambda detection. `DDAgentWriter` (HTTP to `localhost:8126`) is always the default,
so `DD_TRACE_AGENT_URL` is not needed here.

**Fix (`pom.xml`):** Add `dd-trace-api` as a `provided` dependency (provided at
runtime by the javaagent, not bundled into the shaded jar):

```xml
<dependency>
    <groupId>com.datadoghq</groupId>
    <artifactId>dd-trace-api</artifactId>
    <version>1.44.1</version>
    <scope>provided</scope>
</dependency>
```

**Fix (`App.java`):** Annotate all handler methods with `@Trace`. Because annotations
cannot be applied to lambdas, the `emptyHook()` lambda factory was refactored into
four named static methods:

```java
import datadog.trace.api.Trace;

@Trace(operationName = "http.server.request", resourceName = "GET /health")
private static void handleHealth(HttpExchange exchange) throws IOException { ... }

@Trace(operationName = "http.server.request", resourceName = "POST /ready")
private static void handleReady(HttpExchange exchange) throws IOException { ... }

// ... handleLaunch, handleResume, handleSuspend, handleTerminate, handleExecute
```

---

### .NET (`sample-dotnet-app`)

**No code or Dockerfile changes needed.**

The Datadog CLR profiler's `IsRunningInLambda` check requires **both**:
1. `AWS_LAMBDA_FUNCTION_NAME` is set, **and**
2. `/opt/extensions/datadog-agent` exists on disk

Since `serverless-init` does not install the Datadog Lambda extension binary at that
path, `IsRunningInLambda` is always `false` in a MicroVM container. The profiler
stays in its default HTTP mode and sends traces to `DD_TRACE_AGENT_URL`.

ASP.NET Core Minimal API (`app.MapGet` / `app.MapPost`) is fully auto-instrumented
by the CLR profiler. The `dd-lib-dotnet-init` image ships an ARM aarch64-native
`Datadog.Trace.ClrProfiler.Native.so`, so there is no architecture mismatch.

---

## How Each Tracer Detects Lambda

| Tracer | Detection mechanism | Switches to log exporter? | Override |
|--------|--------------------|-----------------------------|---------|
| Node.js (`dd-trace`) | Checks `/tmp/datadog/mini_agent_ready` | Yes, if file absent | Create the sentinel file (serverless-init fix) |
| Python (`ddtrace`) | `in_aws_lambda()` in `_use_log_writer()` | Yes, unless `DD_TRACE_AGENT_URL` set | `ENV DD_TRACE_AGENT_URL=http://localhost:8126` |
| Go (`dd-trace-go`) | `os.LookupEnv("AWS_LAMBDA_FUNCTION_NAME")` sets `logToStdout=true` | Yes, `DD_TRACE_AGENT_URL` ineffective | `tracer.WithLambdaMode(false)` |
| Java (`dd-java-agent`) | Reads env var but does not change writer | No — always `DDAgentWriter` | N/A |
| .NET (CLR profiler) | Requires env var + `/opt/extensions/datadog-agent` on disk | No | N/A |
