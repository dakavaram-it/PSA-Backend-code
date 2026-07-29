-- =============================================================================
-- Migration: insert a "Release for GO Issue" workflow stage into the Nominated
-- Post workflow, between FINALISING and GO_ISSUING.
--
--   Old chain:  FINALISING --MOVE_TO_GO_ISSUING--> GO_ISSUING --ISSUE_GO--> GO_ISSUED
--   New chain:  FINALISING --MOVE_TO_RELEASE_FOR_GO--> RELEASE_FOR_GO
--                          --MOVE_TO_GO_ISSUING--> GO_ISSUING --ISSUE_GO--> GO_ISSUED
--
-- Why the action swap: the FastAPI workflow service triggers the "pick candidates
-- for GO" step on the action code that LEAVES the Finalising stage. After this
-- migration that action is MOVE_TO_RELEASE_FOR_GO (see workflow_service.py). The
-- RELEASE_FOR_GO --MOVE_TO_GO_ISSUING--> GO_ISSUING transition is then a plain
-- stage move (candidates already SHORTLISTED), and ISSUE_GO is unchanged.
--
-- Target DB: pa_track.  Run against pa_track ONLY.
--
-- NOTE ON PORTABILITY: the workflow seed/DDL is not checked into this repo, so the
-- exact column set of these master tables is unknown here. To stay robust against
-- unknown columns this migration CLONES an existing sibling row into a TEMPORARY
-- table, nulls the auto-increment PK, overrides only the columns that change, and
-- re-inserts. That copies every NOT NULL column from the sibling automatically.
-- Assumptions: PK columns are AUTO_INCREMENT and named workflow_stage_id /
-- workflow_action_id / workflow_transition_id (as used by workflow_repository.py).
--
-- Idempotency: guarded so re-running is a no-op once applied. Wrap in a transaction
-- and verify with the SELECT at the bottom before COMMIT.
-- =============================================================================

START TRANSACTION;

-- Resolve the active Nominated Post workflow (post_type_id = 2).
SET @wf := (
  SELECT workflow_id FROM workflow_definition
  WHERE post_type_id = 2 AND is_active = 'Y' AND is_deleted = 'N'
  ORDER BY version_no DESC LIMIT 1
);

-- ---------------------------------------------------------------------------
-- 1. New stage RELEASE_FOR_GO, cloned from GO_ISSUING (inherits all columns).
--    display_order = 6 (Finalising=5, GO_ISSUING bumped to 7 below).
--    mapped_proposal_status_code = NULL so no new proposal_status_master row is
--    required (update_stage() falls back to the stage code and skips the status
--    update when unmatched).
-- ---------------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS _rfg_stage;
CREATE TEMPORARY TABLE _rfg_stage
  SELECT * FROM workflow_stage_master
  WHERE workflow_id = @wf AND stage_code = 'GO_ISSUING' AND is_deleted = 'N'
  LIMIT 1;

UPDATE _rfg_stage SET
  workflow_stage_id           = NULL,
  stage_code                  = 'RELEASE_FOR_GO',
  stage_name                  = 'Release for GO Issue',
  display_order               = 6,
  is_initial_stage            = 'N',
  is_terminal_stage           = 'N',
  mapped_proposal_status_code = NULL;

INSERT INTO workflow_stage_master
  SELECT * FROM _rfg_stage
  WHERE NOT EXISTS (
    SELECT 1 FROM workflow_stage_master
    WHERE workflow_id = @wf AND stage_code = 'RELEASE_FOR_GO' AND is_deleted = 'N'
  );
DROP TEMPORARY TABLE _rfg_stage;

-- Bump the GO stages to make room for RELEASE_FOR_GO at display_order 6.
UPDATE workflow_stage_master
SET display_order = 7
WHERE workflow_id = @wf AND stage_code IN ('GO_ISSUING', 'GO_ISSUED') AND is_deleted = 'N';

-- ---------------------------------------------------------------------------
-- 2. New action MOVE_TO_RELEASE_FOR_GO, cloned from MOVE_TO_GO_ISSUING.
-- ---------------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS _rfg_action;
CREATE TEMPORARY TABLE _rfg_action
  SELECT * FROM workflow_action_master
  WHERE action_code = 'MOVE_TO_GO_ISSUING'
  LIMIT 1;

UPDATE _rfg_action SET
  workflow_action_id = NULL,
  action_code        = 'MOVE_TO_RELEASE_FOR_GO',
  action_name        = 'Assign for GO Issue',
  button_label       = 'Assign for GO Issue';

INSERT INTO workflow_action_master
  SELECT * FROM _rfg_action
  WHERE NOT EXISTS (
    SELECT 1 FROM workflow_action_master WHERE action_code = 'MOVE_TO_RELEASE_FOR_GO'
  );
DROP TEMPORARY TABLE _rfg_action;

-- Resolve ids now that the new rows exist.
SET @st_finalising := (SELECT workflow_stage_id FROM workflow_stage_master
                       WHERE workflow_id = @wf AND stage_code = 'FINALISING'     AND is_deleted = 'N' LIMIT 1);
