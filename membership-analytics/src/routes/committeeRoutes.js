// src/routes/committeeRoutes.js

const express = require('express');
const controller = require('../controllers/committeeController');

const router = express.Router();

router.get('/basic-committees', controller.getBasicCommittees);
router.get('/committees', controller.getCommittees);
router.get('/roles', controller.getCommitteeRoles);
router.get('/kss-check', controller.checkKss);

router.get('/proposals', controller.listProposals);
router.post('/proposals', controller.createProposal);
router.post('/proposals/:proposalId/members', controller.addMembers);
router.delete('/proposals/:proposalId/members/:memberId', controller.removeMember);
router.delete('/proposals/:proposalId/members', controller.removeAllMembers);
router.delete('/proposals/:proposalId/delete', controller.deleteProposal);
router.patch('/proposals/:proposalId/status', controller.updateStatus);
router.post('/proposals/:proposalId/revert-stage', controller.revertStage);
router.post('/proposals/:proposalId/candidates/compare', controller.compareCandidates);
router.get('/proposals/:proposalId', controller.getProposal);

router.get('/workflow/proposals/:proposalId/actions', controller.getWorkflowActions);
router.post('/workflow/proposals/:proposalId/actions', controller.executeWorkflowAction);
router.post('/workflow/proposals/:proposalId/feedbacks', controller.saveWorkflowFeedbacks);
router.put('/workflow/proposals/:proposalId/reviews', controller.saveWorkflowReviews);
router.get('/workflow/proposals/:proposalId/history', controller.getWorkflowHistory);

module.exports = router;
