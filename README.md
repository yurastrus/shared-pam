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
| `detections` | `detection_id`, `recording_id`, `species_id`, `start_s`, `end_s`, `confidence`, `conf_perch_v2`, … | One row per biological event `(recording_id, species_id, start_s, end_s)`, with **one score column per classifier**. `confidence` is BirdNET 2.4's column — a historical name, not a special case. A NULL means that model did not report the event |
| `models` | `model_id`, `name`, `version`, `program`, `conf_column` | Classifier-model catalogue. `conf_column` names the `detections` column holding that model's score and is the **only** source of the model → column mapping. `conf_column IS NULL` disables a model: it is neither offered for import nor shown in the dashboard switcher (this is how Nocmig / Nocmig V2 Beta are turned off) |

> **Why the scores are inline** (migration 0006). They used to live in a
> `detection_models(detection_id, model_id, confidence)` link table, where a
> 4-byte score cost ~84 bytes of storage: a 23-byte tuple header, an 8-byte
> `detection_id`, and a primary-key entry. On production that table held
> 1979 MB for 378 MB of numbers, of which 24,728,562 of 24,772,161 rows were a
> byte-identical copy of `detections.confidence`.
>
> The decisive reason is not size, though. Dashboards filter on "one species,
> score above a threshold", and a composite index `(species_id, <score>)` can
> only cover that while both columns live in the same table. With the score in a
> separate table the filter is split across two tables, which no index can
> cover — measured 90 ms → 610…1452 ms per dashboard query, unchanged by
> `random_page_cost` tuning or by rewriting the join as `EXISTS`.
>
> Adding a classifier is `ALTER TABLE detections ADD COLUMN conf_<model> real`
> (metadata-only and instant in PostgreSQL 11+) plus a `models.conf_column`
> value. Prefer adding the column only once that model has data: `detections`
> has 7 columns, and the 9th widens the NULL bitmap from 1 byte to 2, making
> every new row 8 bytes larger.

> Import supports two formats (see `pam_import_utils.py`): **BirdNET CSV** (species resolved by scientific name) and **Raven Selection Table** (`.txt` from BirdNET Analyzer / Chirpity; species resolved by English common name, using the per-recording **File Offset** for `start_s`). Schema migrations live in [`migrations/`](migrations/).

### Verification pipeline

| Table | Key columns | Purpose |
|---|---|---|
| `segments` | `id`, `species_id`, `filename`, `confidence_level`, `location_name`, `recorded_date`, `recorded_time`, `file_path`, `upload_date`, `status`, `recording_id`, `detection_id`, `model_id` | Audio clips extracted for human review; `status` tracks lifecycle (`pending` → `completed` → `archived`); `model_id` is the classifier the clip was sampled from and `confidence_level` holds that model's own score (legacy rows backfilled to the BirdNET 2.4 reference) |
| `segment_verifications` | `segment_id`, `user_id`, `verification_result`, `verified_at` | One row per verifier per segment; `verification_result` is 1 (positive) or 0 (negative) |
| `detection_verification_map` | `detection_id`, `segment_id`, `result` | Links verified segments back to raw detections; `result` stores the consensus outcome |
| `evaluation` | `species_id`, `model_id`, `precision_score`, `precision_lower_ci`, `precision_upper_ci`, `total_samples`, `logistic_beta0`, `logistic_beta1`, `logistic_r_squared`, `logistic_n_samples`, `logistic_status`, `p0_9_threshold`, `p0_95_threshold`, `p0_99_threshold` (+ CI columns), `calculation_version`, `calculated_by_user_id`, `logistic_calculated_at`, `is_current` | Accuracy metrics **per (species, model)**; the current row is unique per `(species_id, model_id)` (only `is_current = TRUE` is used). Recalculation computes every model that has verified segments; the results page has a model switcher, defaulting to the BirdNET 2.4 reference |

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
# 1. Extract  (no -D here: pybabel extract has no such option, the domain is
#              set on update/compile)
venv/Scripts/pybabel extract -F app/pam/babel.cfg -k _l -k lazy_gettext -o app/pam/messages.pot .

