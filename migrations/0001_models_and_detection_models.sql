-- SPDX-License-Identifier: AGPL-3.0-only
-- Migration 0001 — classifier-model accounting for the PAM detection pipeline.
--
-- PURPOSE
--   Record which classifier model produced each detection, allowing several
--   models to share one biological event. Introduces:
--     * models            — catalogue of classifier models
--     * detection_models  — link table (detection_id, model_id, confidence)
--
-- SAFETY
--   Additive only. Creates two NEW tables; does NOT alter `detections`,
--   `recordings`, `species` or any existing table. The backfill only READS
--   `detections` and writes into the new `detection_models` table. Fully
--   idempotent — safe to run more than once.
--
--   `detections.confidence` keeps its original meaning: the confidence of the
--   REFERENCE model (BirdNET 2.4). It is never turned into a cross-model max,
--   so existing BirdNET threshold filtering and evaluation are unaffected.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0001_models_and_detection_models.sql
--   The DDL/seed run in one transaction; the backfill procedure commits in
--   chunks (it must run outside an explicit transaction block).

-- ─────────────────────────────────────────────────────────────────────────────
-- PART 1 — schema + seed + backfill procedure (transactional)
-- ─────────────────────────────────────────────────────────────────────────────
BEGIN;

-- Catalogue of classifier models.
-- `version` is NOT NULL with an empty-string sentinel so that UNIQUE(name,
-- version) stays reliable and the seed below is truly idempotent (a NULL
-- version would defeat the unique index, since NULL <> NULL in SQL).
CREATE TABLE IF NOT EXISTS models (
    model_id    SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL DEFAULT '',
    program     TEXT,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

-- Seed the four models. BirdNET 2.4 is the reference model; the existing
-- BirdNET CSV import path maps to it. Idempotent via ON CONFLICT (name,version).
INSERT INTO models (name, version, program, description) VALUES
    ('BirdNET', '2.4',     'BirdNET Analyzer', 'Reference model; legacy BirdNET CSV imports map here'),
    ('Perch',   'v2',      'Chirpity',         'Google Perch v2 classifier (via Chirpity)'),
    ('Nocmig',  '',        'Chirpity',         'Nocmig nocturnal flight-call model (via Chirpity)'),
    ('Nocmig',  'V2 Beta', 'Chirpity',         'Nocmig V2 Beta nocturnal flight-call model (via Chirpity)')
ON CONFLICT (name, version) DO NOTHING;

-- Link table: one row per (detection, model) with that model's own confidence.
-- detections stays one row per biological event; multiple models attach here.
CREATE TABLE IF NOT EXISTS detection_models (
    detection_id BIGINT NOT NULL REFERENCES detections(detection_id) ON DELETE CASCADE,
    model_id     INT    NOT NULL REFERENCES models(model_id),
    confidence   REAL,
    PRIMARY KEY (detection_id, model_id)
);

-- Pivoting/filtering by model (Task B dashboards, Task C per-model evaluation).
CREATE INDEX IF NOT EXISTS idx_detection_models_model ON detection_models (model_id);

-- Chunked, resumable backfill. Commits per chunk so it never holds a single
-- transaction over ~19M rows. ON CONFLICT DO NOTHING makes re-runs cheap.
CREATE OR REPLACE PROCEDURE pam_backfill_detection_models(p_chunk INT DEFAULT 500000)
LANGUAGE plpgsql AS $$
DECLARE
    v_ref INT;
    v_lo  BIGINT := 0;
    v_max BIGINT;
BEGIN
    SELECT model_id INTO v_ref FROM models WHERE name = 'BirdNET' AND version = '2.4';
    IF v_ref IS NULL THEN
        RAISE EXCEPTION 'Reference model "BirdNET 2.4" is not seeded; cannot backfill.';
    END IF;

    SELECT max(detection_id) INTO v_max FROM detections;
    IF v_max IS NULL THEN
        RETURN;  -- no detections yet
    END IF;

    WHILE v_lo < v_max LOOP
        INSERT INTO detection_models (detection_id, model_id, confidence)
        SELECT d.detection_id, v_ref, d.confidence
        FROM detections d
        WHERE d.detection_id > v_lo
          AND d.detection_id <= v_lo + p_chunk
        ON CONFLICT (detection_id, model_id) DO NOTHING;
        COMMIT;
        v_lo := v_lo + p_chunk;
    END LOOP;
END;
$$;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- PART 2 — run the backfill (autocommit; procedure commits per chunk)
-- ─────────────────────────────────────────────────────────────────────────────
CALL pam_backfill_detection_models();

-- The procedure is a one-off helper; drop it once the backfill is done.
-- (Re-running this whole file recreates and re-calls it — still idempotent.)
DROP PROCEDURE IF EXISTS pam_backfill_detection_models(INT);
