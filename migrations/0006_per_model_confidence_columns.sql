-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0006 — per-model confidence columns on `detections`.
--
-- PURPOSE
--   Every classifier's score moves into its own column on `detections`, so all
--   models are stored the same way and no model is privileged. This replaces
--   the `detection_models` link table, which stored a 4-byte score inside a
--   ~84-byte row (23-byte tuple header + 8-byte detection_id + primary-key
--   entry). Measured on production: 1979 MB holding 378 MB of numbers, of
--   which 24,728,562 of 24,772,161 rows were a byte-identical copy of
--   `detections.confidence`.
--
--   Keeping the scores inline also preserves the composite index
--   (species_id, <score>) that the dashboards depend on. A score in a separate
--   table splits the "species + threshold" filter across two tables, which no
--   single index can cover: measured 90 ms -> 610..1452 ms per dashboard query.
--
-- MAPPING IS DATA, NOT CODE
--   `models.conf_column` names the column holding that model's score. Nothing
--   in the code may hardcode a model name or a column name; two such hardcodes
--   existed before this migration (utils.get_reference_model_id and
--   utils.get_models_list) and are removed alongside it.
--
--   A model with conf_column IS NULL has nowhere to store scores, so it is
--   neither offered for import nor shown in the dashboard model switcher. That
--   is how Nocmig / Nocmig V2 Beta are disabled here: they have no detections,
--   no segments and no evaluation rows, so nothing breaks. To re-enable one,
--   add a column and set conf_column — no code change needed.
--
--   `detections.confidence` keeps its historical name and belongs to BirdNET
--   2.4. Renaming it would touch ~70 call sites for no functional gain; the
--   COMMENT below records the ownership instead.
--
-- SAFETY
--   Idempotent. ADD COLUMN without a DEFAULT is metadata-only in PostgreSQL 11+
--   (no table rewrite, no long lock). The backfill touches 43,599 rows. No data
--   is destroyed: `detection_models` is left intact and is dropped separately by
--   0007, AFTER the application code that no longer reads it is deployed.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0006_per_model_confidence_columns.sql
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── 1. per-model score columns ───────────────────────────────────────────────
-- One column per model that actually has data. Deliberately NOT adding columns
-- for models with zero detections: `detections` has 6 columns today, and the
-- 9th column pushes the NULL bitmap from 1 byte to 2, which shifts the data
-- offset and makes every NEW row 8 bytes wider. Adding a column later is
-- instant, so there is no reason to pay that now.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS conf_perch_v2 real;

-- ── 2. model_id -> column mapping ────────────────────────────────────────────
ALTER TABLE models ADD COLUMN IF NOT EXISTS conf_column varchar(40);

UPDATE models SET conf_column = 'confidence'
    WHERE name = 'BirdNET' AND version = '2.4' AND conf_column IS NULL;
UPDATE models SET conf_column = 'conf_perch_v2'
    WHERE name = 'Perch' AND version = 'v2' AND conf_column IS NULL;

-- One column may back at most one model.
CREATE UNIQUE INDEX IF NOT EXISTS uq_models_conf_column
    ON models (conf_column) WHERE conf_column IS NOT NULL;

-- Reject anything that could not be safely interpolated into SQL. The
-- application validates this too, but the invariant belongs in the schema.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_models_conf_column') THEN
        ALTER TABLE models
            ADD CONSTRAINT ck_models_conf_column
            CHECK (conf_column IS NULL OR conf_column ~ '^[a-z][a-z0-9_]*$');
    END IF;
END $$;

-- ── 3. backfill Perch scores out of the link table ───────────────────────────
UPDATE detections d
   SET conf_perch_v2 = dm.confidence
  FROM detection_models dm
 WHERE dm.detection_id = d.detection_id
   AND dm.model_id = (SELECT model_id FROM models WHERE conf_column = 'conf_perch_v2')
   AND d.conf_perch_v2 IS NULL;

-- ── 4. index for the new column ──────────────────────────────────────────────
-- Partial: only rows that actually carry a Perch score (43,599 of 24.8M), so
-- this costs ~1 MB rather than the ~770 MB a full index on the column would.
-- Mirrors idx_detections_species_confidence, which serves the same filter shape
-- ("one species, score above a threshold") for the reference model.
CREATE INDEX IF NOT EXISTS idx_detections_species_conf_perch_v2
    ON detections (species_id, conf_perch_v2 DESC)
    WHERE conf_perch_v2 IS NOT NULL;

-- ── 5. document the historical column name ───────────────────────────────────
COMMENT ON COLUMN detections.confidence IS
 'Confidence of the BirdNET 2.4 classifier. The historical name is kept for '
 'compatibility - every other classifier uses a conf_<model> column in this '
 'same table. Resolve the model -> column mapping through models.conf_column; '
 'never hardcode it. NULL means this model did not report the event.';

COMMENT ON COLUMN models.conf_column IS
 'Name of the detections column holding this model''s score. NULL disables the '
 'model: it is not offered for import and not shown in the dashboard switcher.';

COMMIT;

-- ── verification (run manually; expects 43599 / 24728562 / 2 / 0) ────────────
-- SELECT count(*) FILTER (WHERE conf_perch_v2 IS NOT NULL) AS perch_scores,
--        count(*) FILTER (WHERE confidence    IS NOT NULL) AS birdnet_scores
--   FROM detections;
-- SELECT count(*) AS enabled_models FROM models WHERE conf_column IS NOT NULL;
-- -- every link-table row must now be represented by a column value:
-- SELECT count(*) AS unmigrated
--   FROM detection_models dm
--   JOIN models m ON m.model_id = dm.model_id
--   JOIN detections d ON d.detection_id = dm.detection_id
--  WHERE m.conf_column IS NOT NULL
--    AND dm.confidence IS DISTINCT FROM
--        CASE m.conf_column WHEN 'confidence'    THEN d.confidence
--                           WHEN 'conf_perch_v2' THEN d.conf_perch_v2 END;