SET @st_release    := (SELECT workflow_stage_id FROM workflow_stage_master
                       WHERE workflow_id = @wf AND stage_code = 'RELEASE_FOR_GO' AND is_deleted = 'N' LIMIT 1);
SET @st_goissuing  := (SELECT workflow_stage_id FROM workflow_stage_master
                       WHERE workflow_id = @wf AND stage_code = 'GO_ISSUING'     AND is_deleted = 'N' LIMIT 1);
SET @act_release   := (SELECT workflow_action_id FROM workflow_action_master
                       WHERE action_code = 'MOVE_TO_RELEASE_FOR_GO' LIMIT 1);
SET @act_goissuing := (SELECT workflow_action_id FROM workflow_action_master
                       WHERE action_code = 'MOVE_TO_GO_ISSUING' LIMIT 1);

-- ---------------------------------------------------------------------------
-- 3. Repoint the existing FINALISING transition so it now leaves to
--    RELEASE_FOR_GO via MOVE_TO_RELEASE_FOR_GO (keeps its validation flags).
-- ---------------------------------------------------------------------------
UPDATE workflow_transition_master
SET to_stage_id = @st_release,
    action_id   = @act_release
WHERE workflow_id   = @wf
  AND from_stage_id = @st_finalising
  AND action_id     = @act_goissuing
  AND is_deleted    = 'N';

-- ---------------------------------------------------------------------------
-- 4. New transition RELEASE_FOR_GO --MOVE_TO_GO_ISSUING--> GO_ISSUING.
--    Cloned from the (now repointed) Finalising transition to inherit flags,
--    then forced to a plain move: no GO details required here.
-- ---------------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS _rfg_trans;
CREATE TEMPORARY TABLE _rfg_trans
  SELECT * FROM workflow_transition_master
  WHERE workflow_id = @wf AND from_stage_id = @st_finalising AND action_id = @act_release AND is_deleted = 'N'
  LIMIT 1;

UPDATE _rfg_trans SET
  workflow_transition_id = NULL,
  from_stage_id          = @st_release,
  to_stage_id            = @st_goissuing,
  action_id              = @act_goissuing,
  requires_go_details    = 'N';

INSERT INTO workflow_transition_master
  SELECT * FROM _rfg_trans
  WHERE NOT EXISTS (
    SELECT 1 FROM workflow_transition_master
    WHERE workflow_id = @wf AND from_stage_id = @st_release AND action_id = @act_goissuing AND is_deleted = 'N'
  );
DROP TEMPORARY TABLE _rfg_trans;

-- ---------------------------------------------------------------------------
-- 5. (OPTIONAL — only if you enforce non-admin RBAC.) The app calls workflow
--    actions with roleCode 'ADMIN', which bypasses workflow_stage_role_mapping,
--    so this is not required for the app to work. If you drive this workflow
--    with non-admin roles, replicate the FINALISING stage's role mappings for
--    the new RELEASE_FOR_GO stage:
--
--    DROP TEMPORARY TABLE IF EXISTS _rfg_roles;
--    CREATE TEMPORARY TABLE _rfg_roles
--      SELECT * FROM workflow_stage_role_mapping
--      WHERE workflow_id = @wf AND stage_id = @st_finalising AND is_deleted = 'N';
--    UPDATE _rfg_roles SET workflow_stage_role_mapping_id = NULL, stage_id = @st_release;
--    INSERT INTO workflow_stage_role_mapping SELECT * FROM _rfg_roles;
--    DROP TEMPORARY TABLE _rfg_roles;
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- VERIFY (review before COMMIT). Expect the chain:
--   FINALISING --MOVE_TO_RELEASE_FOR_GO--> RELEASE_FOR_GO
--   RELEASE_FOR_GO --MOVE_TO_GO_ISSUING--> GO_ISSUING
--   GO_ISSUING --ISSUE_GO--> GO_ISSUED
-- and stage display_order: FINALISING=5, RELEASE_FOR_GO=6, GO_ISSUING=7, GO_ISSUED=7
-- ---------------------------------------------------------------------------
SELECT fs.stage_code AS from_stage, a.action_code, ts.stage_code AS to_stage,
       t.requires_go_details, t.is_active
FROM workflow_transition_master t
JOIN workflow_action_master a ON t.action_id = a.workflow_action_id
JOIN workflow_stage_master fs ON t.from_stage_id = fs.workflow_stage_id
JOIN workflow_stage_master ts ON t.to_stage_id = ts.workflow_stage_id
WHERE t.workflow_id = @wf AND t.is_deleted = 'N'
  AND fs.stage_code IN ('FINALISING', 'RELEASE_FOR_GO', 'GO_ISSUING')
ORDER BY fs.display_order;

-- COMMIT;   -- uncomment after verifying the SELECT above
-- ROLLBACK; -- if anything looks wrong
