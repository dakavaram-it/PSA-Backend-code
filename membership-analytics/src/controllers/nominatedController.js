const nominatedService = require('../services/nominatedService');

function ok(res, payload) {
  return res.json(payload);
}

async function bootstrap(req, res, next) {
  try { return ok(res, await nominatedService.nominatedBootstrap(req.query)); }
  catch (error) { next(error); }
}

async function locations(req, res, next) {
  try { return ok(res, await nominatedService.nominatedLocations(req.query)); }
  catch (error) { next(error); }
}

async function departments(req, res, next) {
  try { return ok(res, await nominatedService.nominatedDepartments(req.query)); }
  catch (error) { next(error); }
}

async function boards(req, res, next) {
  try { return ok(res, await nominatedService.nominatedBoards(req.query)); }
  catch (error) { next(error); }
}

async function positions(req, res, next) {
  try { return ok(res, await nominatedService.nominatedPositions(req.query)); }
  catch (error) { next(error); }
}

async function capacity(req, res, next) {
  try { return ok(res, await nominatedService.nominatedCapacity(req.query)); }
  catch (error) { next(error); }
}

async function searchCadre(req, res, next) {
  try { return ok(res, await nominatedService.searchCadre(req.query)); }
  catch (error) { next(error); }
}

async function getCasteOptions(req, res, next) {
  try { return ok(res, await nominatedService.getCasteOptions(req.query)); }
  catch (error) { next(error); }
}

// PATCH /api/nominated-post/proposals/candidates/caste
async function updateCandidateCaste(req, res, next) {
  try { return ok(res, await nominatedService.updateCandidateCaste(req.body)); }
  catch (error) { next(error); }
}

async function getOccupationOptions(req, res, next) {
  try { return ok(res, await nominatedService.getOccupationOptions(req.query)); }
  catch (error) { next(error); }
}

// PATCH /api/nominated-post/proposals/candidates/occupation
async function updateCandidateOccupation(req, res, next) {
  try { return ok(res, await nominatedService.updateCandidateOccupation(req.body)); }
  catch (error) { next(error); }
}

async function getEducationOptions(req, res, next) {
  try { return ok(res, await nominatedService.getEducationOptions(req.query)); }
  catch (error) { next(error); }
}

// GET /api/nominated-post/party-options
async function getPartyOptions(req, res, next) {
  try { return ok(res, await nominatedService.getPartyOptions()); }
  catch (error) { next(error); }
}

// PATCH /api/nominated-post/proposals/candidates/education
async function updateCandidateEducation(req, res, next) {
  try { return ok(res, await nominatedService.updateCandidateEducation(req.body)); }
  catch (error) { next(error); }
}
// POST /api/nominated-post/proposals
async function createProposal(req, res, next) {
  try { return ok(res, await nominatedService.createProposal(req.body)); }
  catch (error) { next(error); }
}

// POST /api/nominated-post/proposals/:proposalId/candidates
async function addProposalCandidates(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.addProposalCandidates(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

// POST /api/nominated-post/proposals/:proposalId/candidates/manual
async function createManualCandidate(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.createManualCandidate(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

// DELETE /api/nominated-post/proposals/:proposalId/candidates
async function removeAllProposalCandidates(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.removeAllProposalCandidates(req.params.proposalId, req.query),
    );
  } catch (error) { next(error); }
}

// DELETE /api/nominated-post/proposals/:proposalId/candidates/:proposalCandidateId
async function removeProposalCandidate(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.removeProposalCandidate(
        req.params.proposalId,
        req.params.proposalCandidateId,
        req.query,
      ),
    );
  } catch (error) { next(error); }
}

// DELETE /api/nominated-post/proposals/:proposalId/delete
async function deleteProposal(req, res, next) {
  try {
    return ok(res, await nominatedService.deleteProposal(req.params.proposalId, req.query));
  } catch (error) { next(error); }
}

// POST /api/nominated-post/proposals/:proposalId/revert-stage
async function revertProposalStage(req, res, next) {
  try {
    return ok(res, await nominatedService.revertProposalStage(req.params.proposalId, req.query));
  } catch (error) { next(error); }
}

// GET /api/nominated-post/proposals/:proposalId
async function getProposal(req, res, next) {
  try { return ok(res, await nominatedService.getProposal(req.params.proposalId)); }
  catch (error) { next(error); }
}

// GET /api/nominated-post/proposals/candidates/search?mid=... OR ?mobile=...
async function searchProposalCandidates(req, res, next) {
  try { return ok(res, await nominatedService.searchProposalCandidates(req.query)); }
  catch (error) { next(error); }
}

// GET /api/nominated-post/proposals/candidates/app-usage?mid=...
async function getCandidateAppUsage(req, res, next) {
  try { return ok(res, await nominatedService.getCandidateAppUsage(req.query)); }
  catch (error) { next(error); }
}

// GET /api/nominated-post/proposals/candidates/pool?limit=...
async function getCandidatePool(req, res, next) {
  try { return ok(res, await nominatedService.listCandidatePool(req.query)); }
  catch (error) { next(error); }
}

// POST /api/nominated-post/proposals/candidates/pool/compare
async function comparePoolCandidates(req, res, next) {
  try { return ok(res, await nominatedService.comparePoolCandidates(req.body)); }
  catch (error) { next(error); }
}

// POST /api/nominated-post/proposals/:proposalId/candidates/compare
async function compareProposalCandidates(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.compareProposalCandidates(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

async function listProposals(req, res, next) {
  try { return ok(res, await nominatedService.listProposals(req.query)); }
  catch (error) { next(error); }
}

async function updateProposalStatus(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.updateProposalStatus(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

// GET /api/nominated-post/workflow/proposals/:proposalId/actions?roleCode=ADMIN
async function getWorkflowActions(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.getWorkflowActions(req.params.proposalId, req.query)
    );
  } catch (error) { next(error); }
}

// POST /api/nominated-post/workflow/proposals/:proposalId/actions
async function executeWorkflowAction(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.executeWorkflowAction(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

// POST /api/nominated-post/workflow/proposals/:proposalId/feedbacks
async function saveProposalFeedbacks(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.saveProposalFeedbacks(req.params.proposalId, req.body),
    );
  } catch (error) { next(error); }
}

// PUT /api/nominated-post/workflow/proposals/:proposalId/reviews
async function saveWorkflowReviews(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.saveWorkflowReviews(req.params.proposalId, req.body)
    );
  } catch (error) { next(error); }
}

// GET /api/nominated-post/workflow/proposals/:proposalId/history
async function getWorkflowHistory(req, res, next) {
  try {
    return ok(
      res,
      await nominatedService.getWorkflowHistory(req.params.proposalId)
    );
  } catch (error) { next(error); }
}

module.exports = {
  bootstrap,
  locations,
  departments,
  boards,
  positions,
  capacity,
  searchCadre,
  getCasteOptions,
  updateCandidateCaste,
  getOccupationOptions,
  updateCandidateOccupation,
  getEducationOptions,
  getPartyOptions,
  updateCandidateEducation,
  listProposals,
  createProposal,
  addProposalCandidates,
  createManualCandidate,
  removeProposalCandidate,
  removeAllProposalCandidates,
  deleteProposal,
  revertProposalStage,
  getProposal,
  updateProposalStatus,
  searchProposalCandidates,
  getCandidateAppUsage,
  getCandidatePool,
  comparePoolCandidates,
  compareProposalCandidates,
  getWorkflowActions,
  executeWorkflowAction,
  saveProposalFeedbacks,
  saveWorkflowReviews,
  getWorkflowHistory
};