# 2. Merge
venv/Scripts/pybabel update -i app/pam/messages.pot -d app/pam/translations -D pam

# 3. Translate new msgstr in translations/en/LC_MESSAGES/pam.po and remove #, fuzzy markers.
#    The uk catalog needs no changes (msgids are already in Ukrainian).
#    Read every fuzzy msgstr before clearing the flag: pybabel guesses from the
#    nearest existing msgid, and the guesses can be plain wrong (it once
#    rendered "Квітень" as "Wind" from a weather column). Step 4 uses -f, so a
#    fuzzy guess left in place ships to users.

# 4. Compile (-f required)
venv/Scripts/pybabel compile -f -d app/pam/translations -D pam
```

Replace `venv/Scripts/` with `venv/bin/` on Linux.

---

## Biotope auto-assignment from landcover

Admin hub (`GET /<lang>/pam/admin`, admin-only) with a background action that
assigns biotopes to monitoring points from ESA WorldCover (10 m) via Google Earth
Engine — the PAM counterpart of camera_traps' feature. For each point it samples
the landcover histogram in a radius (default 100 m), takes the top-N classes
(default 3), maps them to biotopes via `biotope_landcover_map`, and **adds** the
missing ones (`ON CONFLICT DO NOTHING` on `location_biotopes` — existing biotopes
are never removed).

- **Module:** `biotope_autoassign.py` — self-contained (does NOT import app.sdm /
  app.camera_traps). Lazy `import ee`, own GEE init from `GEE_SERVICE_ACCOUNT_KEY`,
  `frequencyHistogram` over `Point.buffer(radius)`, additive assignment, background
  `threading.Thread`. `gee_landcover_available()` gates the button; the run is
  wrapped so any failure is recorded, never crashing the site (503 when GEE is off).
- **Routes:** `POST /<lang>/pam/admin/biotopes/auto-assign` (202/409/503),
  `GET .../status` (polling). Template: `pam_admin.html`.
- **Supporting tables** (created idempotently by `ensure_schema()` — no committed
  init script; the DDL lives in the module docstring and self-heals on first use):
  - `biotope_landcover_map (worldcover_class UNIQUE, biotope_id FK)` — the class →
    biotope mapping, managed **directly in the DB**, not in the UI.
  - `pam_calculation_log (source_name UNIQUE, status, started_at, …)` — a generic
    keyed status log for background jobs (polled by the admin page).
- **Seeding (pam_db, one-off):** PAM already has a rich biotope set, so only the
  general biotopes with no good existing match are added — **`Ліс` / `Forest`**
  (class 10), **`Оголений ґрунт` / `Bare / sparse vegetation`** (class 60, i.e.
  bare/sand/desert, not cliffs), and **`Водно-болотне угіддя` / `Wetland`**
  (class 90, broader than reeds). The other classes map to existing biotopes
  (20→Кущі, 30→Лука, 40→C/г поля, 50→Населені пункти, 80→Озера та водосховища).
  Snow/mangroves/moss omitted as irrelevant for Ukraine. Bulk assignment is
  triggered by the admin button, not automatically.

---

## Simultaneous detections (co-occurrence), Beta since 2026-09-10

A lower bound on the number of individuals calling at the same time, from data
already in `pam_db`. If one species is detected at N sufficiently separated
locations inside one short time window, at least N individuals were calling in
that window. **Admin-only for now**, both the page and the hub card: the output
is easy to over-read as an abundance estimate.

Ported from the `Одночасні детекції` CLI prototype (desktop), with matplotlib
figures replaced by Plotly plots and a Leaflet web map.

### What it is not

Not an abundance estimate. It says nothing about silent individuals, and it is
not effort-normalised: a window in which only three recorders were running
cannot produce a high count.

### Files

| File | Role |
| --- | --- |
| `cooccurrence.py` | the whole numeric core: window SQL, projection, spacing rule, width sweep. Flask-free apart from taking an open SQLAlchemy connection, so it is unit-testable without an app context |
| `templates/pam_cooccurrence.html` | the page: filter panel, KPI tiles, histogram, two-mode Leaflet map, sweep panels |
| `routes.py` | `GET /<lang>/pam/cooccurrence` (login + admin), plus `POST .../run`, `POST .../sweep`, `GET .../export` under `/api/pam/cooccurrence/` |

### Nothing computes on page load

Opening the page runs only the cheap lookups (species list, period bounds, and
the shared `/api/pam/get-filters-data` cascade). The analysis starts on
**Розрахувати**; the width sweep is a second button, because it costs more and
is not always wanted. Same shape as the other PAM analytics pages.

### Filters

Species and period, plus five narrowing controls:

* **Ecoregion** ("Розточчя", "Карпати", "Полісся", …). Not a pam_db concept:
  it is `institutions.ecoregion_uk` in the HOST database, exactly what the
  camera-traps scope picker reads. `_cooc_ecoregions` lists them and
  `_cooc_institution_ids` expands the pick to institution ids, which pam_db's
  `location_institutions` already joins on. Ecoregion and institution are a
  **union**, not an intersection. Each `<option>` also carries
  `data-institutions`, so the biotope/location cascade narrows client-side the
  same way the query will.
* **Institution / biotope / location** — the shared
  `/api/pam/get-filters-data` cascade, as on the other analytics pages.
* **Season window**, picked in **decades** (ten-day thirds of a month), which
  is the unit field seasons are named in: "April, first decade" to "May, second
  decade" rather than two bare dates. `season_decades()` builds the 36 rows;
  the "from" list offers the first day of each decade and the "to" list the
  last, so that example resolves to 1 April … 20 May. A third decade runs to
  the end of its month, and February's takes 29, so the window covers
  29 February in leap years and matches nothing on that day in ordinary ones.

  The season window **narrows the date range, it does not replace it**: the
  clause is an extra AND inside the period, so both filters keep their full
  effect. Measured on the dev data: whole record 6 692 detections → 3 248 for
  1 Apr–20 May across all years → 3 177 when the period is additionally cut to
  2025 → 0 when it is cut to 2024.

  The comparison is `month * 100 + day`, **not** day-of-year, because
  day-of-year shifts by one after 29 February and would silently move the
  window in leap years. A window that wraps the new year (1 Dec to 15 Feb) is
  the union of two ranges, and `season_clause` writes that case out
  separately.

### Human verification is visible, not hidden

A detection a person confirmed is a different kind of evidence from a
classifier score, so it is on the surface. The window query joins
`detection_verification_map` (keyed on the authoritative
`segments.detection_id`; no filename or datetime heuristics) and
`segments.positive_verifications`, and `verification_level` grades each
(window, location):

| Level | Meaning | Map colour |
| --- | --- | --- |
| 2 | consensus on a detection, or two or more positive votes | green ramp |
| 1 | exactly one positive vote | green ramp |
| 0 | nobody has listened | blue `#2c7fb8` |
| -1 | people listened and rejected it | red `#b30000` |

