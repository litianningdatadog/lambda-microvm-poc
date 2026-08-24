# sample-nodejs-skill-driven-app

Minimal Node.js app for AWS Lambda MicroVMs: an Express server on port 8080
(`/health`, `/execute`) plus the five lifecycle hooks on port 9000, following
the `aws-lambda-microvms` skill's getting-started pattern.

## Run locally

```bash
make build
make start
make check   # smoke-tests /health, a hook, and /execute
make stop
```

## Deploy as a MicroVM

Zip this directory and follow the "Create a MicroVM Image" / "Launch a
MicroVM" steps in the repo root `CLAUDE.md`, or run `./deploy-microvm.sh
sample-nodejs-skill-driven-app` from the repo root.
