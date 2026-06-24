'use strict'

// Cross-clone RNG test for AWS Lambda MicroVMs.
//
// Goal: decide whether per-call `randomFillSync` (Node's bundled OpenSSL DRBG,
// the source used by dd-trace-js PR #8426) returns DISTINCT bytes across two
// MicroVM clones resumed from the same build snapshot. dd-trace's id generator
// depends on this property for trace/span ID uniqueness across clones.
//
// The app samples three sources side by side, with built-in controls:
//   batched         (positive control) -- dd-trace's default 8192-id buffer.
//                   Filled+frozen at build time, so clones of ONE snapshot MUST
//                   return identical bytes. Proves the harness detects a collision.
//   perCallOpenSSL  (the question)     -- per-call randomFillSync.
//   perCallKernel   (negative control) -- per-call /dev/urandom read. The kernel
//                   CSPRNG is reseeded per clone on resume, so it MUST differ.
//
// Each response carries BOTH groups:
//   buildSamples -- captured at module load, frozen INTO the snapshot. Acts as a
//                   per-snapshot fingerprint: two captures with identical
//                   buildSamples came from the same build/snapshot (the only
//                   pair for which the cross-clone comparison is valid). NOTE: a
//                   single image version has one Build per (arch, chipset), each
//                   with its own snapshot -- so clones on different builds differ.
//   runSamples   -- generated post-resume; the actual behavior under test.
//
// State is primed at module load, which runs at BUILD time (before /ready returns
// and the snapshot is captured), so the batched buffer and OpenSSL DRBG state are
// frozen into the snapshot.

const http = require('http')
const { randomFillSync } = require('crypto')
const { openSync, readSync } = require('fs')
const os = require('os')

const N = 16
const APP_PORT = Number(process.env.APP_PORT) || 8080
const HOOKS_PORT = Number(process.env.HOOKS_PORT) || 9000
const HOOK_PREFIX = '/aws/lambda-microvms/runtime/v1'

const hex = (b, o) => {
  let s = ''
  for (let i = 0; i < 8; i++) s += b[o + i].toString(16).padStart(2, '0')
  return s
}
const take = (fn, n) => {
  const a = []
  for (let i = 0; i < n; i++) a.push(fn())
  return a
}

// Microbenchmark a generator IN the MicroVM: warm up, time `iterations` calls
// across `trials`, drop best+worst, return mean ns/ID. Mirrors the laptop bench
// so in-MicroVM numbers are directly comparable. `sink` defeats dead-code elim.
const bench = (fn, iterations, trials) => {
  for (let i = 0; i < 50_000; i++) fn()
  const times = []
  for (let t = 0; t < trials; t++) {
    let sink = 0
    const start = process.hrtime.bigint()
    for (let i = 0; i < iterations; i++) sink ^= fn()[0]
    const end = process.hrtime.bigint()
    if (sink === -1) throw new Error('unreachable')
    times.push(Number(end - start) / iterations)
  }
  times.sort((a, b) => a - b)
  const kept = trials >= 3 ? times.slice(1, -1) : times
  return Number((kept.reduce((a, b) => a + b, 0) / kept.length).toFixed(2))
}

// Captured at process start (pre-snapshot). Answers: is AWS_LAMBDA_MICROVM_IMAGE_ARN
// set this early? (validates dd-trace's module-load gate assumption)
const envAtLoad = process.env.AWS_LAMBDA_MICROVM_IMAGE_ARN ?? null

// (1) batched path -- dd-trace's default 8192-id buffer
const data = new Uint8Array(8 * 8192)
let batch = 0
function batched () {
  if (batch === 0) randomFillSync(data)
  batch = (batch + 1) % 8192
  return hex(data, batch * 8)
}

// (2) per-call OpenSSL DRBG -- PR #8426's source
const obuf = new Uint8Array(8)
function perCallOpenSSL () {
  randomFillSync(obuf)
  return hex(obuf, 0)
}

