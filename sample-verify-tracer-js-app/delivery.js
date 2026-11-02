'use strict'

const http = require('node:http')
const { randomUUID } = require('node:crypto')

const EXPORT_DEADLINE_MS = 10000
const RECOVERY_DEADLINE_MS = 20000
const RECOVERY_TRACES = 24
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

function matchingSpan (span, ids, expected) {
  return span.trace_id === ids?.traceId && span.span_id === ids?.spanId &&
    span.parent_id === '0' && span.name === expected.operation && span.service === expected.service &&
    span.meta?.['runtime-id'] === expected.runtimeId &&
    span.meta?.['verify.delivery.marker'] === expected.marker && span.metrics?.['_dd.measured'] === 1
}

// Receipt and decoded spans alone are not acknowledgement. Capture-only 2xx is
// intentionally distinguishable from a real Agent response in every result.
function acknowledgedRequests (records, ids, expected) {
  return records.filter(request => request.outcome === 'accepted' &&
    request.status >= 200 && request.status < 300 &&
    Number.isFinite(request.completedAt) && request.completedAt >= request.receivedAt &&
    request.receivedAt >= (expected.after || 0) &&
    (!expected.forwardToAgent || request.forwarded === true) &&
    request.spans.some(span => matchingSpan(span, ids, expected)))
}

async function observe (predicate, deadline, sample = () => {}) {
  while (true) {
    sample()
    if (predicate()) return true
    const remaining = deadline - Date.now()
    if (remaining <= 0) return false
    await delay(Math.min(25, remaining))
  }
}

function healthProbe (baseUrl, timeout) {
  const at = Date.now()
  return new Promise(resolve => {
    let settled = false
    const finish = result => {
      if (settled) return
      settled = true
      resolve({ at, ...result, elapsedMs: Date.now() - at })
    }
    const request = http.get(new URL('/health', baseUrl), response => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', chunk => { body += chunk })
      response.on('error', error => finish({ status: response.statusCode, error: error.message }))
      response.on('end', () => {
        try {
          finish({ status: response.statusCode, runtimeId: JSON.parse(body).runtimeId })
        } catch (error) {
          finish({ status: response.statusCode, error: `Invalid health response: ${error.message}` })
        }
      })
    })
    request.on('error', error => finish({ status: null, error: error.message }))
    request.setTimeout(timeout, () => request.destroy(new Error('Health probe timed out')))
  })
}

