-- Seed the ABN_REMARKS feedback source for Step 3 (Feedback).
-- Database: pa_track  (DO NOT mix with dakavara_pa DDL — per CLAUDE.md schema rules).
--
-- Adds a 4th, non-mandatory per-candidate feedback source ("ABN REMARKS") alongside
-- the existing PROGRAM_COMMITTEE / IVRS / EXTERNAL_MLA sources, for BOTH workflows:
--   NOMINATED_POST_WORKFLOW (post_type_id 2)  and  COMMITTEE_WORKFLOW (post_type_id 1).
--
-- Without this row, saving feedback with feedbackCode=ABN_REMARKS is rejected by the
-- backend ("Invalid feedbackCode" — workflow_repository.save_feedbacks /
-- committee_repository validation), so the Feedback step would 400.
--
-- is_mandatory='N' on purpose: ABN REMARKS must never block MOVE_TO_REVIEW.
--
-- Idempotent: re-running is a no-op (guarded by NOT EXISTS). workflow_id is resolved
-- from workflow_definition by workflow_code, not hardcoded.

INSERT INTO workflow_feedback_type_master
    (workflow_id, feedback_code, feedback_name, is_mandatory, display_order, is_active, is_deleted)
SELECT
    wd.workflow_id, 'ABN_REMARKS', 'ABN Remarks', 'N', 4, 'Y', 'N'
FROM workflow_definition wd
WHERE wd.workflow_code IN ('NOMINATED_POST_WORKFLOW', 'COMMITTEE_WORKFLOW')
  AND wd.is_active = 'Y'
  AND wd.is_deleted = 'N'
  AND NOT EXISTS (
      SELECT 1
      FROM workflow_feedback_type_master f
      WHERE f.workflow_id = wd.workflow_id
        AND f.feedback_code = 'ABN_REMARKS'
        AND f.is_deleted = 'N'
  );

-- Verify (expect one ABN_REMARKS row per active workflow, display_order=4, is_mandatory=N):
-- SELECT workflow_id, feedback_code, feedback_name, is_mandatory, display_order
-- FROM workflow_feedback_type_master
-- WHERE feedback_code = 'ABN_REMARKS' AND is_deleted = 'N'
-- ORDER BY workflow_id;
