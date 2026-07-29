-- =====================================================================
-- Reset all "position created" data for Nominated Post + Committee.
--
-- Wipes every proposal / candidate / member / workflow / audit / GO /
-- finalization row from the pa_track pipeline for BOTH modules, and
-- resets AUTO_INCREMENT so the next proposal id starts from 1.
--
-- Scope: pa_track ONLY. dakavara_pa legacy/master data (seats, cadre,
-- legacy committees, etc.) is NOT touched. cases_* / pulse_trend_* are
-- analytics-only and unaffected.
--
-- DESTRUCTIVE — run only against a test/clean environment.
--
-- Usage:
--   mysql -u <user> -p pa_track < backend-python/scripts/reset_proposals.sql
-- =====================================================================

USE pa_track;

SET FOREIGN_KEY_CHECKS = 0;

-- ---- Nominated Post pipeline ----------------------------------------
TRUNCATE TABLE nominated_post_proposal_candidate;
TRUNCATE TABLE nominated_post_proposal_audit;
TRUNCATE TABLE proposal_feedback;
TRUNCATE TABLE proposal_review;
TRUNCATE TABLE proposal_go_details;
TRUNCATE TABLE proposal_workflow_history;
TRUNCATE TABLE nominated_post_proposal;

-- ---- Committee pipeline ---------------------------------------------
TRUNCATE TABLE committee_proposal_member;
TRUNCATE TABLE committee_proposal_position;
TRUNCATE TABLE committee_proposal_audit;
TRUNCATE TABLE committee_feedback;
TRUNCATE TABLE committee_review;
TRUNCATE TABLE committee_finalization;
TRUNCATE TABLE committee_legacy_sync_log;
TRUNCATE TABLE committee_workflow_history;
TRUNCATE TABLE committee_proposal;

-- ---- Shared workflow / outbox (only these two modules write here) ----
TRUNCATE TABLE workflow_instance;
TRUNCATE TABLE event_outbox;

SET FOREIGN_KEY_CHECKS = 1;

-- TRUNCATE already resets AUTO_INCREMENT to 1; the explicit ALTERs below
-- are a belt-and-braces guarantee that the next proposal ids start at 1.
ALTER TABLE nominated_post_proposal AUTO_INCREMENT = 1;
ALTER TABLE committee_proposal       AUTO_INCREMENT = 1;
