'use strict'

const assert = require('node:assert/strict')
const { test } = require('node:test')
const { once } = require('node:events')
const http = require('node:http')
const { encode } = require('@msgpack/msgpack')
const { acknowledgedRequests } = require('./delivery')
const { startIntake } = require('./intake')
const { evaluateFeatures } = require('./feature-checks')

const ids = { traceId: '123', spanId: '456' }
const expected = {
  operation: 'verify.js.automatic', marker: 'delivery-marker', runtimeId: 'runtime-id', service: 'test-service',
  forwardToAgent: true, after: 100
}
const span = {
  trace_id: '123', span_id: '456', parent_id: '0', name: expected.operation, resource: expected.operation,
  service: expected.service, meta: { 'runtime-id': expected.runtimeId, 'verify.delivery.marker': expected.marker },
  metrics: { '_dd.measured': 1 }
}
const payload = encode([[{ ...span, trace_id: 123n, span_id: 456n, parent_id: 0n }]], { useBigInt64: true })
const accepted = overrides => ({
  sequence: 1, receivedAt: 100, completedAt: 101, status: 200, outcome: 'accepted',
  forwarded: true, bytes: payload.length, spans: [span], ...overrides
})

function request (path = '/v0.4/traces') {
  return new Promise((resolve, reject) => {
    // Fixtures reuse a port, not a process-lifetime connection pool.
    const req = http.request({ host: '127.0.0.1', port: 18126, path, method: 'PUT', agent: false }, res => {
      res.resume()
      res.on('error', reject)
      res.on('end', () => resolve(res.statusCode))
    })
    req.on('error', reject)
    req.setTimeout(2000, () => req.destroy(new Error('Test intake request timed out')))
    req.end(path === '/v0.4/traces' ? payload : undefined)
  })
}

async function intakeForTest (t, forward) {
  const previous = process.env.VERIFY_FORWARD_TO_AGENT
  process.env.VERIFY_FORWARD_TO_AGENT = String(forward)
  const intake = startIntake()
  intake.relay.bind(0, '127.0.0.1')
  await Promise.all([once(intake.server, 'listening'), once(intake.udp, 'listening'), once(intake.relay, 'listening')])
  t.after(async () => {
    intake.setTraceFault('none')
    await Promise.all([
      new Promise((resolve, reject) => intake.server.close(error => error ? reject(error) : resolve())),
      new Promise(resolve => intake.udp.close(resolve)),
      new Promise(resolve => intake.relay.close(resolve))
    ])
    if (previous === undefined) delete process.env.VERIFY_FORWARD_TO_AGENT
    else process.env.VERIFY_FORWARD_TO_AGENT = previous
  })
  return intake
}

test('delivery acknowledgement rejects receipt, failures, stale IDs and capture-only forwarding claims', () => {
  const good = accepted()
  const candidates = [
    accepted({ outcome: 'injected-http503', status: 503, forwarded: false }),
    accepted({ outcome: 'injected-disconnect', status: null, forwarded: false }),
    accepted({ outcome: 'upstream-error', status: 503 }),
    accepted({ outcome: null, status: null, completedAt: null }),
    accepted({ completedAt: null }),
    accepted({ completedAt: 99 }),
    accepted({ receivedAt: 99 }),
    accepted({ forwarded: false }),
    accepted({ spans: [{ ...span, span_id: '789' }] }),
    accepted({ spans: [{ ...span, meta: { ...span.meta, 'runtime-id': 'old-runtime' } }] }),
    accepted({ spans: [{ ...span, meta: { ...span.meta, 'verify.delivery.marker': 'old-marker' } }] }),
    accepted({ spans: [{ ...span, parent_id: '789' }] }),
    accepted({ spans: [{ ...span, metrics: {} }] }),
    good
  ]
  assert.deepEqual(acknowledgedRequests(candidates, ids, expected), [good])
  const capture = accepted({ forwarded: false })
  assert.deepEqual(acknowledgedRequests([capture], ids, { ...expected, forwardToAgent: false }), [capture])
})

