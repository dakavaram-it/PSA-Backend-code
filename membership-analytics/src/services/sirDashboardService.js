// BFF forwarder: Node /api/sir-dashboard/* -> FastAPI /api/v1/sir-dashboard/*
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';
const FASTAPI_TIMEOUT_MS = Number(process.env.FASTAPI_TIMEOUT_MS || 90000);

async function call(path, { method = 'GET', timeoutMs = FASTAPI_TIMEOUT_MS } = {}) {
  const res = await fetch(`${FASTAPI_BASE_URL}/api/v1/sir-dashboard${path}`, {
    method, headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; }
  catch (_e) { const err = new Error('FastAPI non-JSON'); err.statusCode = 502; throw err; }
  if (!res.ok) {
    const err = new Error(payload?.detail?.message || payload?.message || 'SIR dashboard API error');
    err.statusCode = res.status;
    throw err;
  }
  return payload;
}

module.exports = {
  overview: () => call('/overview'),
  parliament: (qs) => call(`/parliament${qs}`),
  assembly: (qs) => call(`/assembly${qs}`),
  cubsOverview: (qs) => call(`/cubs/overview${qs}`),
  cubsParliament: (qs) => call(`/cubs/parliament${qs}`),
  cubsAssembly: (qs) => call(`/cubs/assembly${qs}`),
};
