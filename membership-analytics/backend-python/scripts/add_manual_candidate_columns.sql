-- =====================================================================
-- Manual ("Create New Candidate") support for Nominated Post.
--
-- Adds snapshot columns to pa_track.nominated_post_proposal_candidate so a
-- brand-new candidate (not in dakavara_pa.tdp_cadre) can be created directly
-- on a proposal with a full profile. These rows have tdp_cadre_id = NULL and
-- source_type = 'MANUAL'.
--
-- Already present and reused (no change): tdp_cadre_id (nullable),
-- candidate_name, mobile_no, gender, age, caste_state_id, caste_name,
-- caste_category_name, source_type enum (already includes 'MANUAL').
-- The uk_prop_cadre_active unique key is (proposal_id, tdp_cadre_id, is_deleted);
-- MySQL treats NULLs as distinct, so multiple manual candidates per proposal
-- do not collide.
--
-- Scope: pa_track ONLY. Safe/idempotent-ish — run once on each environment.
--
-- Usage:
--   mysql -u <user> -p pa_track < backend-python/scripts/add_manual_candidate_columns.sql
-- =====================================================================

USE pa_track;

ALTER TABLE nominated_post_proposal_candidate
  ADD COLUMN date_of_birth   DATE          DEFAULT NULL AFTER age,
  ADD COLUMN occupation_id   BIGINT        DEFAULT NULL AFTER caste_category_name,
  ADD COLUMN occupation_name VARCHAR(150)  DEFAULT NULL AFTER occupation_id,
  ADD COLUMN education_id    BIGINT        DEFAULT NULL AFTER occupation_name,
  ADD COLUMN education_name  VARCHAR(150)  DEFAULT NULL AFTER education_id,
  ADD COLUMN parliament_id   BIGINT        DEFAULT NULL AFTER education_name,
  ADD COLUMN parliament_name VARCHAR(150)  DEFAULT NULL AFTER parliament_id,
  ADD COLUMN assembly_id     BIGINT        DEFAULT NULL AFTER parliament_name,
  ADD COLUMN assembly_name   VARCHAR(150)  DEFAULT NULL AFTER assembly_id,
  ADD COLUMN mandal_id       BIGINT        DEFAULT NULL AFTER assembly_name,
  ADD COLUMN mandal_name     VARCHAR(150)  DEFAULT NULL AFTER mandal_id;
