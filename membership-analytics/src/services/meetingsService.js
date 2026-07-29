// BFF forwarder: Node /api/meetings/* -> FastAPI /api/v1/meetings/*
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';
const FASTAPI_TIMEOUT_MS = Number(process.env.FASTAPI_TIMEOUT_MS || 30000);

async function call(path, { timeoutMs = FASTAPI_TIMEOUT_MS } = {}) {
  const res = await fetch(`${FASTAPI_BASE_URL}/api/v1/meetings${path}`, {
    method: 'GET', headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; }
  catch (_e) { const err = new Error('FastAPI non-JSON'); err.statusCode = 502; throw err; }
  if (!res.ok) {
    const err = new Error(payload?.detail?.message || payload?.message || 'Meetings API error');
    err.statusCode = res.status;
    throw err;
  }
  return payload;
}

module.exports = {
  filters: () => call('/filters'),
  overview: (qs) => call(`/overview${qs}`, { timeoutMs: 60000 }),
  list: (qs) => call(`/list${qs}`, { timeoutMs: 60000 }),
};
