# Sample Guest Application

A simple Python application that implements the Lambda MicroVMs lifecycle hooks.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/aws/lambda-microvms/runtime/beta/v1/ready` | Ready hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/resume` | Resume hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/suspend` | Suspend hook |
| POST | `/aws/lambda-microvms/runtime/beta/v1/terminate` | Terminate hook |
| GET | `/health` | Health check |

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

The application listens on port 9000.

## Testing

```bash
# Test resume hook
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/resume \
  -H "Content-Type: application/json" \
  -d '{"microVmId": "vm-123", "meshIpv6Address": "fe80::1", "changedResources": ["Entropy"]}'

# Test suspend hook
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/suspend

# Test terminate hook
curl -X POST http://localhost:9000/aws/lambda-microvms/runtime/beta/v1/terminate
```
