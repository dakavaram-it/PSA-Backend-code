const pulseService = require('../services/pulseService');
async function overview(req, res, next) {
  try { return res.json(await pulseService.overview()); } catch (e) { next(e); }
}
async function ivrs(req, res, next) {
  try { return res.json(await pulseService.ivrs()); } catch (e) { next(e); }
}
async function refresh(req, res, next) {
  try {
    const token = req.get('X-Refresh-Token') || req.body?.token;
    const force = req.query.force === 'true' || req.body?.force === true;
    return res.json(await pulseService.refresh(token, force));
  } catch (e) { next(e); }
}
async function refreshStatus(req, res, next) {
  try { return res.json(await pulseService.refreshStatus()); } catch (e) { next(e); }
}
async function refreshPrecheck(req, res, next) {
  try { return res.json(await pulseService.refreshPrecheck()); } catch (e) { next(e); }
}
async function constituency(req, res, next) {
  try { return res.json(await pulseService.constituency(req.params.name)); } catch (e) { next(e); }
}
async function warmConstituencies(req, res, next) {
  try {
    const token = req.get('X-Refresh-Token') || req.body?.token;
    const force = req.query.force === 'true' || req.body?.force === true;
    return res.json(await pulseService.warmConstituencies(token, force));
  } catch (e) { next(e); }
}
async function warmStatus(req, res, next) {
  try { return res.json(await pulseService.warmStatus()); } catch (e) { next(e); }
}
async function panelSurveys(req, res, next) {
  try { return res.json(await pulseService.panelSurveys()); } catch (e) { next(e); }
}
async function panel(req, res, next) {
  try { return res.json(await pulseService.panel(req.query.sids || '')); } catch (e) { next(e); }
}
async function surveyAc(req, res, next) {
  try { return res.json(await pulseService.surveyAc(req.query.name || '', req.query.agency || '')); } catch (e) { next(e); }
}
async function survey(req, res, next) {
  try { return res.json(await pulseService.survey(req.query.agency || '')); } catch (e) { next(e); }
}
async function surveyCompare(req, res, next) {
  try { return res.json(await pulseService.surveyCompare()); } catch (e) { next(e); }
}
module.exports = { overview, ivrs, refresh, refreshStatus, refreshPrecheck, constituency, warmConstituencies, warmStatus, panelSurveys, panel, survey, surveyAc, surveyCompare };