// (3) per-call kernel /dev/urandom
const kbuf = new Uint8Array(8)
const fd = openSync('/dev/urandom', 'r')
function perCallKernel () {
  let off = 0
  while (off < 8) off += readSync(fd, kbuf, off, 8 - off, null)
  return hex(kbuf, 0)
}

// Prime + freeze state into the snapshot (runs at build, before /ready returns).
batched()                                       // fills the 8192 buffer once
for (let i = 0; i < 32; i++) perCallOpenSSL()    // establish OpenSSL DRBG state
const buildSamples = {
  batched: take(batched, N),
  perCallOpenSSL: take(perCallOpenSSL, N),
  perCallKernel: take(perCallKernel, N),
}

// --- Application server (APP_PORT) -----------------------------------------
http.createServer((req, res) => {
  if (req.url.startsWith('/ids')) {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      hostname: os.hostname(),
      envAtLoad,
      envAtRequest: process.env.AWS_LAMBDA_MICROVM_IMAGE_ARN ?? null,
      buildSamples,
      runSamples: {
        batched: take(batched, N),
        perCallOpenSSL: take(perCallOpenSSL, N),
        perCallKernel: take(perCallKernel, N),
      },
    }))
    return
  }
  if (req.url.startsWith('/bench')) {
    // Perf comparison of the ID-generation strategies, measured in-MicroVM.
    // Query: ?iterations=200000&trials=7
    const params = new URL(req.url, 'http://local').searchParams
    const iterations = Math.min(Number(params.get('iterations')) || 200_000, 5_000_000)
    const trials = Math.min(Number(params.get('trials')) || 7, 15)
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      node: process.version,
      arch: process.arch,
      cpu: os.cpus()[0]?.model,
      iterations,
      trials,
      nsPerId: {
        batched: bench(batched, iterations, trials),
        perCallOpenSSL: bench(perCallOpenSSL, iterations, trials),
        perCallKernel: bench(perCallKernel, iterations, trials),
      },
    }, null, 2))
    return
  }
  if (req.url.startsWith('/health')) {
    res.writeHead(200)
    res.end('ok')
    return
  }
  res.writeHead(404)
  res.end()
}).listen(APP_PORT, '0.0.0.0', () => console.log(`app on ${APP_PORT}`))

// --- Lifecycle hooks server (HOOKS_PORT) -----------------------------------
// Lambda POSTs to /aws/lambda-microvms/runtime/v1/<hook>. We bind 0.0.0.0 on a
// separate server so hooks answer even if the app server is busy.
http.createServer((req, res) => {
  req.resume()  // drain any body (/run and /resume may carry runHookPayload)
  const hook = req.url.startsWith(HOOK_PREFIX) ? req.url.slice(HOOK_PREFIX.length) : req.url

  switch (hook) {
    case '/ready':
      // Priming ran synchronously at module load, before this server began
      // listening -- so by the time /ready is reachable, the app is fully
      // initialized and it is safe to capture the snapshot. Return 200.
      res.writeHead(200)
      res.end()
      return

    case '/validate':
      // Runs on a throwaway test instance AFTER the snapshot is captured. Hit
      // /ids once so the app path is exercised (lets the platform prefetch it)
      // and the snapshot is smoke-tested end to end. This does NOT affect the
      // captured snapshot or the real clones.
      http.get({ host: '127.0.0.1', port: APP_PORT, path: '/ids' }, r => {
        r.resume()
        res.writeHead(r.statusCode === 200 ? 200 : 503)
        res.end()
      }).on('error', () => {
        res.writeHead(503)
        res.end()
      })
      return

    case '/run':
    case '/resume':
    case '/suspend':
    case '/terminate':
      // Intentionally NO reseed here. The test observes the RAW post-resume
      // behavior of each RNG source. Production code would regenerate per-VM
      // unique state on /run (per the AWS guidance), but doing so would mask
      // exactly what we are trying to measure.
      res.writeHead(200)
      res.end()
      return

    default:
      res.writeHead(404)
      res.end()
  }
}).listen(HOOKS_PORT, '0.0.0.0', () => console.log(`hooks on ${HOOKS_PORT}`))
