'use strict'

const http = require('node:http')
const dgram = require('node:dgram')
const { gunzipSync } = require('node:zlib')
const { decode } = require('@msgpack/msgpack')

// Decode uint64 IDs as BigInt, never through a lossy JS Number.
function normalize (value) {
  if (typeof value === 'bigint') return value.toString()
  if (Array.isArray(value)) return value.map(normalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalize(item)]))
  }
  return value
}

function startIntake () {
  const records = { spans: [], traceRequests: [], telemetry: [], remoteConfig: [], metrics: [], deliveries: [], errors: [] }
  const forward = process.env.VERIFY_FORWARD_TO_AGENT === 'true'
  let traceFault = 'none'
  function setTraceFault (mode) {
    if (!['none', 'http503', 'disconnect'].includes(mode)) throw new Error(`Unknown trace fault: ${mode}`)
    traceFault = mode
  }
  const server = http.createServer(async (req, res) => {
    const traceRequest = req.url === '/v0.4/traces'
      ? {
          sequence: records.traceRequests.length + 1, receivedAt: Date.now(), completedAt: null,
          status: null, outcome: null, forwarded: false, bytes: 0, spans: []
        }
      : null
    if (traceRequest) records.traceRequests.push(traceRequest)
    const fault = traceFault
    const complete = (status, outcome) => {
      if (!traceRequest || traceRequest.completedAt !== null) return
      Object.assign(traceRequest, { status, outcome, completedAt: Date.now() })
    }
    const chunks = []
    try {
      for await (const chunk of req) chunks.push(chunk)
      const raw = Buffer.concat(chunks)
      if (traceRequest) traceRequest.bytes = raw.length
      const body = req.headers['content-encoding'] === 'gzip' ? gunzipSync(raw) : raw
      if (traceRequest) {
        const traces = normalize(decode(body, { useBigInt64: true }))
        for (const trace of traces) {
          for (const span of trace) {
            for (const key of ['trace_id', 'span_id', 'parent_id']) span[key] = String(span[key])
            records.spans.push(span)
            traceRequest.spans.push(span)
          }
        }
        if (fault === 'http503') {
          res.once('finish', () => complete(503, 'injected-http503'))
          res.writeHead(503, { 'content-type': 'application/json' })
          res.end('{"error":"injected trace intake outage"}')
          return
        }
        if (fault === 'disconnect') {
          req.socket.once('close', () => complete(null, 'injected-disconnect'))
          req.socket.destroy()
          return
        }
      } else if (req.url.includes('apmtelemetry') && body.length) {
        records.telemetry.push({ body: JSON.parse(body), sessionId: req.headers['dd-session-id'] })
      } else if (req.url === '/v0.7/config' && body.length) {
        records.remoteConfig.push(JSON.parse(body))
      } else if (req.url === '/dogstatsd/v2/proxy') {
        records.metrics.push(...body.toString().trim().split('\n').filter(Boolean))
      } else if (/^\/v[\d.]+\/traces$/.test(req.url)) {
        throw new Error(`Expected v0.4 trace payload, received ${req.url}`)
      }
      if (forward) {
        const headers = Object.fromEntries(Object.entries(req.headers).filter(([key]) =>
          !['host', 'connection', 'content-length', 'transfer-encoding'].includes(key)))
        let settled = false
        const fail = error => {
          if (settled) return
          settled = true
          records.errors.push(`Agent relay: ${error.message}`)
          complete(502, 'upstream-error')
          if (!res.headersSent) res.writeHead(502)
          res.end('{}')
        }
        const upstream = http.request({
          host: '127.0.0.1', port: 8126, method: req.method, path: req.url, headers,
          timeout: 5000
        }, reply => {
          const parts = []
          reply.on('data', part => parts.push(part))
          reply.on('error', fail)
          reply.on('aborted', () => fail(new Error('Agent response aborted')))
          reply.on('end', () => {
            if (settled) return
            settled = true
            const accepted = reply.statusCode >= 200 && reply.statusCode < 300
            records.deliveries.push({ path: req.url, status: reply.statusCode })
            complete(reply.statusCode, accepted ? 'accepted' : 'upstream-error')
            // RC availability is diagnostic; trace acknowledgement requires 2xx.
            if ((traceRequest && !accepted) || (reply.statusCode >= 400 && req.url !== '/v0.7/config')) {
              records.errors.push(`Agent ${req.url}: HTTP ${reply.statusCode}`)
            }
            res.writeHead(reply.statusCode, { 'content-type': reply.headers['content-type'] || 'application/json' })
            res.end(Buffer.concat(parts))
          })
        })
        upstream.on('finish', () => { if (traceRequest) traceRequest.forwarded = true })
        upstream.on('timeout', () => upstream.destroy(new Error('Agent relay timeout')))
        upstream.on('error', fail)
        upstream.end(raw)
        return
      }
      res.once('finish', () => complete(200, 'accepted'))
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify(req.url === '/info'
        ? { endpoints: ['/v0.4/traces', '/v0.7/config', '/dogstatsd/v2/proxy', '/telemetry/proxy/api/v2/apmtelemetry'] }
        : traceRequest ? { rate_by_service: {} } : {}))
    } catch (error) {
      records.errors.push(error.message)
      res.once('finish', () => complete(400, 'upstream-error'))
      if (!res.headersSent) res.writeHead(400)
      res.end('{}')
    }
  })
  const udp = dgram.createSocket('udp4')
  const relay = dgram.createSocket('udp4')
  udp.on('message', packet => {
    records.metrics.push(...packet.toString().trim().split('\n').filter(Boolean))
    if (forward) relay.send(packet, 8125, '127.0.0.1', error => {
      if (error) records.errors.push(`DogStatsD relay: ${error.message}`)
    })
  })
  udp.on('error', error => records.errors.push(error.message))
  server.listen(18126, '127.0.0.1')
  udp.bind(18125, '127.0.0.1')
  return { records, server, udp, relay, setTraceFault, forwardToAgent: forward }
}

module.exports = { startIntake }
