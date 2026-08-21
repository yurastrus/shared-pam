-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0007 — drop the `detection_models` link table.
--
-- PURPOSE
--   Phase 2 of the change started in 0006. Every score now lives in a
--   `detections.conf_<model>` column, so the link table holds nothing that is
--   not derivable from `detections`. Dropping it returns ~1979 MB to the
--   operating system immediately: DROP TABLE unlinks the files, so unlike a
--   mass DELETE it needs no VACUUM FULL and no temporary disk headroom. That
--   matters here — the root filesystem had 5.3 GB free of 38 GB.
--
-- DO NOT RUN THIS UNTIL
--   1. 0006 has been applied, and
--   2. the application code that reads conf_<model> columns is deployed and
--      verified on production (dashboard model switcher in all three modes:
--      reference / a specific model / combined; plus one import of each
--      supported format).
--   Until this migration runs, a rollback is just redeploying the previous
--   code — the table is still there, fully populated.
--
-- SAFETY
--   Verifies first that no score would be lost, and aborts if any row in the
--   link table disagrees with the column value. Only then drops.
--
-- REVERSAL
--   The table is fully reconstructible from `detections`, because it never held
--   independent information:
--
--     CREATE TABLE detection_models (
--         detection_id BIGINT NOT NULL REFERENCES detections(detection_id) ON DELETE CASCADE,
--         model_id     INT    NOT NULL REFERENCES models(model_id),
--         confidence   real,
--         PRIMARY KEY (detection_id, model_id));
--     INSERT INTO detection_models (detection_id, model_id, confidence)
--         SELECT d.detection_id, m.model_id, d.confidence
--           FROM detections d, models m
--          WHERE m.conf_column = 'confidence'    AND d.confidence    IS NOT NULL
--     UNION ALL
--         SELECT d.detection_id, m.model_id, d.conf_perch_v2
--           FROM detections d, models m
--          WHERE m.conf_column = 'conf_perch_v2' AND d.conf_perch_v2 IS NOT NULL;
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0007_drop_detection_models.sql
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── guard: refuse to drop if any score is not represented by a column ────────
DO $$
DECLARE
    v_unmigrated bigint;
    v_unmapped   bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'detection_models') THEN
        RAISE NOTICE 'detection_models already dropped - nothing to do';
        RETURN;
    END IF;

    -- Rows belonging to a model that has no column: those WOULD lose data.
    SELECT count(*) INTO v_unmapped
      FROM detection_models dm
      JOIN models m ON m.model_id = dm.model_id
     WHERE m.conf_column IS NULL;

    IF v_unmapped > 0 THEN
        RAISE EXCEPTION 'ABORT: % detection_models row(s) belong to a model with '
                        'no conf_column - add the column and backfill first',
                        v_unmapped;
    END IF;

    -- Rows whose column value disagrees with the link table.
    SELECT count(*) INTO v_unmigrated
      FROM detection_models dm
      JOIN models m     ON m.model_id = dm.model_id
      JOIN detections d ON d.detection_id = dm.detection_id
     WHERE dm.confidence IS DISTINCT FROM
           CASE m.conf_column WHEN 'confidence'    THEN d.confidence
                              WHEN 'conf_perch_v2' THEN d.conf_perch_v2 END;

    IF v_unmigrated > 0 THEN
        RAISE EXCEPTION 'ABORT: % row(s) not migrated to conf_<model> columns - '
                        're-run 0006 before dropping', v_unmigrated;
    END IF;
END $$;

DROP TABLE IF EXISTS detection_models;

COMMIT;

-- Reclaiming index space elsewhere is NOT part of this migration. `detections`
-- still carries idx_detections_species_id and idx_detections_recording_id,
-- which are redundant prefixes of composite indexes, plus
-- detections_confidence_idx (625 MB, 936 scans). Those are a separate,
-- reversible change - DROP INDEX also returns space immediately.
