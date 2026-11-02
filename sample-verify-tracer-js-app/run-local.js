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

async function saveResult (name, value) {
  value.execution = 'local-lifecycle-simulation-capture-only-not-agent-or-backend-proof'
  await writeFile(path.join(results, `${name}.json`), JSON.stringify(value, null, 2) + '\n')
  for (const [check, passed] of Object.entries(value.checks || {})) {
    console.log(`${passed ? 'PASS' : 'FAIL'} ${name}: ${check}`)
  }
  assert.equal(value.passed, true, `see ${name}.json`)
}

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
      await saveResult('verification-1', verification)
      for (const scenario of ['automatic', 'recovery']) {
        const delivery = await request(url, `/delivery/${scenario}`, 'POST')
        await saveResult(`delivery-${scenario}`, delivery)
        assert.equal(delivery.evidence.forwardToAgent, false, 'local capture is not Agent delivery')
        assert.equal(delivery.evidence.manualFlushCountBefore, delivery.evidence.manualFlushCountAfter)
        if (scenario === 'recovery') {
          assert.deepEqual(delivery.evidence.cases.map(value => value.mode), ['http503', 'disconnect'])
          for (const value of delivery.evidence.cases) {
            assert.ok(value.failureAttempts.length > 0)
            assert.ok(value.failureAttempts.every(attempt => attempt.outcome === `injected-${value.mode}`))
            assert.ok(value.freshAcknowledgements.every(attempt => attempt.outcome === 'accepted'))
            assert.ok(value.probes.length >= 3)
          }
        }
      }
      for (let cycle = 1; cycle <= 3; cycle++) {
        await request(url, `${BASE}/suspend`, 'POST')
        await request(url, `${BASE}/resume`, 'POST')
        const resumed = await request(url, '/verify', 'POST')
        await saveResult(`verification-resumed-${cycle}`, resumed)
        assert.equal(resumed.state.runtimeId, after.runtimeId)
        assert.equal(resumed.state.rcClientId, after.rcClientId)
        assert.equal(resumed.state.runCount, 1)
        assert.equal(resumed.state.suspendCount, cycle)
        assert.equal(resumed.state.resumeCount, negative.resumeCount + cycle)
        assert.equal(resumed.state.verificationCount, cycle + 1)
      }
      const armed = await request(url, '/delivery/termination', 'POST')
      const armedAgain = await request(url, '/delivery/termination', 'POST')
      assert.equal(armed.armed, true)
      assert.equal(armed.marker, armedAgain.marker, 'arming must be idempotent')
      await writeFile(path.join(results, 'termination-armed.json'), JSON.stringify(armed, null, 2) + '\n')
      const terminated = await request(url, `${BASE}/terminate`, 'POST')
      await saveResult('termination', terminated)
      assert.equal(terminated.marker, armed.marker)
      assert.equal(terminated.evidence.runtimeId, after.runtimeId)
      assert.equal(terminated.evidence.manualFlushCountBefore, terminated.evidence.manualFlushCountAfter)
      assert.equal(terminated.state.terminateCount, 1)
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
