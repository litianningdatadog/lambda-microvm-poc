# Sample Java Guest Application

A Java port of [`sample-flask-app/`](../sample-flask-app/). Implements the same
Lambda MicroVMs lifecycle hooks plus a `/execute` endpoint that interprets Java
source via [`jdk.jshell`](https://docs.oracle.com/en/java/javase/21/docs/api/jdk.jshell/jdk/jshell/JShell.html)
— the JShell API that ships with the JDK. A fresh interpreter is created per
request, mirroring the Python sample's stateless code-running behavior.

Built on the JDK's built-in `com.sun.net.httpserver.HttpServer` (no servlet
container) and Jackson for JSON. Targets **Java 21**.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/launch` | Launch hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| POST | `/execute` | Run a Java snippet (`{"code": "..."}`) |
| GET  | `/health` | Health check |

## Running

```bash
mvn package -DskipTests
java -jar target/app.jar

# Or via Docker (ARM64)
docker build -t sample-java-app .
docker run --rm -p 8080:8080 sample-java-app
```

The application listens on port **8080**.

## Testing

```bash
# JShell snippets terminate with `;` — same as a Java statement.
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "System.out.println(1 + 1);"}'
```
