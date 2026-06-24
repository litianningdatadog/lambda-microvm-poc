# Sample Node.js Guest Application

A Node.js port of [`sample-flask-app/`](../sample-flask-app/). Implements the same
Lambda MicroVMs lifecycle hooks plus a `/execute` endpoint that evaluates
JavaScript via the built-in [`vm`](https://nodejs.org/api/vm.html) module
(with a 5-second timeout and a sandboxed `console`).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/launch` | Launch hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| POST | `/execute` | Run a JavaScript snippet (`{"code": "..."}`) |
| GET  | `/health` | Health check |

## Running

```bash
npm install
node app.js

# Or via Docker (ARM64)
docker build -t sample-nodejs-app .
docker run --rm -p 8080:8080 sample-nodejs-app
```

The application listens on port **8080**.

## Deployment

### 1. Streamlined MicroVM deployment

```bash
export DD_API_KEY=<YOUR_API_KEY>; make build; cd - ;  export S3_BUCKET=microvm-sample-nodejs-app; cd ..; ./deploy-microvm.sh sample-nodejs-app
```

### 2. Local Docker-based deployment

```bash
export DD_API_KEY=<YOUR_API_KEY>;make stop; sleep 3; make build-local; make start; sleep 5; make check
```

## Testing

```bash
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "console.log(1 + 1)"}'
```