The green is a **ramp, not two fixed shades**: the shade carries how many
people confirmed the point, scaled to `max_positive_votes` for the whole run
rather than per window, so a shade means the same thing while flipping between
windows and the darkest green in view really is the best-verified point in the
selection. Five ColorBrewer Greens stops, neither extreme (the palest vanishes
on a light basemap, the darkest reads as black). Consensus keeps a darker
outline, so that fact is not lost when the vote count alone gives a pale fill.

Today the whole database tops out at **two** positive votes per segment: two
verifiers reach consensus and the segment stops being served, so the ramp
usually shows two shades. It spreads over more on its own as verification
deepens, with no code change. `verification_level` is still what the ranking
and the CSV use; the ramp is a display of `positive_votes`.

Windows are ordered by `n_verified`, then `max_level`, then `max_votes`, so a
better-verified window leads among equally verified ones.

Red therefore always means "a person said no"; a location suppressed by the
spacing rule is a dashed **grey** circle, not a red one.

The Top-windows table lists only windows reaching `TABLE_MIN_COUNTED` = 2
well-separated locations. A single location carries no simultaneity however
many detections it holds: one bird singing twelve times in a minute is still
one bird, so those rows were noise in a table about co-occurrence. The
histogram, the KPI tiles and the CSV export still cover every window, because
the shape of the whole distribution is what shows how rare the co-occurrences
are, and the export is the audit trail. When nothing in the selection reaches
two, the list falls back to single-location windows and says so rather than
going blank.

