# Sample Go Guest Application

A Go port of [`sample-flask-app/`](../sample-flask-app/). Implements the same
Lambda MicroVMs lifecycle hooks plus a `/execute` endpoint that interprets Go
source via [`traefik/yaegi`](https://github.com/traefik/yaegi) — an in-process
Go interpreter. A fresh interpreter is created per request, mirroring the
Python sample's stateless code-running behavior.

Built on `net/http` (no third-party web framework). Statically compiled with
`CGO_ENABLED=0` so the runtime image is just AL2023 + a single binary.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/launch` | Launch hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| POST | `/execute` | Run a Go snippet (`{"code": "..."}`) |
| GET  | `/health` | Health check |

## Running

```bash
go mod tidy
go run .

# Or via Docker (ARM64)
docker build -t sample-go-app .
docker run --rm -p 8080:8080 sample-go-app
```

The application listens on port **8080**.

## Testing

```bash
# yaegi pre-loads the standard library, so packages like `fmt` are usable
# directly without an `import` statement at the top level.
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "fmt.Println(1 + 1)"}'
```
