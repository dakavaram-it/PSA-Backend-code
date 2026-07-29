const casesService = require('../services/casesService');

async function geo(req, res, next) {
  try { return res.json(await casesService.geo()); } catch (e) { next(e); }
}
async function overview(req, res, next) {
  try { return res.json(await casesService.overview(req.query)); } catch (e) { next(e); }
}
async function leaders(req, res, next) {
  try { return res.json(await casesService.leaders(req.query)); } catch (e) { next(e); }
}
async function search(req, res, next) {
  try { return res.json(await casesService.search(req.query)); } catch (e) { next(e); }
}
async function leaderDetail(req, res, next) {
  try { return res.json(await casesService.leaderDetail(req.params.key)); } catch (e) { next(e); }
}

module.exports = { geo, overview, leaders, search, leaderDetail };