Windows carrying a confirmed counted location rank above merely large ones,
because those are the citable ones. The returned slice is the union of the
verification-ranked head and the count-ranked head, so a three-location peak
cannot be pushed off a list full of single-location verified windows, and
`peak_window` stays a property of the bound (max `n_counted`), not of the list
order. The table sorts on every column client-side, so either view is one
click.

**Counting is not changed by any of this.** A rejected detection still occupies
its window; it is only labelled. Dropping consensus-rejected detections from
the bound is a defensible next step and a separate decision.

### The two parameters that decide the answer

* **Minimum distance** handles pseudo-replication. `cluster_method` says how:
  `greedy` (default) enforces the rule *inside each window*, keeping a
  largest-first subset of the detecting locations that is pairwise at least
  `min_distance_m` apart, so no location is discarded a priori and the greedy
  answer stays a lower bound. `complete` builds global clusters of bounded
  diameter. `single` builds global connected components and is offered for
  comparison only: in a dense network it chains, and at 1000 m the Розточчя
  network collapses from 61 locations to 4 clusters.
* **Confidence threshold** handles classifier error. A false positive at two distant
  points in the same minute fabricates a "simultaneous pair". The page takes a
  **raw confidence** on the chosen model column (`detections.confidence` or
  `conf_perch_v2`, whitelisted because the name is interpolated into SQL).
  Default `DEFAULT_MIN_CONF = 0.95`, which leans strict: a false positive at
  two distant points in the same minute fabricates a simultaneous pair.
  Reading the threshold off the species logistic precision fit in `evaluation`,
  the way `/pam/pam_detailed` does, is a deliberate later step.

### How the query works

Detection time is not stored: it is reconstructed as
`recordings.datetime_start + detections.start_s`, then binned with
`date_bin(width, ts, origin)`. All aggregation is server-side, so only the small
(window, location) table travels to Python. Shifting `origin` by less than the
width is what makes overlapping windows possible, at one database pass per
offset (`MAX_DB_PASSES` caps that).

The sweep works differently, because binning in SQL fixes one width per query.
It fetches the raw event times once and scans them in Python, which covers every
width in a single round trip and makes the maximum **exact over all window
positions** rather than only over aligned bins: the maximum is always attained
by a window whose left edge sits on a detection.

### Limits, all enforced server-side

`STATEMENT_TIMEOUT_MS` 30 s per query (`SET LOCAL`, so the pooled connection
goes back unchanged), `MAX_DB_PASSES` 12, `SWEEP_MAX_EVENTS` 20 000 and
`SWEEP_MAX_WIDTHS` 120. The sweep cost is (events × widths) in Python, hence a
limit far below the CLI prototype's 400 000, which was tuned for a script
nobody waits on. Everything runs synchronously; the heaviest observed case is
about ten seconds.

### Map

One Leaflet container, two modes. **Window** draws the whole filtered network as
grey dots, counted locations as filled circles sized by detection count,
locations suppressed by the spacing rule as dashed open circles, and around each
counted point a circle of *half* the minimum spacing, so the rule holds exactly
when no two circles overlap. **Network** (after a sweep) draws every co-detecting
pair as a line coloured and thickened by the smallest window at which that pair
ever co-occurs, with node size showing the number of partners.

