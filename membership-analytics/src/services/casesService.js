// BFF forwarder: Node /api/cases/* -> FastAPI /api/v1/cases/*
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';
const FASTAPI_TIMEOUT_MS = Number(process.env.FASTAPI_TIMEOUT_MS || 30000);

async function callCasesApi(path, query = {}) {
  const url = new URL(`${FASTAPI_BASE_URL}/api/v1/cases${path}`);
  Object.entries(query || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  });
  const res = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(FASTAPI_TIMEOUT_MS),
  });
  const text = await res.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; }
  catch (_e) {
    const err = new Error(`FastAPI returned non-JSON: ${text.slice(0, 200)}`);
    err.statusCode = 502; throw err;
  }
  if (!res.ok) {
    const err = new Error(payload?.detail?.message || payload?.message || 'Cases API error');
    err.statusCode = res.status; throw err;
  }
  return payload;
}

module.exports = {
  geo: () => callCasesApi('/geo'),
  overview: (q) => callCasesApi('/overview', q),
  leaders: (q) => callCasesApi('/leaders', q),
  search: (q) => callCasesApi('/search', q),
  leaderDetail: (key) => callCasesApi(`/leaders/${encodeURIComponent(key)}`),
};
