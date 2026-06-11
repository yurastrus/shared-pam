# shared-pam

Passive Acoustic Monitoring (PAM) module for the [biomon](https://github.com/yurastrus/biomon) platform. It ingests automated BirdNET classifier output, manages a human-verification workflow for audio segments, calculates per-species accuracy metrics, and produces long-term abundance trends (RAI) and biotope-activity summaries.

The package is a Flask blueprint (`pam_bp`) consumed as a Git submodule by biomon at `app/pam/`.

---

## Database schema

The PAM module uses its own PostgreSQL database (`pam_db`, configured via `PAM_DATABASE_URL`). Tables are managed with raw SQL migrations; the five analytics tables are additionally declared as SQLAlchemy ORM models in `pam_analytics_calculator.py`.

### Core detection pipeline

| Table | Key columns | Purpose |
|---|---|---|
| `species` | `species_id`, `scientific_name`, `common_name_en`, `common_name_uk`, `required_role` | Species catalogue; `required_role` gates access per species |
| `locations` | `location_id`, `name`, `latitude`, `longitude` | Acoustic monitoring stations |
| `recordings` | `recording_id`, `filename`, `location_id`, `datetime_start`, `duration_minutes` | One row per audio file processed by BirdNET |
| `detections` | `detection_id`, `recording_id`, `species_id`, `start_s`, `end_s`, `confidence` | Individual BirdNET detections within a recording |

### Verification pipeline

| Table | Key columns | Purpose |
|---|---|---|
| `segments` | `id`, `species_id`, `filename`, `confidence_level`, `location_name`, `recorded_date`, `recorded_time`, `file_path`, `upload_date`, `status` | Audio clips extracted for human review; `status` tracks lifecycle (`pending` → `completed` → `archived`) |
| `segment_verifications` | `segment_id`, `user_id`, `verification_result`, `verified_at` | One row per verifier per segment; `verification_result` is 1 (positive) or 0 (negative) |
| `detection_verification_map` | `detection_id`, `segment_id`, `result` | Links verified segments back to raw detections; `result` stores the consensus outcome |
| `evaluation` | `species_id`, `precision_score`, `precision_lower_ci`, `precision_upper_ci`, `total_samples`, `logistic_beta0`, `logistic_beta1`, `logistic_r_squared`, `logistic_n_samples`, `logistic_status`, `p0_9_threshold`, `p0_95_threshold`, `p0_99_threshold` (+ CI columns), `calculation_version`, `calculated_by_user_id`, `logistic_calculated_at`, `is_current` | BirdNET accuracy metrics per species; only the row with `is_current = TRUE` is used |

### Organisation & access

| Table | Key columns | Purpose |
|---|---|---|
| `location_institutions` | `location_id`, `institution_id` | Controls which institutions can see which locations |
| `biotopes` | `biotope_id`, `name` | Biotope type definitions |
| `location_biotopes` | `location_id`, `biotope_id` | Biotope assignment per location |

### Analytics (SQLAlchemy ORM)

| Table | Key columns | Purpose |
|---|---|---|
| `analytics_log` | `species_id`, `detection_count`, `last_calculated_at` | Tracks when each species' analytics were last recalculated |
| `species_monitoring_periods` | `species_id`, `start_month`, `end_month` | Phenological window; restricts trend calculations to the active season |
| `analysis_intermediate` | `species_id`, `location_id`, `year`, `month`, `month_part`, `day_part`, `detection_count`, `effort_hours` | Intermediate aggregates used to compute RAI trends |
| `species_yearly_trends` | `species_id`, `year`, `institution_id`, `mean_rai`, `lower_ci`, `upper_ci`, `calculated_at` | Relative Abundance Index per species / year / institution with 95 % bootstrap CI; unique on `(species_id, year, institution_id)` |
| `species_biotope_yearly_activity` | `species_id`, `biotope_id`, `year`, `institution_id`, `detection_count`, `effort_hours` | Detection density per biotope and year; unique on `(species_id, biotope_id, year, institution_id)` |

---

## Flask routes

All page routes are prefixed with `/<lang_code>` (e.g. `/en` or `/uk`). API routes follow the same prefix convention.

### Landing & dashboards

| Method | URL | Auth | Description |
|---|---|---|---|
| GET | `/<lang>/pam` | viewer | PAM hub: links to Analytics, Verification, Management |
| GET | `/<lang>/pam/pam_detailed` | viewer | Species dashboard — scatter plots, bar charts, coverage calendar |
| GET | `/<lang>/pam/pam_overview` | viewer | Ranked species overview table |
| GET | `/<lang>/pam-static/<path>` | — | Serves PAM-specific static files (CSS, JS) |

### Audio segment verification

| Method | URL | Auth | Description |
|---|---|---|---|
| GET | `/<lang>/pam/verification/upload` | manager | ZIP archive upload page |
| POST | `/<lang>/pam/verification/upload/process` | manager | Processes uploaded ZIP; extracts segments, runs auto-linking |
| GET | `/<lang>/pam/verification/segments` | pam_verifier | Paginated segment list with status filters |
| GET | `/<lang>/pam/verification/verify` | pam_verifier | Inline audio player + spectrogram verification interface |
| GET | `/<lang>/audio/segments/<id>` | authenticated | Streams audio file for the given segment |
| GET | `/<lang>/audio/spectrograms/<id>` | authenticated | Returns spectrogram PNG; generates on first request |

### Evaluation

| Method | URL | Auth | Description |
|---|---|---|---|
| GET | `/<lang>/pam/evaluation/results` | viewer | BirdNET accuracy results page (precision, logistic curves) |
| GET | `/<lang>/api/evaluation/detailed-results` | viewer | Paginated detailed results with sorting |
| POST | `/<lang>/admin/evaluation/recalculate` | admin | Triggers full or single-species metrics recalculation |
| POST | `/<lang>/admin/verification/cleanup` | admin | Deletes audio/spectrogram files for `archived` segments |

### Data APIs (charts & tables)

| Method | URL | Description |
|---|---|---|
| GET | `/<lang>/api/pam/get-plot-data` | Detection points for scatter plot |
| GET | `/<lang>/api/pam/get-barchart-data` | Daily detection counts |
| GET | `/<lang>/api/pam/get-time-scatter-data` | Time-of-day activity with sunrise/sunset overlay |
| GET | `/<lang>/api/pam/get-species-summary` | Total detections, unique locations, active days |
| GET | `/<lang>/api/pam/get-unique-points` | Detection locations with counts (for maps) |
| GET | `/<lang>/api/pam/get-species-ranking` | Species ranked by detection count |
| GET | `/<lang>/api/pam/get-overview-stats` | Platform-wide totals (detections, species, locations) |
| GET | `/<lang>/api/pam/get-locations-map` | Location polygons + detection counts for the map layer |
| GET | `/<lang>/api/pam/get-filters-data` | Cascading filter options (institution → location → biotope) |

### Verification APIs

| Method | URL | Auth | Description |
|---|---|---|---|
| GET | `/<lang>/api/verification/segments` | authenticated | Paginated segment list with per-segment stats |
| GET | `/<lang>/api/verification/next-segment` | pam_verifier | Returns the next unverified segment for the current user |
| POST | `/<lang>/api/verification/submit` | pam_verifier | Saves a verification result |
| GET | `/<lang>/api/verification/stats` | authenticated | User's verification totals and per-species breakdown |
| GET | `/<lang>/api/verification/consensus-status` | authenticated | Consensus statistics and top-verifier leaderboard |

---

## Translations / i18n

The module ships an autonomous Babel domain — `pam` — independent of the host application's `messages` domain.

| Item | Value |
|---|---|
| Domain name | `pam` |
| Catalog files | `translations/<locale>/LC_MESSAGES/pam.po/.mo` |
| Extraction config | `babel.cfg` (covers this directory only) |
| Runtime lookup | `domain.py` — `flask_babel.Domain`; falls back to the host `messages` domain when a string is not found |

The blueprint's `__init__.py` injects `_` / `gettext` / `ngettext` into every template via a context processor, so templates use the standard `{{ _('…') }}` syntax without changes.

To update translations (run from the **biomon repo root**):

```bash
# 1. Extract
venv/Scripts/pybabel extract -F app/pam/babel.cfg -k _l -k lazy_gettext -D pam -o app/pam/messages.pot .

# 2. Merge
venv/Scripts/pybabel update -i app/pam/messages.pot -d app/pam/translations -D pam

# 3. Translate new msgstr in translations/en/LC_MESSAGES/pam.po and remove #, fuzzy markers.
#    The uk catalog needs no changes (msgids are already in Ukrainian).

# 4. Compile (-f required)
venv/Scripts/pybabel compile -f -d app/pam/translations -D pam
```

Replace `venv/Scripts/` with `venv/bin/` on Linux.

---

## Integration

This package is registered in biomon's `create_app()` factory as the `pam_bp` blueprint. It connects to a dedicated PostgreSQL database (`PAM_DATABASE_URL`) and uses institution-based access control inherited from the main app's `User` / `Institution` models.

For environment setup, deployment, and the full role hierarchy see the **[biomon README](https://github.com/yurastrus/biomon#readme)**.