### From a point to the verification queue

Each popup on the window map offers one action, chosen by what the point
actually needs:

* **Верифікувати (N)** — for any `pam_verifier`, when the point still has
  segments this person could vote on. Links to
  `/pam/verification/verify?species_id=&location_ids=&from_ts=&to_ts=&scope_label=`.
* **Підготувати сегменти** — admin only, shown when nothing is left to verify
  (usually because no segments were ever cut for those detections). Links to
  `/pam/verification/sample-upload?location_ids=&species=&year_start=&year_end=&month_start=&month_end=&conf=`.

`n_verifiable` per (window, location) comes from the window query itself:
segments of those detections that are `pending` and carry no vote from this
user (a skip or an "unknown" counts as a vote and hides the segment). The
predicate is `verifiable_expr()`, and the host's segment access baseline is
**passed in** rather than rebuilt, so the page and the queue cannot drift apart
on who may see what. With no verifier asking, the whole expression collapses to
a literal `0`.

Both target pages had to learn to read those parameters:

* `/api/verification/next-segment` gained `location_ids`, `from_ts`, `to_ts`,
  applied through `segments.detection_id` → `detections` → `recordings`.
  This has to go through the detection, not the segment:
  `segments.recorded_date/recorded_time` is the **recording's** start, shared by
  every segment cut from that recording, so it cannot tell one minute from
  another. Timestamps are parsed by `_parse_ts_arg`, which tolerates the two
  ways a URL mangles an offset (`+00:00` arriving as a space, and `Z`) and
  answers 400 rather than leaking a database error.
* `pam_verification_interface.html` carries the three values into every request
  of the flow (next-segment, stats, cascade), seeds `class_name` and
  `institution_ids` too, and shows a banner naming the narrowing with a link
  that drops it. Without the banner a verifier cannot tell why the queue is
  three segments long.
* `pam_sample_upload.html` gained `applyPrefill()`. The chain there is
  institution → locations → species (the species list is server-side per
  location), so the prefill awaits each step instead of setting five fields at
  once.

One limit worth knowing: popup actions exist only for points in **listed**
windows, i.e. those with two or more well-separated locations. A verifiable
detection sitting in a single-location window is not reachable this way, by
the same design decision that keeps singles out of the table.

### Export

`GET /api/pam/cooccurrence/export` returns a ZIP with the same two tables the
CLI prototype writes (`__windows_locations.csv`, `__window_summary.csv`) plus a
README of the run parameters. Unlike `/run` it keeps every window, not just the
ones the map shows, so the page and the export can be checked against each
other.

### Next steps

* Drop consensus-rejected detections from the bound, not just label them.
* Threshold from the logistic precision fit (a precision target instead of a raw
  score), and the width-against-precision curves the prototype drew as Figure 5.
* Effort normalisation: divide by the number of locations actually recording in
  each window, derivable from `recordings`.
* A real region filter. `locations.state_province` currently holds one value
  (`Lviv Oblast`), so the institution and location pickers are the only usable
  way to name a region.

---

## Integration

This package is registered in biomon's `create_app()` factory as the `pam_bp` blueprint. It connects to a dedicated PostgreSQL database (`PAM_DATABASE_URL`) and uses institution-based access control inherited from the main app's `User` / `Institution` models.

For environment setup, deployment, and the full role hierarchy see the **[biomon README](https://github.com/yurastrus/biomon#readme)**.

---

## License: GNU AGPL-3.0

Copyright (C) 2025–2026 Iurii Strus.

This program is free software: you can redistribute it and/or modify it under
the terms of the **GNU Affero General Public License, version 3** as published
by the Free Software Foundation. See the [LICENSE](LICENSE) file for the full
text (`SPDX-License-Identifier: AGPL-3.0-only`).

Because this is an AGPL-licensed network application: if you run a modified
version of this module on a server and let users interact with it over a
network, you must also make the complete corresponding source code of your
modified version available to those users (AGPL §13).
