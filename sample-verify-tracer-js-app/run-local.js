#!/usr/bin/env node
'use strict'

// Real tracer + HTTP/MessagePack/DogStatsD, synthetic lifecycle notifications.
// This deliberately does NOT claim to reproduce a Firecracker memory snapshot.
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { once } = require('node:events')
const { mkdir, writeFile } = require('node:fs/promises')
const { setTimeout: delay } = require('node:timers/promises')
const path = require('node:path')
const { request } = require('./verify')
const BASE = '/aws/lambda-microvms/runtime/v1'
const port = Number(process.env.LOCAL_PORT || 18080)
const url = `http://127.0.0.1:${port}`
const results = path.resolve('results', `local-${new Date().toISOString().replaceAll(/[:.]/g, '-')}`)

async function run (microvm) {
  const env = { ...process.env, PORT: String(port), VERIFY_FORWARD_TO_AGENT: 'false',
    DD_TRACE_AGENT_URL: 'http://127.0.0.1:18126', DD_DOGSTATSD_PORT: '18125', DD_VERSION: 'local-smoke' }
  for (const key of ['DD_API_KEY', 'DD_APP_KEY', 'NODE_OPTIONS', 'AWS_LAMBDA_FUNCTION_NAME', 'AWS_LAMBDA_MICROVM_IMAGE_ARN', 'MICROVM_TOKEN']) delete env[key]
  if (microvm) env.AWS_LAMBDA_MICROVM_IMAGE_ARN = 'local-lifecycle-simulation'
  const child = spawn(process.execPath, ['app.js'], { cwd: __dirname, env, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  let started = false
  child.stdout.on('data', data => { output += data; if (data.toString().includes('verifier_listening')) started = true })
  child.stderr.on('data', data => { output += data })
  try {
    const deadline = Date.now() + 20000
    while (!started) {
      if (child.exitCode !== null || Date.now() > deadline) throw new Error(`Guest failed to start:\n${output}`)
      await delay(50)
    }
    const before = await request(url, '/state')
    assert.equal(before.microvm, microvm)
    await request(url, `${BASE}/ready`, 'POST')
    const prepared = await request(url, '/state')
    await request(url, `${BASE}/ready`, 'POST')
    await request(url, `${BASE}/run`)
    await request(url, `${BASE}/run-extra`, 'POST')
    await request(url, `${BASE}/resume`, 'POST')
    const negative = await request(url, '/state')
    assert.equal(negative.runtimeId, before.runtimeId, 'ready/lookalike/GET/resume must not refresh identity')
    assert.equal(negative.rcClientId, before.rcClientId)
    assert.deepEqual(negative.before, prepared.before, 'repeated ready must not reseed the image scenario')
    await request(url, `${BASE}/run`, 'POST')
    const after = await request(url, '/state')
    if (microvm) {
      assert.equal(after.rcClientId, null, 'Remote Config stays disabled in MicroVM mode')
      assert.notEqual(after.runtimeId, before.runtimeId)
      const verification = await request(url, '/verify', 'POST')
      verification.execution = 'local-lifecycle-simulation-not-a-snapshot'
      await writeFile(path.join(results, 'verification.json'), JSON.stringify(verification, null, 2) + '\n')
      for (const [name, passed] of Object.entries(verification.checks)) console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`)
      assert.equal(verification.passed, true, 'see verification.json')
      await request(url, `${BASE}/suspend`, 'POST')
      await request(url, `${BASE}/resume`, 'POST')
      const resumed = await request(url, '/verify', 'POST')
      resumed.execution = 'local-lifecycle-simulation-not-a-snapshot'
      await writeFile(path.join(results, 'verification-resumed.json'), JSON.stringify(resumed, null, 2) + '\n')
      assert.equal(resumed.passed, true)
      assert.equal(resumed.state.runtimeId, after.runtimeId)
      assert.equal(resumed.state.rcClientId, after.rcClientId)
      assert.equal(resumed.state.runCount, 1)
      assert.equal(resumed.state.suspendCount, 1)
      assert.equal(resumed.state.resumeCount, 2)
    } else {
      assert.equal(after.runtimeId, before.runtimeId, 'ordinary Node process must not refresh on a run-like route')
      assert.equal(after.rcClientId, before.rcClientId)
    }
    await request(url, `${BASE}/run`, 'POST')
    const repeated = await request(url, '/state')
    assert.equal(repeated.runtimeId, after.runtimeId, 'second /run must not refresh identity')
    assert.equal(repeated.rcClientId, after.rcClientId)
    console.log(`PASS ${microvm ? 'MicroVM simulation' : 'ordinary-process control'}: exact route/method, idempotent ready, one-shot run, stable resume`)
  } finally {
    const exited = once(child, 'exit')
    child.kill('SIGTERM')
    if (child.exitCode === null) await exited
    await writeFile(path.join(results, microvm ? 'microvm.log' : 'regular.log'), output)
  }
}

async function main () {
  // Do not accidentally attach an inherited AWS ingress token to local requests.
  delete process.env.MICROVM_TOKEN
  await mkdir(results, { recursive: true })
  console.log(`Local lifecycle simulation only; no snapshot or backend proof. Evidence: ${results}`)
  await run(true)
  await run(false)
}
main().catch(error => { console.error(error.stack); process.exitCode = 1 })
