#!/usr/bin/env node
/**
 * Sample guest application that implements Lambda MicroVMs lifecycle hooks.
 *
 * Listens on port 8080 and implements ready, launch, resume, suspend, terminate.
 *
 * Endpoints:
 *  - POST /aws/lambda-microvms/runtime/beta/v1/{ready,launch,resume,suspend,terminate}
 *  - POST /execute   (evaluates JavaScript via the built-in `vm` module)
 *  - GET  /health
 */

const express = require('express');
const vm = require('vm');

const BASE_PATH = '/aws/lambda-microvms/runtime/beta/v1';
const PORT = 8080;

const app = express();
app.use(express.json());

let microVmId = null;

function nowTs() {
  return new Date().toISOString();
}

function log(msg) {
  console.log(`${nowTs()} - INFO - [sample-nodejs-app] ${msg}`);
}

app.get('/health', (req, res) => {
  log(`Health check called [ts=${nowTs()}, microVmId=${microVmId}]`);
  res.json({ status: 'healthy' });
});

app.post(`${BASE_PATH}/ready`, (req, res) => {
  log(`Ready hook called [ts=${nowTs()}, microVmId=${microVmId}]`);
  res.status(200).end();
});

app.post(`${BASE_PATH}/launch`, (req, res) => {
  const data = req.body || {};
  microVmId = data.microVmId;
  const meshIpv6Address = data.meshIpv6Address;
  log(`Launch hook called — ts=${nowTs()}, microVmId=${microVmId}, meshIpv6Address=${meshIpv6Address}`);
  res.status(200).end();
});

app.post(`${BASE_PATH}/resume`, (req, res) => {
  log(`Resume hook called [ts=${nowTs()}, microVmId=${microVmId}]`);
  res.status(200).end();
});

app.post(`${BASE_PATH}/suspend`, (req, res) => {
  log(`Suspend hook called [ts=${nowTs()}, microVmId=${microVmId}]`);
  res.status(200).end();
});

app.post(`${BASE_PATH}/terminate`, (req, res) => {
  log(`Terminate hook called [ts=${nowTs()}, microVmId=${microVmId}]`);
  res.status(200).end();
});

app.post('/execute', (req, res) => {
  try {
    const code = (req.body || {}).code || '';
    if (!code) return res.status(400).json({ error: 'No code provided' });

    log(`Execute called [ts=${nowTs()}, microVmId=${microVmId}]`);

    let stdout = '';
    let stderr = '';
    const sandbox = {
      console: {
        log: (...args) => { stdout += args.map((a) => String(a)).join(' ') + '\n'; },
        error: (...args) => { stderr += args.map((a) => String(a)).join(' ') + '\n'; },
        warn: (...args) => { stderr += args.map((a) => String(a)).join(' ') + '\n'; },
      },
    };

    try {
      vm.runInNewContext(code, sandbox, { timeout: 5000 });
      return res.json({ success: true, output: stdout, stderr });
    } catch (err) {
      return res.json({ success: false, error: (err && err.stack) || String(err), stderr });
    }
  } catch (err) {
    return res.status(500).json({ error: String(err) });
  }
});

function logEnvVars() {
  const env = process.env;
  const keys = Object.keys(env).sort();
  log(`Environment dump (${keys.length} vars) [ts=${nowTs()}]`);
  for (const key of keys) {
    log(`  env ${key}=${env[key]}`);
  }
}

app.listen(PORT, '0.0.0.0', () => {
  log(`Starting sample guest application on port ${PORT}`);
  logEnvVars();
  console.log(`
Sample commands (server running on port ${PORT}):

  curl http://127.0.0.1:${PORT}/health

  curl -X POST http://127.0.0.1:${PORT}${BASE_PATH}/ready

  curl -X POST http://127.0.0.1:${PORT}${BASE_PATH}/launch \\
    -H 'Content-Type: application/json' \\
    -d '{"microVmId": "hello_world", "meshIpv6Address": "::1"}'

  curl -X POST http://127.0.0.1:${PORT}${BASE_PATH}/resume
  curl -X POST http://127.0.0.1:${PORT}${BASE_PATH}/suspend
  curl -X POST http://127.0.0.1:${PORT}${BASE_PATH}/terminate

  curl -X POST http://127.0.0.1:${PORT}/execute \\
    -H 'Content-Type: application/json' \\
    -d '{"code": "console.log(1 + 1)"}'
`);
});
