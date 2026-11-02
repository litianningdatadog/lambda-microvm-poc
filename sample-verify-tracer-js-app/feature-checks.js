'use strict';

function evaluateFeatures({ features, spans, runtimeId, service }) {
  const records = Array.isArray(spans) ? spans : [];
  const validIds = value => Boolean(value && /^[1-9][0-9]*$/.test(value.traceId) &&
    /^[1-9][0-9]*$/.test(value.spanId));
  const same = (left, right) => validIds(left) && validIds(right) &&
    left.traceId === right.traceId && left.spanId === right.spanId;
  const byId = new Map(records.map(span => [`${span.trace_id}:${span.span_id}`, span]));
  const find = value => validIds(value) ? byId.get(`${value.traceId}:${value.spanId}`) : undefined;
  const childOf = (child, parent) => {
    const span = find(child);
    return Boolean(span && validIds(parent) && span.trace_id === parent.traceId &&
      span.parent_id === parent.spanId);
  };
  const descendantOf = (child, ancestor) => {
    let span = find(child);
    for (let count = 0; span && count < records.length; count++) {
      if (span.trace_id === ancestor?.traceId && span.span_id === ancestor.spanId) return true;
      span = byId.get(`${span.trace_id}:${span.parent_id}`);
    }
    return false;
  };
  const ids = span => span && ({ traceId: span.trace_id, spanId: span.span_id });
  const meta = (span, key) => span?.meta?.[key];
  const status = (span, expected) => String(meta(span, 'http.status_code')) === String(expected);
  const isError = span => Number(span?.error) === 1;
  const hasErrorDetails = span => isError(span) && Boolean(meta(span, 'error.type')) &&
    Boolean(meta(span, 'error.message')) && Boolean(meta(span, 'error.stack'));
  const roots = features?.rootSpans || [];
  const rootNames = Object.values(features?.roots || {});
  const rootPresent = root => {
    const span = find(root);
    return Boolean(span && span.name === root.name && span.parent_id === '0');
  };

  const checks = {
    feature_roots_exported: roots.length === 8 && rootNames.length === roots.length &&
      roots.every(root => rootPresent(root) && rootNames.includes(root.name)),
    feature_root_identity: Boolean(runtimeId) && roots.length === 8 &&
      roots.every(root => meta(find(root), 'runtime-id') === runtimeId),
    feature_root_service: Boolean(service) && roots.length === 8 &&
      roots.every(root => find(root)?.service === service),
    feature_root_isolation: roots.length === 8 &&
      new Set(roots.map(root => root.traceId)).size === roots.length,
  };

  const manual = features?.manual;
  const manualRoot = find(manual?.root);
  const manualChild = find(manual?.child);
  const manualGrandchild = find(manual?.grandchild);
  checks.manual_hierarchy = childOf(manual?.child, manual?.root) &&
    childOf(manual?.grandchild, manual?.child) &&
    manualChild?.name === 'verify.js.manual.child' &&
    manualGrandchild?.name === 'verify.js.manual.grandchild';
  checks.manual_tags_and_resources = Boolean(manualRoot && manualChild && manualGrandchild) &&
    meta(manualRoot, 'verify.tag') === 'manual-root' &&
    manualRoot.metrics?.['verify.numeric'] === 42.5 &&
    manualRoot.resource === 'verify.js.manual' && manualChild.resource === 'manual-child' &&
    manualGrandchild.resource === 'manual-grandchild' &&
    meta(manualGrandchild, 'verify.tag') === 'nested-grandchild';
  checks.manual_scope_restoration = same(manual?.childBefore, manual?.child) &&
    same(manual?.childAfter, manual?.child) && same(manual?.restored, manual?.root) &&
    checks.manual_hierarchy;

  const expectedError = features?.expectedError;
  const errorSpan = find(expectedError?.span);
  checks.callback_error_exported = Boolean(errorSpan) &&
    errorSpan.name === features?.errorOperation && hasErrorDetails(errorSpan) &&
    meta(errorSpan, 'error.type') === 'Error' &&
    meta(errorSpan, 'error.message') === expectedError?.message &&
    expectedError?.caught === expectedError?.message;

  function httpEvidence(response, root, expectedStatus) {
    const client = find(response?.client);
    const servers = records.filter(span => validIds(response?.client) &&
      span.trace_id === response.client.traceId && span.parent_id === response.client.spanId &&
      meta(span, 'span.kind') === 'server');
    const server = servers[0];
    const clients = records.filter(span => validIds(root) && span.trace_id === root.traceId &&
      span.parent_id === root.spanId && meta(span, 'span.kind') === 'client' &&
      meta(span, 'component') === 'http' && meta(span, 'http.url') === meta(client, 'http.url'));
    // Retransmitting the same IDs is not duplicate instrumentation. Additional
    // unique client/server spans for one request are, including after resume.
    const singleRequest = new Set(clients.map(span => span.span_id)).size === 1 &&
      new Set(servers.map(span => span.span_id)).size === 1;
    return {
      client,
      server,
      exported: Boolean(client && server) && singleRequest && response?.status === expectedStatus &&
        childOf(response?.client, root) && meta(client, 'component') === 'http' &&
        meta(client, 'span.kind') === 'client' && status(client, expectedStatus) &&
        status(server, expectedStatus),
    };
  }

  function w3c(response, root) {
    const value = response?.body?.traceparent;
    if (typeof value !== 'string' || value !== response?.traceparent ||
        !/^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$/.test(value)) return false;
    const [, trace, parent] = value.split('-');
    const high = meta(find(root), '_dd.p.tid') || '0000000000000000';
    return validIds(response?.client) && trace.slice(0, 16) === high &&
      BigInt(`0x${trace.slice(16)}`).toString() === response.client.traceId &&
      BigInt(`0x${parent}`).toString() === response.client.spanId;
  }

  function echoEvidence(response, root) {
    const http = httpEvidence(response, root, 200);
    return http.exported && w3c(response, root) &&
      descendantOf(response?.body?.server, ids(http.server)) &&
      response?.body?.runtimeId === runtimeId;
  }

  const http = features?.http;
  const echo = httpEvidence(http?.echo, http?.root, 200);
  const failure = httpEvidence(http?.error, http?.root, 500);
  checks.http_incoming_and_outgoing = echo.exported &&
    descendantOf(http?.echo?.body?.server, ids(echo.server));
  checks.http_w3c_propagation = echo.exported && w3c(http?.echo, http?.root);
  checks.http_runtime_identity = echo.exported && http?.echo?.body?.runtimeId === runtimeId;
  // dd-trace-js defaults client errors to 400-499, server errors to 500-599.
  checks.http_500_and_error_metadata = failure.exported && Number(failure.client.error) === 0 &&
    hasErrorDetails(failure.server) && meta(failure.server, 'error.type') === 'Error' &&
    meta(failure.server, 'error.message') === 'microvm verification expected HTTP error';
  const notFound = httpEvidence(http?.notFound, http?.root, 404);
  checks.http_404_client_error = notFound.exported && isError(notFound.client) &&
    Number(notFound.server.error) === 0;
  checks.http_scope_restoration = echo.exported && failure.exported && notFound.exported &&
    same(http?.afterEcho, http?.root) && same(http?.afterError, http?.root) &&
    same(http?.afterNotFound, http?.root);

  const asyncEvidence = features?.async;
  const tasks = asyncEvidence?.tasks || [];
  checks.async_overlapping_contexts = tasks.length === 2 &&
    new Set(tasks.map(task => task.span?.spanId)).size === 2 && tasks.every(task =>
      childOf(task.span, asyncEvidence?.root) && childOf(task.leaf, task.span) &&
      find(task.span)?.name === `verify.js.async.task.${task.index}` &&
      find(task.leaf)?.name === `verify.js.async.leaf.${task.index}` &&
      same(task.before, task.span) && same(task.afterAwait, task.span) &&
      same(task.restored, task.span));
  checks.async_parent_restoration = checks.async_overlapping_contexts &&
    same(asyncEvidence?.restored, asyncEvidence?.root);

  const concurrent = features?.concurrent || [];
  checks.concurrent_http_context_isolation = concurrent.length === 4 &&
    new Set(concurrent.map(item => item.root?.traceId)).size === concurrent.length &&
    new Set(concurrent.map(item => item.response?.client?.spanId)).size === concurrent.length &&
    concurrent.every(item => rootPresent({ ...item.root, name: `verify.js.concurrent.${item.index}` }) &&
      same(item.before, item.root) && same(item.after, item.root) &&
      echoEvidence(item.response, item.root));

  // Captured Pino JSON is checked separately by the caller. Here require the
  // expected correlation IDs to belong to the actual exported manual child.
  checks.log_correlation_span_exported = Boolean(features?.log?.marker && features.log.expected) &&
    same(features?.log, manual?.child) && Boolean(manualChild) &&
    String(features.log.expected.span_id) === manual.child.spanId &&
    descendantOf(features.log, manual?.root);

  return checks;
}

module.exports = { evaluateFeatures };
