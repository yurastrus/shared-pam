-- SPDX-License-Identifier: AGPL-3.0-only
-- Migration 0005 — model-aware segments + per-model evaluation.
--
-- PURPOSE
--   Make the human-verification + accuracy-evaluation pipeline aware of WHICH
--   classifier model a sampled segment was drawn from, so precision / logistic
--   metrics can be computed and displayed per model (BirdNET 2.4, Perch, Nocmig …)
--   rather than only for the BirdNET reference.
--
--   Adds:
--     * segments.model_id    — the model whose detection this segment was cut
--                              from (and whose confidence lives in
--                              segments.confidence_level for that segment).
--     * evaluation.model_id  — the model a metrics row was computed for; the
--                              is_current row is now unique per (species, model).
--
-- SAFETY
--   Additive only. Two NULLABLE columns; no existing column is altered or
--   dropped. FKs are added NOT VALID (metadata-only lock, no table scan) — new
--   rows are enforced, pre-existing NULLs left unvalidated (validate later, see
--   PART 2). Fully idempotent — safe to run more than once.
--
--   The backfill maps every legacy row to the BirdNET 2.4 reference model. That
--   is correct: until this migration, every sampled segment was cut from the
--   BirdNET reference confidence (segments.confidence_level == detections.confidence
--   == the reference score), and every evaluation row was a BirdNET metric.
--   So after the backfill the reference-model view reproduces today's behaviour
--   exactly, and other models simply have no rows yet.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0005_model_aware_segments_evaluation.sql
--
--   Depends on migration 0001 (models table + BirdNET 2.4 seed) and 0002
--   (segments.detection_id). Apply those first.

BEGIN;

-- Resolve the reference model once; abort loudly if 0001 was never applied.
DO $$
DECLARE
    v_ref INT;
BEGIN
    SELECT model_id INTO v_ref FROM models WHERE name = 'BirdNET' AND version = '2.4';
    IF v_ref IS NULL THEN
        RAISE EXCEPTION 'Reference model "BirdNET 2.4" is not seeded (run migration 0001 first).';
    END IF;
END $$;

-- ── segments.model_id ─────────────────────────────────────────────────────────
ALTER TABLE segments ADD COLUMN IF NOT EXISTS model_id INT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_segments_model') THEN
        ALTER TABLE segments
            ADD CONSTRAINT fk_segments_model
            FOREIGN KEY (model_id) REFERENCES models(model_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END $$;

-- Per-model sampling dedup (NOT EXISTS on detection_id + model_id) and per-model
-- evaluation grouping both filter on this column; partial keeps it small.
CREATE INDEX IF NOT EXISTS idx_segments_model_id
    ON segments (model_id) WHERE model_id IS NOT NULL;

-- Backfill every legacy segment to the reference model (see SAFETY).
UPDATE segments
SET model_id = (SELECT model_id FROM models WHERE name = 'BirdNET' AND version = '2.4')
WHERE model_id IS NULL;

-- ── evaluation.model_id ───────────────────────────────────────────────────────
ALTER TABLE evaluation ADD COLUMN IF NOT EXISTS model_id INT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_evaluation_model') THEN
        ALTER TABLE evaluation
            ADD CONSTRAINT fk_evaluation_model
            FOREIGN KEY (model_id) REFERENCES models(model_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END $$;

-- Backfill every legacy metrics row to the reference model.
UPDATE evaluation
SET model_id = (SELECT model_id FROM models WHERE name = 'BirdNET' AND version = '2.4')
WHERE model_id IS NULL;

-- The "current" row is now per (species, model). A plain composite index backs
-- the WHERE is_current AND model_id = :m lookups the app now runs. The app also
-- guarantees a single current row per (species, model) by clearing is_current
-- before each insert (recalculate_all_metrics), so a hard UNIQUE index is left
-- OPTIONAL (see PART 2) — it must not fail this migration on any pre-existing
-- duplicate is_current row.
CREATE INDEX IF NOT EXISTS idx_evaluation_current_species_model
    ON evaluation (species_id, model_id) WHERE is_current;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- PART 2 — OPTIONAL, run later once you have confirmed there is exactly one
-- current row per (species, model):
--
--   -- one-time cleanup if duplicates exist (keeps the newest per species+model):
--   -- WITH ranked AS (
--   --   SELECT ctid, ROW_NUMBER() OVER (PARTITION BY species_id, model_id
--   --                                   ORDER BY calculated_at DESC) rn
--   --   FROM evaluation WHERE is_current)
--   -- UPDATE evaluation e SET is_current = FALSE
--   -- FROM ranked r WHERE e.ctid = r.ctid AND r.rn > 1;
--
--   -- then enforce it:
--   -- CREATE UNIQUE INDEX CONCURRENTLY uq_evaluation_current_species_model
--   --   ON evaluation (species_id, model_id) WHERE is_current;
--
--   -- and validate the FKs (scans only unvalidated rows, SHARE UPDATE EXCLUSIVE):
--   -- ALTER TABLE segments   VALIDATE CONSTRAINT fk_segments_model;
--   -- ALTER TABLE evaluation VALIDATE CONSTRAINT fk_evaluation_model;
-- ─────────────────────────────────────────────────────────────────────────────