function createDeliveryController ({ tracer, intake, logger, getConfig, identity, snapshotState, state, baseUrl }) {
  let armed = null
  let finalizing = null
  const flushInterval = () => getConfig().flushInterval
  const expected = (operation, marker, runtimeId, after = 0) => ({
    operation, marker, runtimeId, after, service: getConfig().service, forwardToAgent: intake.forwardToAgent
  })
  const requests = () => intake.records.traceRequests
  const stable = runtimeId => Boolean(runtimeId) && runtimeId === state.run?.runtimeId && runtimeId === identity().runtimeId
  const root = (operation, marker, onSpan) => tracer.trace(operation, {
    childOf: null, resource: operation, tags: { 'verify.delivery.marker': marker, '_dd.measured': 1 }
  }, span => {
    const ids = { traceId: span.context().toTraceId(), spanId: span.context().toSpanId() }
    if (onSpan) onSpan(ids)
    return ids
  })
  // Pinned dd-trace 6.15.0 AgentWriter._encoder.count() reads queued traces;
  // makePayload() resets it before transport/retry. This is not an in-flight
  // request or a global memory-bound probe, and it never mutates the writer.
  const encoderCount = () => tracer._tracer._exporter._writer._encoder.count()
  const result = (scenario, checks, evidence) => ({
    schema: 2, scenario, passed: Object.values(checks).every(Boolean), checks, evidence
  })

  async function automatic () {
    const started = Date.now()
    const marker = randomUUID()
    const operation = 'verify.js.automatic'
    const runtimeId = identity().runtimeId
    const manualFlushCountBefore = state.manualFlushCount
    const span = root(operation, marker)
    const wire = expected(operation, marker, runtimeId, started)
    await observe(() => acknowledgedRequests(requests(), span, wire).length > 0, started + EXPORT_DEADLINE_MS)
    const successfulRequests = acknowledgedRequests(requests(), span, wire)
    const elapsedMs = Date.now() - started
    const evidence = {
      marker, runtimeId, operation, span, forwardToAgent: intake.forwardToAgent,
      flushIntervalMs: flushInterval(), manualFlushCountBefore, manualFlushCountAfter: state.manualFlushCount,
      elapsedMs, deadlineMs: EXPORT_DEADLINE_MS, successfulRequests
    }
    return result('automatic', {
      automatic_default_interval: evidence.flushIntervalMs === 2000,
      automatic_no_manual_flush: evidence.manualFlushCountBefore === evidence.manualFlushCountAfter,
      automatic_trace_acknowledged: successfulRequests.length > 0,
      automatic_identity: stable(runtimeId) && successfulRequests.length > 0,
      automatic_deadline: successfulRequests.length > 0 && successfulRequests.some(request =>
        request.completedAt - started <= EXPORT_DEADLINE_MS)
    }, evidence)
  }

  async function recoveryCase (mode, runtimeId) {
    const started = Date.now()
    const deadline = started + RECOVERY_DEADLINE_MS
    const marker = randomUUID()
    const operation = `verify.js.recovery.${mode}`
    const errorOffset = intake.records.errors.length
    const sample = () => evidence.encoderCounts.push({ at: Date.now(), count: encoderCount() })
    const evidence = {
      mode, marker, operation, startedAt: started, generated: 0, offeredSpans: [], freshSpan: null, freshMarker: null,
      baselineEncoderCount: null, baselineDrained: false, baselineEncoderCounts: [],
      faultActiveAt: null, restoredAt: null, freshCreatedAt: null,
      failureAttempts: [], freshAcknowledgements: [], probes: [], encoderCounts: [], encoderPeak: 0,
      queueDrained: false, elapsedMs: 0, deadlineMs: RECOVERY_DEADLINE_MS, unexpectedErrors: [], errors: []
    }
    const failed = () => requests().filter(request =>
      request.outcome === `injected-${mode}` &&
      request.forwarded === false && request.completedAt !== null &&
      request.receivedAt >= evidence.faultActiveAt &&
      request.spans.some(span => evidence.offeredSpans.some(ids => matchingSpan(span, ids,
        expected(operation, marker, runtimeId)))))
    try {
      evidence.baselineDrained = await observe(() => encoderCount() === 0, Math.min(deadline, started + 5000),
        () => evidence.baselineEncoderCounts.push({ at: Date.now(), count: encoderCount() }))
      evidence.baselineEncoderCount = encoderCount()
      sample()
      if (!evidence.baselineDrained) {
        evidence.errors.push('Native encoder did not drain before the fault phase')
      } else {
        intake.setTraceFault(mode)
        evidence.faultActiveAt = Date.now()
        for (let index = 0; index < RECOVERY_TRACES; index++) evidence.offeredSpans.push(root(operation, marker))
        evidence.generated = evidence.offeredSpans.length
        sample()
        for (let index = 0; index < 3; index++) {
          evidence.probes.push(await healthProbe(baseUrl, Math.max(1, Math.min(1000, deadline - Date.now()))))
          sample()
        }
        await observe(() => failed().length > 0, Math.min(deadline, Date.now() + 7000), sample)
      }
    } catch (error) {
      evidence.errors.push(`${error.name}: ${error.message}`)
    } finally {
      intake.setTraceFault('none')
      evidence.restoredAt = Date.now()
    }
    evidence.failureAttempts = failed()
    evidence.freshMarker = randomUUID()
    evidence.freshCreatedAt = Date.now()
    evidence.freshSpan = root(operation, evidence.freshMarker)
    sample()
    const freshExpected = expected(operation, evidence.freshMarker, runtimeId, evidence.freshCreatedAt)
    await observe(() => acknowledgedRequests(requests(), evidence.freshSpan, freshExpected).length > 0 &&
      encoderCount() === 0, deadline, sample)
    evidence.freshAcknowledgements = acknowledgedRequests(requests(), evidence.freshSpan, freshExpected)
    evidence.queueDrained = encoderCount() === 0
    evidence.encoderPeak = Math.max(...evidence.encoderCounts.map(item => item.count))
    evidence.unexpectedErrors = intake.records.errors.slice(errorOffset)
    evidence.elapsedMs = Date.now() - started
    return evidence
  }

  async function recovery () {
    const runtimeId = identity().runtimeId
    const manualFlushCountBefore = state.manualFlushCount
    const errorOffset = intake.records.errors.length
    const cases = []
    // Native retries may drop the rejected payload or replay its IDs. Neither
    // outcome creates more spans here; recovery is proved by NEW accepted work.
    for (const mode of ['http503', 'disconnect']) cases.push(await recoveryCase(mode, runtimeId))
    const evidence = {
      runtimeId, forwardToAgent: intake.forwardToAgent, flushIntervalMs: flushInterval(),
      manualFlushCountBefore, manualFlushCountAfter: state.manualFlushCount,
      unexpectedErrors: intake.records.errors.slice(errorOffset), cases
    }
    return result('recovery', {
      recovery_both_failures_observed: cases.every(item => item.failureAttempts.length > 0),
      recovery_application_responsive: cases.every(item => item.probes.length >= 3 && item.probes.every(probe =>
        probe.status === 200 && probe.elapsedMs < 1000 && probe.at >= item.faultActiveAt &&
        probe.at + probe.elapsedMs <= item.restoredAt)),
      recovery_encoder_bounded: cases.every(item => item.baselineDrained && item.generated === RECOVERY_TRACES &&
        item.encoderCounts.every(sample => Number.isInteger(sample.count) && sample.count >= 0) &&
        item.encoderPeak <= item.baselineEncoderCount + item.generated + 1),
      recovery_queue_drained: cases.every(item => item.baselineDrained && item.queueDrained),
      recovery_fresh_delivery: cases.every(item => item.freshAcknowledgements.length > 0 &&
        item.freshAcknowledgements.some(request => request.completedAt <= item.startedAt + item.deadlineMs)),
      recovery_identity_stable: stable(runtimeId) && cases.every(item => item.probes.length >= 3 &&
        item.probes.every(probe => probe.runtimeId === runtimeId) && item.freshAcknowledgements.length > 0),
      recovery_no_manual_flush: manualFlushCountBefore === state.manualFlushCount,
      recovery_no_unexpected_errors: evidence.unexpectedErrors.length === 0 && cases.every(item =>
        item.unexpectedErrors.length === 0 && item.errors.length === 0)
    }, evidence)
  }

  function armTermination () {
    armed ??= {
      schema: 2, scenario: 'termination-armed', armed: true, marker: randomUUID(),
      runtimeId: identity().runtimeId, operation: 'verify.js.termination'
    }
    return armed
  }

  async function finalizeTermination () {
    const started = Date.now()
    const manualFlushCountBefore = state.manualFlushCount
    const span = root(armed.operation, armed.marker, ids => {
      logger.info({ version: process.env.DD_VERSION || 'local', marker: armed.marker,
        runtimeId: armed.runtimeId, ...ids }, 'microvm_js_termination_log')
    })
    const wire = expected(armed.operation, armed.marker, armed.runtimeId, started)
    await observe(() => acknowledgedRequests(requests(), span, wire).length > 0, started + EXPORT_DEADLINE_MS)
    const successfulRequests = acknowledgedRequests(requests(), span, wire)
    const evidence = {
      span, runtimeId: armed.runtimeId, operation: armed.operation, forwardToAgent: intake.forwardToAgent,
      manualFlushCountBefore, manualFlushCountAfter: state.manualFlushCount, flushIntervalMs: flushInterval(),
      elapsedMs: Date.now() - started, deadlineMs: EXPORT_DEADLINE_MS, successfulRequests
    }
    const final = {
      ...result('termination', {
        termination_no_manual_flush: evidence.manualFlushCountBefore === evidence.manualFlushCountAfter,
        termination_trace_acknowledged: successfulRequests.length > 0,
        termination_identity: stable(armed.runtimeId) && successfulRequests.length > 0,
        termination_deadline: successfulRequests.some(request => request.completedAt - started <= EXPORT_DEADLINE_MS),
        termination_hook_once: state.terminateCount === 1
      }, evidence),
      marker: armed.marker, state: snapshotState()
    }
    logger.info({ version: process.env.DD_VERSION || 'local', result: final }, 'microvm_js_termination_result')
    return final
  }

  return {
    automatic, recovery, armTermination,
    get armed () { return armed !== null },
    async terminate () {
      if (!armed) return null
      finalizing ??= finalizeTermination()
      const final = await finalizing
      if (state.terminateCount === 1) return final
      const duplicate = { ...final, passed: false, checks: { ...final.checks, termination_hook_once: false }, state: snapshotState() }
      logger.info({ version: process.env.DD_VERSION || 'local', result: duplicate }, 'microvm_js_termination_result')
      return duplicate
    }
  }
}

module.exports = { createDeliveryController, acknowledgedRequests }
