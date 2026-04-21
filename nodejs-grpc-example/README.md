# Node.js gRPC Echo Service

A simple gRPC echo service with bidirectional streaming support. The server runs on AWS Lambda MicroVM with SSL/TLS encryption using self-signed certificates.

## Setup

```bash
npm install
```

## Server

The server will run on the Lambda MicroVM, and automatically generates self-signed certificates on startup.

It can also be run locally with:

```bash
node server.js
```

## Client

The client connects to the server with SSL credentials and requires a MicroVM auth token.

### Update Auth Token

Before running the client, update the auth token in `client.js` auth interceptor:

```javascript
metadata.set('X-aws-proxy-auth', 'YOUR_MICROVM_AUTH_TOKEN');
```

### Run Client

```bash
node client.js
```

## Features

- SSL/TLS encryption with self-signed certificates
- Unary and bidirectional streaming echo methods
- Request/response logging interceptors
- MicroVM authentication support