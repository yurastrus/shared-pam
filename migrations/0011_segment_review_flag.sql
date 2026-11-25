-- Migration 0011 — "flag for re-review" on verification segments.
--
-- Mirrors the camera-trap series flag (observations.flagged / flag_note):
-- a verifier marks a segment on the verification page, an admin sees it on
-- /<lang>/pam/admin/flagged and removes the flag. While the flag is on, the
-- "delete files of completed verifications" cleanup keeps the audio and the
-- spectrogram, so reaching consensus does not lose the evidence.
--
-- The flag does NOT change the segment status and does NOT take it out of the
-- verification queue or the analytics.
--
-- Unlike the camera-trap table (where the author had to be packed into
-- flag_note), the author and time get their own columns here. flagged_by is a
-- user id of the main database (pam_db has no users table, so no FK).
--
-- Backwards-compatible and idempotent: code unaware of the columns keeps working.

ALTER TABLE segments ADD COLUMN IF NOT EXISTS flagged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE segments ADD COLUMN IF NOT EXISTS flag_note TEXT;
ALTER TABLE segments ADD COLUMN IF NOT EXISTS flagged_by INTEGER;
ALTER TABLE segments ADD COLUMN IF NOT EXISTS flagged_at TIMESTAMP;

-- Few rows are ever flagged: a partial index keeps the admin list and badge cheap.
CREATE INDEX IF NOT EXISTS idx_segments_flagged ON segments (flagged_at DESC) WHERE flagged;
