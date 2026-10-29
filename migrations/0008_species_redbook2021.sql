-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0008 — Red Data Book of Ukraine (2021) status on species.
--
-- PURPOSE
--   species.redbook2021 holds the category from the ministry order No. 29 of
--   19.01.2021 as a language-neutral code; NULL = not in the Red Book.
--   Values are filled by scripts/redbook2021/import_redbook_2021.py and can be
--   edited on the admin species page.
--
-- SAFETY
--   Idempotent. Nullable column, existing rows get NULL. Older code ignores it.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0008_species_redbook2021.sql
--   or: venv/bin/python -m scripts.init_redbook2021
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE species ADD COLUMN IF NOT EXISTS redbook2021 VARCHAR(30);

ALTER TABLE species DROP CONSTRAINT IF EXISTS species_redbook2021_check;
ALTER TABLE species ADD CONSTRAINT species_redbook2021_check CHECK (
    redbook2021 IS NULL OR redbook2021 IN (
        'extinct', 'extinct_in_wild', 'endangered', 'vulnerable',
        'rare', 'not_evaluated', 'data_deficient'));

COMMIT;
