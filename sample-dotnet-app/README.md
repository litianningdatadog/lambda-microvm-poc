# Sample .NET Guest Application

A .NET 8 port of [`sample-flask-app/`](../sample-flask-app/). Implements the
same Lambda MicroVMs lifecycle hooks plus a `/execute` endpoint that interprets
C# source via [Roslyn scripting](https://github.com/dotnet/roslyn/blob/main/docs/wiki/Scripting-API-Samples.md)
(`Microsoft.CodeAnalysis.CSharp.Scripting`). `Console.Out` / `Console.Error`
are redirected per request so snippet output is captured.

Built on ASP.NET Core minimal APIs (top-level statements). Targets **net8.0**.

Pre-imported namespaces in the script context: `System`, `System.Linq`,
`System.Collections.Generic`, `System.IO`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/launch` | Launch hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| POST | `/execute` | Run a C# snippet (`{"code": "..."}`) |
| GET  | `/health` | Health check |

## Running

```bash
dotnet run

# Or via Docker (ARM64)
docker build -t sample-dotnet-app .
docker run --rm -p 8080:8080 sample-dotnet-app
```

The application listens on port **8080**.

## Testing

```bash
# Statements end with `;`. Expressions return a value too — Roslyn scripting
# accepts either.
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "Console.WriteLine(1 + 1);"}'
```