test('real loopback intake preserves each failed attempt and restores acceptance', async t => {
  const intake = await intakeForTest(t, false)
  const wire = { ...expected, after: 0, forwardToAgent: false }
  intake.setTraceFault('http503')
  assert.equal(await request(), 503)
  assert.equal(await request('/info'), 200, 'fault applies only to trace intake')
  assert.equal(intake.records.traceRequests[0].outcome, 'injected-http503')
  assert.equal(intake.records.traceRequests[0].forwarded, false)
  assert.equal(intake.records.traceRequests[0].status, 503)
  assert.deepEqual(acknowledgedRequests(intake.records.traceRequests, ids, wire), [])

  intake.setTraceFault('disconnect')
  await assert.rejects(request(), error => error.code === 'ECONNRESET')
  const disconnected = intake.records.traceRequests[1]
  assert.equal(disconnected.outcome, 'injected-disconnect')
  assert.equal(disconnected.status, null)
  assert.equal(disconnected.forwarded, false)
  assert.ok(disconnected.completedAt >= disconnected.receivedAt)
  assert.deepEqual(acknowledgedRequests(intake.records.traceRequests, ids, wire), [])

  intake.setTraceFault('none')
  assert.equal(await request(), 200)
  assert.equal(intake.records.traceRequests.length, 3)
  assert.equal(intake.records.spans.length, 3, 'attempts retain exact same IDs without creating spans')
  assert.equal(acknowledgedRequests(intake.records.traceRequests, ids, wire).length, 1)
  assert.deepEqual(intake.records.errors, [], 'injected failures are expected, not upstream errors')
  assert.throws(() => intake.setTraceFault('unknown'), /Unknown trace fault/)
})

test('forwarding requires the completed upstream acknowledgement, not decoded receipt or response headers', async t => {
  let replyStatus = 503
  let pendingReply
  let announcePending
  let forwarded = 0
  const upstream = http.createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      forwarded++
      if (replyStatus === 503) {
        res.writeHead(503)
        res.end('{}')
      } else {
        pendingReply = res
        res.writeHead(200, { 'content-type': 'application/json' })
        res.write('{"rate_by_service":')
        announcePending()
      }
    })
  })
  upstream.listen(8126, '127.0.0.1')
  await once(upstream, 'listening')
  t.after(() => new Promise((resolve, reject) => upstream.close(error => error ? reject(error) : resolve())))
  const intake = await intakeForTest(t, true)
  const wire = { ...expected, after: 0 }

  assert.equal(await request(), 503)
  assert.equal(intake.records.traceRequests[0].outcome, 'upstream-error')
  assert.equal(intake.records.traceRequests[0].forwarded, true)
  assert.deepEqual(acknowledgedRequests(intake.records.traceRequests, ids, wire), [])
  assert.deepEqual(intake.records.errors, ['Agent /v0.4/traces: HTTP 503'])

  intake.setTraceFault('http503')
  assert.equal(await request(), 503)
  assert.equal(forwarded, 1, 'injected failures must not reach the Agent')
  assert.equal(intake.records.traceRequests[1].forwarded, false)
  intake.setTraceFault('none')
  replyStatus = 200
  const pending = new Promise(resolve => { announcePending = resolve })
  const response = request()
  await pending
  assert.equal(intake.records.traceRequests[2].completedAt, null)
  assert.equal(intake.records.traceRequests[2].outcome, null)
  assert.deepEqual(acknowledgedRequests(intake.records.traceRequests, ids, wire), [])
  pendingReply.end('{}}')
  assert.equal(await response, 200)
  const acknowledgements = acknowledgedRequests(intake.records.traceRequests, ids, wire)
  assert.equal(acknowledgements.length, 1)
  assert.equal(acknowledgements[0].sequence, 3)
  assert.equal(acknowledgements[0].forwarded, true)
  assert.ok(acknowledgements[0].completedAt >= acknowledgements[0].receivedAt)
})

test('HTTP checks reject duplicate instrumentation but accept retransmitted span IDs', () => {
  const root = { traceId: '100', spanId: '100' }
  const client = { traceId: '100', spanId: '101' }
  const server = { traceId: '100', spanId: '102' }
  const url = 'http://127.0.0.1:8080/feature/echo'
  const records = [
    { trace_id: '100', span_id: '100', parent_id: '0', name: 'verify.js.http', meta: {} },
    { trace_id: '100', span_id: '101', parent_id: '100', name: 'http.request',
      meta: { component: 'http', 'span.kind': 'client', 'http.url': url, 'http.status_code': '200' } },
    { trace_id: '100', span_id: '102', parent_id: '101', name: 'express.request',
      meta: { component: 'express', 'span.kind': 'server', 'http.url': url, 'http.status_code': '200' } }
  ]
  const features = { http: { root, echo: { client, status: 200, body: { server } } } }
  const passes = spans => evaluateFeatures({ features, spans, runtimeId: 'runtime', service: 'service' })
    .http_incoming_and_outgoing
  assert.equal(passes(records), true)
  assert.equal(passes([...records, { ...records[1] }]), true)
  assert.equal(passes([...records, { ...records[2] }]), true)
  assert.equal(passes([...records, { ...records[1], span_id: '103' }]), false)
  assert.equal(passes([...records, { ...records[2], span_id: '104' }]), false)
})
