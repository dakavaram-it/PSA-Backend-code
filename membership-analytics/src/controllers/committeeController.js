// src/controllers/committeeController.js

const committeeService = require('../services/committeeService');

function ok(res, payload) {
  return res.json(payload);
}

async function getBasicCommittees(req, res, next) {
  try { return ok(res, await committeeService.getBasicCommittees(req.query)); }
  catch (error) { next(error); }
}

async function getCommittees(req, res, next) {
  try { return ok(res, await committeeService.getCommittees(req.query)); }
  catch (error) { next(error); }
}

async function getCommitteeRoles(req, res, next) {
  try { return ok(res, await committeeService.getCommitteeRoles(req.query)); }
  catch (error) { next(error); }
}

async function checkKss(req, res, next) {
  try { return ok(res, await committeeService.checkKss(req.query)); }
  catch (error) { next(error); }
}

async function listProposals(req, res, next) {
  try { return ok(res, await committeeService.listCommitteeProposals(req.query)); }
  catch (error) { next(error); }
}

async function createProposal(req, res, next) {
  try { return ok(res, await committeeService.createCommitteeProposal(req.body)); }
  catch (error) { next(error); }
}

async function addMembers(req, res, next) {
  try { return ok(res, await committeeService.addCommitteeMembers(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function compareCandidates(req, res, next) {
  try { return ok(res, await committeeService.compareCommitteeCandidates(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function removeMember(req, res, next) {
  try { return ok(res, await committeeService.removeCommitteeMember(req.params.proposalId, req.params.memberId, req.query)); }
  catch (error) { next(error); }
}

async function removeAllMembers(req, res, next) {
  try { return ok(res, await committeeService.removeAllCommitteeMembers(req.params.proposalId, req.query)); }
  catch (error) { next(error); }
}

async function deleteProposal(req, res, next) {
  try { return ok(res, await committeeService.deleteCommitteeProposal(req.params.proposalId, req.query)); }
  catch (error) { next(error); }
}

async function updateStatus(req, res, next) {
  try { return ok(res, await committeeService.updateCommitteeStatus(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function revertStage(req, res, next) {
  try { return ok(res, await committeeService.revertCommitteeStage(req.params.proposalId, req.query)); }
  catch (error) { next(error); }
}

async function getProposal(req, res, next) {
  try { return ok(res, await committeeService.getCommitteeProposal(req.params.proposalId)); }
  catch (error) { next(error); }
}

async function getWorkflowActions(req, res, next) {
  try { return ok(res, await committeeService.getCommitteeWorkflowActions(req.params.proposalId, req.query)); }
  catch (error) { next(error); }
}

async function executeWorkflowAction(req, res, next) {
  try { return ok(res, await committeeService.executeCommitteeWorkflowAction(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function saveWorkflowFeedbacks(req, res, next) {
  try { return ok(res, await committeeService.saveCommitteeWorkflowFeedbacks(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function saveWorkflowReviews(req, res, next) {
  try { return ok(res, await committeeService.saveCommitteeWorkflowReviews(req.params.proposalId, req.body)); }
  catch (error) { next(error); }
}

async function getWorkflowHistory(req, res, next) {
  try { return ok(res, await committeeService.getCommitteeWorkflowHistory(req.params.proposalId)); }
  catch (error) { next(error); }
}

module.exports = {
  getBasicCommittees,
  getCommittees,
  getCommitteeRoles,
  checkKss,
  listProposals,
  createProposal,
  addMembers,
  compareCandidates,
  removeMember,
  removeAllMembers,
  deleteProposal,
  updateStatus,
  revertStage,
  getProposal,
  getWorkflowActions,
  executeWorkflowAction,
  saveWorkflowFeedbacks,
  saveWorkflowReviews,
  getWorkflowHistory,
};
