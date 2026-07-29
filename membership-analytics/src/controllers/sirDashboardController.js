const svc = require('../services/sirDashboardService');

const QUERY_KEYS = ['range', 'from', 'to', 'pc'];
function buildQuery(req) {
  const sp = new URLSearchParams();
  QUERY_KEYS.forEach((k) => {
    const v = req.query[k];
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  });
  const s = sp.toString();
  return s ? `?${s}` : '';
}

async function overview(req, res, next) {
  try { return res.json(await svc.overview()); } catch (e) { next(e); }
}
async function parliament(req, res, next) {
  try { return res.json(await svc.parliament(buildQuery(req))); } catch (e) { next(e); }
}
async function assembly(req, res, next) {
  try { return res.json(await svc.assembly(buildQuery(req))); } catch (e) { next(e); }
}
async function cubsOverview(req, res, next) {
  try { return res.json(await svc.cubsOverview(buildQuery(req))); } catch (e) { next(e); }
}
async function cubsParliament(req, res, next) {
  try { return res.json(await svc.cubsParliament(buildQuery(req))); } catch (e) { next(e); }
}
async function cubsAssembly(req, res, next) {
  try { return res.json(await svc.cubsAssembly(buildQuery(req))); } catch (e) { next(e); }
}

module.exports = { overview, parliament, assembly, cubsOverview, cubsParliament, cubsAssembly };
