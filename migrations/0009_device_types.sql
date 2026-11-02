-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0009 — recorder model per deployment.
--
-- PURPOSE
--   device_types lists recorder models; service_visits.device_type_id says
--   which model an installation visit put on the site. Only installation
--   visits carry it: the device stays the same until the next installation.
--   Older visits keep NULL ("unknown"); nothing is backfilled.
--
-- SAFETY
--   Idempotent. New table plus a nullable column; older code ignores both.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0009_device_types.sql
--   or: venv/bin/python -m scripts.init_pam_device_types
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

CREATE TABLE IF NOT EXISTS device_types (
    id         SERIAL PRIMARY KEY,
    name_ua    VARCHAR(100) NOT NULL UNIQUE,
    name_en    VARCHAR(100) NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 100
);

INSERT INTO device_types (name_ua, name_en, sort_order) VALUES
    ('SM Micro 2', 'SM Micro 2', 10),
    ('SM Micro',   'SM Micro',   20),
    ('SM Mini',    'SM Mini',    30),
    ('SM Mini 2',  'SM Mini 2',  40),
    ('AudioMoth',  'AudioMoth',  50),
    ('Інший',      'Other',      90)
ON CONFLICT (name_ua) DO NOTHING;

ALTER TABLE service_visits
    ADD COLUMN IF NOT EXISTS device_type_id INTEGER REFERENCES device_types(id);

COMMIT;
