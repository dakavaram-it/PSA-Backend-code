// BFF forwarder: Node /api/pulse/* -> FastAPI /api/v1/pulse/*
const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';
const FASTAPI_TIMEOUT_MS = Number(process.env.FASTAPI_TIMEOUT_MS || 30000);

async function call(path, { method = 'GET', headers = {}, timeoutMs = FASTAPI_TIMEOUT_MS } = {}) {
  const res = await fetch(`${FASTAPI_BASE_URL}/api/v1/pulse${path}`, {
    method, headers: { Accept: 'application/json', ...headers },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; }
  catch (_e) { const err = new Error('FastAPI non-JSON'); err.statusCode = 502; throw err; }
  if (!res.ok) { const err = new Error(payload?.detail?.message || payload?.message || 'Pulse API error'); err.statusCode = res.status; throw err; }
  return payload;
}
module.exports = {
  overview: () => call('/overview'),
  ivrs: () => call('/ivrs'),
  refresh: (token, force) => call(`/refresh${force ? '?force=true' : ''}`, { method: 'POST', headers: token ? { 'X-Refresh-Token': token } : {} }),
  refreshStatus: () => call('/refresh/status'),
  refreshPrecheck: () => call('/refresh/precheck', { timeoutMs: 90000 }),
  warmConstituencies: (token, force) => call(`/constituencies/warm${force ? '?force=true' : ''}`, { method: 'POST', headers: token ? { 'X-Refresh-Token': token } : {} }),
  warmStatus: () => call('/constituencies/warm/status'),
  panelSurveys: () => call('/panel/surveys'),
  panel: (sids) => call(`/panel?sids=${encodeURIComponent(sids)}`),
  surveyCompare: () => call('/survey/compare', { timeoutMs: 60000 }),
  survey: (agency) => call(`/survey${agency ? `?agency=${encodeURIComponent(agency)}` : ''}`, { timeoutMs: 60000 }),
  surveyAc: (name, agency) => call(`/survey/ac?name=${encodeURIComponent(name)}${agency ? `&agency=${encodeURIComponent(agency)}` : ''}`, { timeoutMs: 60000 }),
  constituency: (name) => call(`/constituency/${encodeURIComponent(name)}`, { timeoutMs: 240000 }),
};
