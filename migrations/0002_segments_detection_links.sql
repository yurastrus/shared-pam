-- SPDX-License-Identifier: AGPL-3.0-only
-- Migration 0002 — explicit links from segments to recordings/detections.
--
-- PURPOSE
--   Give the `segments` table a direct, reliable link to the biological event
--   it was cut from. Until now the link was reconstructed post-hoc by parsing
--   the segment filename (see pam_linking_utils.py) — fragile, and impossible
--   for legacy filenames that carry no start-second. The new automated sampling
--   upload path (pam_segment_sampling.py) knows recording_id/detection_id at
--   sampling time and writes them straight into these columns.
--
-- SAFETY
--   Additive only. Adds two NULLABLE columns to `segments`; touches no data and
--   no other table. Existing rows get NULL and keep working exactly as before
--   (verification, filename-based fallback linking, audio serving are all
--   unaffected). Fully idempotent — safe to run more than once.
--
--   The foreign keys are created NOT VALID so that adding them takes only a
--   metadata lock and never scans the (large) segments/detections tables. New
--   rows are still checked; only the pre-existing NULLs are left unvalidated.
--   Once the backfill has run you may validate them in a separate, low-priority
--   step (see PART 2, commented out).
--
--   No UNIQUE constraint on detection_id yet — legacy uploads may contain more
--   than one segment for the same detection, which would break a unique index.
--   Decide on uniqueness only after the backfill collision report is clean
--   (scripts/backfill_pam_segment_links.py --report).
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0002_segments_detection_links.sql

BEGIN;

-- Nullable link columns. BIGINT matches detections.detection_id (see 0001) and
-- is wide enough for recording_id regardless of its own width (int→bigint FK
-- comparison is allowed by Postgres).
ALTER TABLE segments ADD COLUMN IF NOT EXISTS recording_id BIGINT;
ALTER TABLE segments ADD COLUMN IF NOT EXISTS detection_id BIGINT;

-- Foreign keys, added NOT VALID (metadata-only lock, no table scan).
-- Guarded by a catalog check so the migration stays idempotent — ADD
-- CONSTRAINT has no IF NOT EXISTS form.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_segments_recording'
    ) THEN
        ALTER TABLE segments
            ADD CONSTRAINT fk_segments_recording
            FOREIGN KEY (recording_id) REFERENCES recordings(recording_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_segments_detection'
    ) THEN
        ALTER TABLE segments
            ADD CONSTRAINT fk_segments_detection
            FOREIGN KEY (detection_id) REFERENCES detections(detection_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END $$;

-- Indexes: the sampling dedup does NOT EXISTS on segments.detection_id, and the
-- backfill / ON DELETE SET NULL touch both columns. Partial (NOT NULL) keeps
-- them small since most legacy rows stay NULL.
CREATE INDEX IF NOT EXISTS idx_segments_detection_id
    ON segments (detection_id) WHERE detection_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_segments_recording_id
    ON segments (recording_id) WHERE recording_id IS NOT NULL;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- PART 2 — OPTIONAL, run later, outside this file, after the backfill:
--   Validate the FKs (scans only unvalidated rows; a SHARE UPDATE EXCLUSIVE
--   lock, so reads/writes continue). Safe to skip — NOT VALID FKs still enforce
--   on all new/changed rows.
-- ─────────────────────────────────────────────────────────────────────────────
-- ALTER TABLE segments VALIDATE CONSTRAINT fk_segments_recording;
-- ALTER TABLE segments VALIDATE CONSTRAINT fk_segments_detection;
