// Disable Console eager evaluation, paste this function, then call collectMetricsAdminConfig().
// Four read-only RPCs; the session token is never printed or downloaded.
async function collectMetricsAdminConfig() {
  if (location.origin !== "https://metrics-admin.us1.prod.dog") {
    throw new Error("Run this only on https://metrics-admin.us1.prod.dog after signing in.");
  }
  const orgId = Number(location.pathname.match(/^\/orgs\/(\d+)(?:\/|$)/)?.[1]);
  if (!Number.isSafeInteger(orgId) || orgId <= 0) {
    throw new Error("Open the intended organization's /orgs/<org-id> page first.");
  }
  const stored = localStorage.getItem("auth-token");
  const token = stored?.startsWith('"') ? JSON.parse(stored) : stored;
  if (typeof token !== "string" || !token) throw new Error("Sign in to Metrics Admin first.");
  const names = ["runtime.python.mem.rss", "mvmtest.runtime_rss_control"];
  const transport = {};
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const concat = (...parts) => Uint8Array.from(parts.flatMap(part => Array.from(part)));
  const varint = value => {
    let n = BigInt(value), result = [];
    while (n > 127n) { result.push(Number(n & 127n) | 128); n >>= 7n; }
    return Uint8Array.from([...result, Number(n)]);
  };
  const integer = (field, value) => concat(varint(field * 8), varint(value));
  const bytes = (field, value) => concat(varint(field * 8 + 2), varint(value.length), value);
  function fields(data) {
    let pos = 0;
    const read = () => {
      let n = 0n, shift = 0n;
      while (pos < data.length) {
        const b = data[pos++]; n |= BigInt(b & 127) << shift;
        if (b < 128) return n;
        shift += 7n;
      }
      throw new Error("Truncated protobuf response");
    };
    const result = [];
    while (pos < data.length) {
      const key = Number(read()), field = key >> 3, wire = key & 7;
      let value;
      if (wire === 0) value = read().toString();
      else if (wire === 2) { const length = Number(read()); value = data.slice(pos, pos += length); }
      else throw new Error(`Unexpected wire type ${wire}`);
      result.push({field, value});
    }
    return result;
  }
  const first = (data, field) => fields(data).find(entry => entry.field === field)?.value;
  async function rpc(method, message) {
    const framed = new Uint8Array(message.length + 5);
    new DataView(framed.buffer).setUint32(1, message.length);
    framed.set(message, 5);
    const response = await fetch(`https://metrics-admin-api.us1.prod.dog/metricspb.HMSResolver/${method}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/grpc+proto, application/grpc-web+proto",
        "X-Grpc-Web": "1", "X-User-Agent": "connect-es/1.4.0",
        Authorization: `Bearer ${token}`,
      },
      redirect: "error",
      body: framed,
    });
    if (!response.ok) throw new Error(`${method}: HTTP ${response.status}`);
    const data = new Uint8Array(await response.arrayBuffer()), messages = [];
    let status = response.headers.get("grpc-status");
    for (let pos = 0; pos < data.length;) {
      if (pos + 5 > data.length) throw new Error("Truncated gRPC frame");
      const flags = data[pos], length = new DataView(data.buffer, data.byteOffset + pos + 1, 4).getUint32(0);
      if (pos + 5 + length > data.length) throw new Error("Truncated gRPC payload");
      const payload = data.slice(pos + 5, pos + 5 + length);
      pos += 5 + length;
      if (flags & 128) status = /grpc-status:\s*(\d+)/i.exec(decoder.decode(payload))?.[1] ?? status;
      else if (flags !== 0) throw new Error("Compressed gRPC frames are unsupported");
      else messages.push(payload);
    }
    transport[method] = {
      http_status: response.status, content_type: response.headers.get("content-type"),
      response_bytes: data.length, message_count: messages.length, grpc_status: status,
    };
    if (status !== null && status !== "0") {
      throw new Error(`${method}: gRPC status ${status}${status === "16" ? "; sign in again" : ""}`);
    }
    // The page's unary client also accepts a decoded message when status is omitted.
    if (messages.length !== 1) {
      throw new Error(`${method}: expected one response message; ${JSON.stringify(transport[method])}`);
    }
    return messages;
  }
  const lookup = await rpc("MetricsByName", concat(...names.map(name => bytes(1, encoder.encode(name))), integer(2, orgId)));
  const metrics = lookup.flatMap(message => fields(message).filter(entry => entry.field === 1).map(entry => {
    const metric = first(entry.value, 2);
    return {name: decoder.decode(first(entry.value, 1)), key: first(metric, 1)};
  }));
  if (metrics.length !== names.length || metrics.some(metric => !metric.key)) {
    throw new Error("Could not resolve both metric keys; inspect the two metrics in the UI.");
  }
  const request = concat(integer(1, orgId), bytes(2, concat(...metrics.map(metric => varint(metric.key)))));
  const methods = ["MetadataByKeys", "FullMetadataByKeys", "GetSummaryAggrByOrgMetrics"];
  const replies = await Promise.all(methods.map(async method => [method, await rpc(method, request)]));
  const base64 = data => btoa(Array.from(data, byte => String.fromCharCode(byte)).join(""));
  const result = {
    org_id: orgId, metrics, transport,
    encoding: "uncompressed protobuf messages, base64; credentials omitted",
    responses: Object.fromEntries(replies.map(([method, messages]) => [method, messages.map(base64)])),
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2) + "\n"], {type: "application/json"}));
  const link = document.createElement("a");
  link.href = url; link.download = `metrics-admin-rss-config-${orgId}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  console.log(`Downloaded metrics-admin-rss-config-${orgId}.json; share the file path. No credentials are included.`);
}
