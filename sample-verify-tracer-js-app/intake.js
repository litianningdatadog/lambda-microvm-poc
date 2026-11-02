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
  const records = { spans: [], telemetry: [], remoteConfig: [], metrics: [], deliveries: [], errors: [] }
  const forward = process.env.VERIFY_FORWARD_TO_AGENT === 'true'
  const server = http.createServer(async (req, res) => {
    const chunks = []
    try {
      for await (const chunk of req) chunks.push(chunk)
      const raw = Buffer.concat(chunks)
      const body = req.headers['content-encoding'] === 'gzip' ? gunzipSync(raw) : raw
      if (req.url === '/v0.4/traces') {
        const traces = normalize(decode(body, { useBigInt64: true }))
        for (const trace of traces) {
          for (const span of trace) {
            for (const key of ['trace_id', 'span_id', 'parent_id']) span[key] = String(span[key])
            records.spans.push(span)
          }
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
        const upstream = http.request({
          host: '127.0.0.1', port: 8126, method: req.method, path: req.url, headers,
          timeout: 5000
        }, reply => {
          const parts = []
          reply.on('data', part => parts.push(part))
          reply.on('end', () => {
            records.deliveries.push({ path: req.url, status: reply.statusCode })
            // RC availability is diagnostic; trace, metric and telemetry delivery must succeed.
            if (reply.statusCode >= 400 && req.url !== '/v0.7/config') {
              records.errors.push(`Agent ${req.url}: HTTP ${reply.statusCode}`)
            }
            res.writeHead(reply.statusCode, { 'content-type': reply.headers['content-type'] || 'application/json' })
            res.end(Buffer.concat(parts))
          })
        })
        upstream.on('timeout', () => upstream.destroy(new Error('Agent relay timeout')))
        upstream.on('error', error => {
          records.errors.push(`Agent relay: ${error.message}`)
          if (!res.headersSent) res.writeHead(502)
          res.end('{}')
        })
        upstream.end(raw)
        return
      }
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify(req.url === '/info'
        ? { endpoints: ['/v0.4/traces', '/v0.7/config', '/dogstatsd/v2/proxy', '/telemetry/proxy/api/v2/apmtelemetry'] }
        : req.url === '/v0.4/traces' ? { rate_by_service: {} } : {}))
    } catch (error) {
      records.errors.push(error.message)
      res.writeHead(400)
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
  return { records, server, udp, relay }
}

module.exports = { startIntake }
