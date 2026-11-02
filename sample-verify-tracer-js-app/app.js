'use strict'

// Set defaults before loading dd-trace. AWS_LAMBDA_MICROVM_IMAGE_ARN must come
// from the platform (or the explicitly labeled local simulation), never here.
const defaults = {
  DD_SERVICE: 'sample-verify-tracer-js-app',
  DD_ENV: 'devmicrovm',
  DD_TRACE_AGENT_URL: 'http://127.0.0.1:18126',
  DD_DOGSTATSD_PORT: '18125',
  DD_TRACE_SAMPLE_RATE: '1',
  // The 128-ID clone probe must not exhaust the default 100 traces/s limiter.
  DD_TRACE_RATE_LIMIT: '1000',
  DD_LOGS_INJECTION: 'true',
  DD_INSTRUMENTATION_TELEMETRY_ENABLED: 'true',
  DD_RUNTIME_METRICS_ENABLED: 'true',
  DD_RUNTIME_METRICS_RUNTIME_ID_ENABLED: 'true',
  DD_REMOTE_CONFIGURATION_ENABLED: 'true',
  DD_REMOTE_CONFIG_POLL_INTERVAL_SECONDS: '1',
  DD_TELEMETRY_HEARTBEAT_INTERVAL: '1',
  DD_TRACE_STARTUP_LOGS: 'false'
}
for (const [key, value] of Object.entries(defaults)) process.env[key] ??= value
const tracer = require('dd-trace').init({ protocolVersion: '0.4' })
tracer.use('http', {
  server: { blocklist: [/^\/health$/, /^\/state$/, /^\/verify$/, /^\/delivery(?:\/|$)/, /^\/info$/, /^\/v0\.[47]\//, /^\/telemetry\//, /^\/dogstatsd\//] },
  client: { blocklist: [/^http:\/\/127\.0\.0\.1:(18126|8126)\//, /^http:\/\/127\.0\.0\.1:\d+\/(?:health|delivery)(?:[/?]|$)/] }
})
const express = require('express')
const pino = require('pino')
const { startIntake } = require('./intake')
const { runFeatures } = require('./features')
const { evaluateFeatures } = require('./feature-checks')
const { createDeliveryController } = require('./delivery')
// Intentional read/flush probes pinned to 6.15.0, not replacements for tracer behavior.
const getConfig = require('dd-trace/packages/dd-trace/src/config')
const runtimeMetrics = require('dd-trace/packages/dd-trace/src/runtime_metrics')
const { IS_AWS_LAMBDA_MICROVM, IS_SERVERLESS, NODE_BUNDLES_OPENSSL } = require('dd-trace/packages/dd-trace/src/serverless')
const version = require('dd-trace/package.json').version
const intake = startIntake()
const logs = []
const logger = pino({ name: 'sample-verify-tracer-js-app' }, {
  write (line) {
    logs.push(JSON.parse(line))
    process.stdout.write(line)
  }
})
const app = express()
app.use(express.json())
const BASE = '/aws/lambda-microvms/runtime/v1'
const port = Number(process.env.PORT || 8080)
const state = {
  prepared: false, runCount: 0, resumeCount: 0, suspendCount: 0, terminateCount: 0,
  bundledOpenSSL: NODE_BUNDLES_OPENSSL, microvm: IS_AWS_LAMBDA_MICROVM, serverless: IS_SERVERLESS,
  before: null, run: null, runIds: [], verificationCount: 0, manualFlushCount: 0
}
let preparing
let activeWork = null

function identity () {
  const tags = getConfig().tags
  return { runtimeId: tags['runtime-id'], rcClientId: tags['_dd.rc.client_id'] || null }
}
function spanIdentity (span) {
  return { traceId: span.context().toTraceId(), spanId: span.context().toSpanId() }
}
function snapshotState () {
  return { ...state, ...identity(), version }
}
async function flush () {
  state.manualFlushCount++
  await new Promise(resolve => tracer._tracer._exporter.flush(resolve))
  await new Promise(resolve => runtimeMetrics.flush(resolve))
  await new Promise(resolve => tracer.dogstatsd.flush(resolve))
}
async function prepare () {
  state.before = identity()
  // Warm the tracer's 8192-ID cache and Node's UUID cache before the snapshot.
  tracer.trace('verify.js.build.warmup', { childOf: null }, () => {})
  tracer.dogstatsd.gauge('microvm.js.build_warmup', 1)
  await flush()
  state.prepared = true
}

const delivery = createDeliveryController({
  tracer, intake, logger, getConfig, identity, snapshotState, state, baseUrl: `http://127.0.0.1:${port}`
})
function workConflict (res) {
  if (!state.prepared || !state.run) {
    res.status(409).json({ error: 'Expected platform ready then run hooks' })
    return true
  }
  if (activeWork) {
    res.status(409).json({ error: `Work already running: ${activeWork}` })
    return true
  }
  return false
}

for (const scenario of ['automatic', 'recovery', 'termination']) {
  app.post(`/delivery/${scenario}`, async (req, res, next) => {
    if (workConflict(res)) return
    if (delivery.armed && scenario !== 'termination') {
      return res.status(409).json({ error: 'Final termination batch already armed' })
    }
    activeWork = `delivery/${scenario}`
    try {
      res.json(scenario === 'termination' ? delivery.armTermination() : await delivery[scenario]())
    } catch (error) { next(error) } finally { activeWork = null }
  })
}

app.get('/health', (req, res) => res.json({ status: 'ready', version, ...identity() }))
app.get('/state', (req, res) => res.json(snapshotState()))
app.get('/feature/echo', (req, res) => res.json({
  server: spanIdentity(tracer.scope().active()), traceparent: req.headers.traceparent, runtimeId: identity().runtimeId
}))
app.get('/feature/error', () => { throw new Error('microvm verification expected HTTP error') })
app.get('/feature/not-found', (req, res) => res.status(404).json({ error: 'expected missing resource' }))
app.post(`${BASE}/ready`, async (req, res, next) => {
  try {
    preparing ??= prepare()
    await preparing
    res.json({ prepared: state.prepared, before: state.before })
  } catch (error) { next(error) }
})
app.post(`${BASE}/run`, (req, res) => {
  state.runCount++
  state.run = identity()
  // No refresh call here: the real dd-trace HTTP subscriber must run first.
  // These are the first application-created IDs after /run, before any features.
  state.runIds = Array.from({ length: 128 }, () => {
    const span = tracer.startSpan('verify.js.run.identity', { childOf: null })
    const ids = spanIdentity(span)
    span.finish()
    return ids
  })
  res.json({ ...state.run, runCount: state.runCount })
})
for (const hook of ['validate', 'resume', 'suspend', 'terminate']) {
  app.post(`${BASE}/${hook}`, async (req, res, next) => {
    if (activeWork) return res.status(409).json({ error: `Work already running: ${activeWork}` })
    activeWork = `hook/${hook}`
    try {
      if (hook !== 'validate') state[`${hook}Count`]++
      if (hook === 'suspend') await flush()
      if (hook === 'terminate' && delivery.armed) {
        // The forwarded hook must return only after the native tracer timer
        // reaches the Agent. Native Agent flushing happens after this response.
        return res.json(await delivery.terminate())
      }
      res.json({ status: 'ok', ...identity() })
    } catch (error) { next(error) } finally { activeWork = null }
  })
}
// Lookalike routes are used by local negative controls only.
app.get(`${BASE}/run`, (req, res) => res.json(identity()))
app.post(`${BASE}/run-extra`, (req, res) => res.json(identity()))

app.post('/verify', async (req, res, next) => {
  if (workConflict(res)) return
  if (delivery.armed) return res.status(409).json({ error: 'Final termination batch already armed' })
  activeWork = 'verify'
  try {
    state.verificationCount++
    const offsets = Object.fromEntries(['spans', 'metrics', 'telemetry', 'remoteConfig', 'errors'].map(key => [key, intake.records[key].length]))
    const features = await runFeatures({ tracer, logger, baseUrl: `http://127.0.0.1:${port}` })
    const after = identity()
    tracer.dogstatsd.gauge('microvm.js.fresh', 42, { verification: String(state.verificationCount) })
    await flush()
    const spans = intake.records.spans.slice(offsets.spans)
    const metrics = intake.records.metrics.slice(offsets.metrics)
    const capturedTelemetry = intake.records.telemetry.slice(offsets.telemetry)
    const remoteConfig = intake.records.remoteConfig.slice(offsets.remoteConfig)
    const errors = intake.records.errors.slice(offsets.errors)
    const metricTags = line => (line.split('|').find(part => part.startsWith('#')) || '#').slice(1).split(',')
    const tagged = line => metricTags(line).includes(`runtime-id:${after.runtimeId}`) &&
      metricTags(line).includes(`service:${getConfig().service}`)
    const rootLog = logs.find(log => log.marker === features.log.marker)
    const logTrace = rootLog?.dd?.trace_id
    // Pino emits a 128-bit hex trace ID when the upper bits exist; compare low64 exactly.
    const logLow64 = logTrace && (/^[0-9a-f]{32}$/i.test(logTrace) ? BigInt(`0x${logTrace.slice(-16)}`).toString() : logTrace)
    const checks = {
      pinned_version: version === '6.15.0',
      microvm_serverless_detected: IS_AWS_LAMBDA_MICROVM && IS_SERVERLESS,
      runtime_identity_refreshed_before_handler: Boolean(state.before.runtimeId) && state.before.runtimeId !== state.run.runtimeId,
      identity_stable_after_run: after.runtimeId === state.run.runtimeId && after.rcClientId === state.run.rcClientId,
      run_called_once: state.runCount === 1,
      fresh_runtime_metrics_identity: metrics.some(line => line.startsWith('runtime.node.mem.rss:') && tagged(line)),
      fresh_custom_metric_identity: metrics.some(line => line.startsWith('microvm.js.fresh:42|g') && tagged(line)),
      telemetry_disabled_in_serverless: getConfig().telemetry.DD_INSTRUMENTATION_TELEMETRY_ENABLED === false,
      remote_config_disabled_in_serverless: getConfig().remoteConfig.DD_REMOTE_CONFIGURATION_ENABLED === false && after.rcClientId === null,
      no_disabled_pipeline_requests: intake.records.telemetry.length === 0 && intake.records.remoteConfig.length === 0,
      pino_log_correlation: logLow64 === features.log.traceId && rootLog?.dd?.span_id === features.log.spanId && rootLog?.dd?.service === getConfig().service,
      intake_delivery_succeeded: errors.length === 0,
      ...evaluateFeatures({ features, spans, runtimeId: after.runtimeId, service: getConfig().service })
    }
    const result = {
      schema: 2, version, passed: Object.values(checks).every(Boolean), checks,
      evidence: { before: state.before, run: state.run, after,
        ids: [...(state.verificationCount === 1 ? state.runIds : []), ...features.rootSpans],
        spans, features,
        metrics, telemetry: capturedTelemetry, remoteConfig, errors, log: rootLog },
      state: snapshotState()
    }
    logger.info({ version: process.env.DD_VERSION || 'local', result: {
      schema: result.schema, passed: result.passed, checks, state: result.state,
      evidence: { before: state.before, run: state.run, after }
    } }, 'microvm_js_verification')
    res.json(result)
  } catch (error) { next(error) } finally { activeWork = null }
})
app.use((error, req, res, next) => {
  logger.error({ err: { message: error.message, stack: error.stack } }, 'verification request error')
  res.status(500).json({ error: error.message })
})
app.listen(port, '0.0.0.0', () => logger.info({ port, version, microvm: IS_AWS_LAMBDA_MICROVM }, 'verifier_listening'))
