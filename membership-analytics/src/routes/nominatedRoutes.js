const express = require('express');
const controller = require('../controllers/nominatedController');

const router = express.Router();

router.get('/bootstrap', controller.bootstrap);
router.get('/locations', controller.locations);
router.get('/departments', controller.departments);
router.get('/boards', controller.boards);
router.get('/positions', controller.positions);
router.get('/capacity', controller.capacity);
router.get('/cadre/search', controller.searchCadre);
router.get('/caste-options', controller.getCasteOptions);
router.get('/occupation-options', controller.getOccupationOptions);
router.get('/education-options', controller.getEducationOptions);
router.get('/party-options', controller.getPartyOptions);

router.get('/proposals', controller.listProposals);
router.get('/proposals/candidates/search', controller.searchProposalCandidates);
router.get('/proposals/candidates/app-usage', controller.getCandidateAppUsage);
router.get('/proposals/candidates/pool', controller.getCandidatePool);
router.post('/proposals/candidates/pool/compare', controller.comparePoolCandidates);
router.patch('/proposals/candidates/caste', controller.updateCandidateCaste);
router.patch('/proposals/candidates/occupation', controller.updateCandidateOccupation);
router.patch('/proposals/candidates/education', controller.updateCandidateEducation);
router.post('/proposals/:proposalId/candidates/compare', controller.compareProposalCandidates);
router.post('/proposals/:proposalId/candidates/manual', controller.createManualCandidate);
router.post('/proposals', controller.createProposal);
router.get('/proposals/:proposalId', controller.getProposal);
router.patch('/proposals/:proposalId/status', controller.updateProposalStatus);
router.post('/proposals/:proposalId/candidates', controller.addProposalCandidates);
router.delete('/proposals/:proposalId/candidates', controller.removeAllProposalCandidates);
router.delete('/proposals/:proposalId/candidates/:proposalCandidateId', controller.removeProposalCandidate);
router.delete('/proposals/:proposalId/delete', controller.deleteProposal);
router.post('/proposals/:proposalId/revert-stage', controller.revertProposalStage);

// Workflow APIs
router.get('/workflow/proposals/:proposalId/actions', controller.getWorkflowActions);
router.post('/workflow/proposals/:proposalId/actions', controller.executeWorkflowAction);
router.post('/workflow/proposals/:proposalId/feedbacks', controller.saveProposalFeedbacks);
router.put('/workflow/proposals/:proposalId/reviews', controller.saveWorkflowReviews);
router.get('/workflow/proposals/:proposalId/history', controller.getWorkflowHistory);

module.exports = router;
