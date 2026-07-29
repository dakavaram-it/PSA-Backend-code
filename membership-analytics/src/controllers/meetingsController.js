const meetingsService = require('../services/meetingsService');

// Forward only the known filter/query params (avoids passing junk to FastAPI).
const QUERY_KEYS = ['from', 'to', 'mainType', 'type', 'level', 'occurrence',
  'conducted', 'ivr', 'q', 'sort', 'limit', 'offset'];
function buildQuery(req) {
  const sp = new URLSearchParams();
  QUERY_KEYS.forEach((k) => {
    const v = req.query[k];
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  });
  const s = sp.toString();
  return s ? `?${s}` : '';
}

async function filters(req, res, next) {
  try { return res.json(await meetingsService.filters()); } catch (e) { next(e); }
}
async function overview(req, res, next) {
  try { return res.json(await meetingsService.overview(buildQuery(req))); } catch (e) { next(e); }
}
async function list(req, res, next) {
  try { return res.json(await meetingsService.list(buildQuery(req))); } catch (e) { next(e); }
}

module.exports = { filters, overview, list };
