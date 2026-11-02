'use strict';

// The caller initializes dd-trace before loading this module or node:http.
const http = require('node:http');

function ids(span) {
  if (!span) return null;
  const context = span.context();
  return { traceId: context.toTraceId(), spanId: context.toSpanId() };
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function get({ tracer, baseUrl, path }) {
  return new Promise((resolve, reject) => {
    let client = null;
    let traceparent = null;
    const request = http.get(new URL(path, baseUrl), response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('error', reject);
      response.on('end', () => {
        try {
          resolve({ status: response.statusCode, body: JSON.parse(body), client, traceparent });
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.setTimeout(10000, () => request.destroy(new Error(`Feature request timed out: ${path}`)));

    // These headers belong to the automatically-created client span. Never
    // inject headers ourselves: doing so would hide broken HTTP instrumentation.
    const headers = request.getHeaders();
    const context = tracer.extract('http_headers', headers);
    client = context && { traceId: context.toTraceId(), spanId: context.toSpanId() };
    traceparent = headers.traceparent || null;
  });
}

async function runFeatures({ tracer, logger, baseUrl }) {
  const features = { roots: {}, rootSpans: [] };
  const active = () => ids(tracer.scope().active());
  const root = (key, name, callback) => {
    features.roots[key] = name;
    return tracer.trace(name, {
      childOf: null,
      resource: name,
      tags: { 'verify.feature': key, '_dd.measured': 1 },
    }, span => {
      features.rootSpans.push({ name, ...ids(span) });
      return callback(span);
    });
  };

  await root('manual', 'verify.js.manual', async span => {
    const manual = features.manual = { root: ids(span) };
    span.setTag('verify.tag', 'manual-root');
    span.setTag('verify.numeric', 42.5);
    await tracer.trace('verify.js.manual.child', { resource: 'manual-child' }, async child => {
      manual.child = ids(child);
      manual.childBefore = active();
      features.log = { marker: `verify.js.log.${child.context().toSpanId()}`, ...ids(child) };
      // Public log injection describes the expected Pino correlation encoding,
      // including the configured 128-bit trace ID logging representation.
      const carrier = {};
      tracer.inject(child.context(), 'log', carrier);
      features.log.expected = carrier.dd;
      logger.info({ marker: features.log.marker }, 'Tracer feature log correlation');
      await Promise.resolve();
      await tracer.trace('verify.js.manual.grandchild', { resource: 'manual-grandchild' }, grandchild => {
        manual.grandchild = ids(grandchild);
        grandchild.setTag('verify.tag', 'nested-grandchild');
      });
      manual.childAfter = active();
    });
    manual.restored = active();
  });

  const message = 'verify.js expected callback failure';
  features.errorOperation = 'verify.js.expected_error';
  features.expectedError = { message, caught: null };
  try {
    await root('expectedError', features.errorOperation, async span => {
      features.expectedError.span = ids(span);
      await Promise.resolve();
      // trace() must record this rejected callback, not an explicit error tag.
      throw new Error(message);
    });
  } catch (error) {
    features.expectedError.caught = error.message;
  }

  await root('http', 'verify.js.http', async span => {
    const evidence = features.http = { root: ids(span) };
    evidence.echo = await get({ tracer, baseUrl, path: '/feature/echo' });
    evidence.afterEcho = active();
    evidence.error = await get({ tracer, baseUrl, path: '/feature/error' });
    evidence.afterError = active();
    evidence.notFound = await get({ tracer, baseUrl, path: '/feature/not-found' });
    evidence.afterNotFound = active();
  });

  await root('async', 'verify.js.async', async span => {
    const evidence = features.async = { root: ids(span), tasks: [] };
    let entered = 0;
    let release;
    const bothEntered = new Promise(resolve => { release = resolve; });
    evidence.tasks = await Promise.all([0, 1].map(index => tracer.trace(
      `verify.js.async.task.${index}`, { resource: `async-task-${index}` }, async task => {
        const taskEvidence = { index, span: ids(task), before: active() };
        if (++entered === 2) release();
        await bothEntered;
        await delay(index === 0 ? 12 : 3);
        taskEvidence.afterAwait = active();
        await tracer.trace(`verify.js.async.leaf.${index}`, {}, leaf => {
          taskEvidence.leaf = ids(leaf);
        });
        taskEvidence.restored = active();
        return taskEvidence;
      },
    )));
    evidence.restored = active();
  });

  features.concurrent = await Promise.all([0, 1, 2, 3].map(index => root(
    `concurrent${index}`, `verify.js.concurrent.${index}`, async span => {
      const evidence = { index, root: ids(span), before: active() };
      evidence.response = await get({ tracer, baseUrl, path: '/feature/echo' });
      evidence.after = active();
      return evidence;
    },
  )));

  return features;
}

module.exports = { runFeatures };
