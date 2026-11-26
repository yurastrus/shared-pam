# SPDX-License-Identifier: AGPL-3.0-only
"""Automated, confidence-stratified segment sampling and upload.

This is the server side of the "sample upload" page: a parallel, automated path
to the manual ZIP upload in ``pam_upload_utils.py``. Instead of the operator
hand-cutting segments locally and zipping them, the server draws a
confidence-stratified sample of *detections* for a chosen location + species
(mirroring ``produce_random_segments.R``), the browser cuts those windows out of
the operator's local recordings, and each cut is streamed back here to be
registered.

Two things make this path better than the ZIP path:

1. **Explicit links.** We know ``detection_id`` / ``recording_id`` at sampling
   time, so they are written straight into the ``segments`` columns added by
   migration 0002 — no fragile filename parsing after the fact.
2. **Dedup.** The sampling query skips detections that already have a segment
   (``NOT EXISTS`` on ``segments.detection_id``), so a detection uploaded via
   the old path — once backfilled — is never sampled again.

The generated filename keeps the canonical
``<conf>_<LOCATION>_<YYYYMMDD>_<HHMMSS>_sec<start>_part<N>.<ext>`` shape so the
verification UI and the legacy filename-based linker keep working unchanged.
"""
import io
import ntpath
import os
import re
import math
import shutil
import time
from collections import defaultdict
from datetime import datetime

from flask import current_app
from sqlalchemy import text

from .utils import get_pam_db_connection

# Canonical recording stem: LOCATION_YYYYMMDD_HHMMSS (location = single token,
# no underscore). This is what the verification UI / legacy linker expect to be
# embedded in a segment filename.
_RECORDING_STEM_RE = re.compile(r'^([A-Za-z0-9\-]+)_(\d{8})_(\d{6})$')

# Sane bounds for the UI-selectable knobs, enforced server-side too.
ALLOWED_SEGMENT_DURATIONS = (3, 5, 10)
MAX_SAMPLE_PER_SPECIES = 5000

# Earliest recording year, cached for the process lifetime. The floor moves only
# when a *historical* import lands, which is rare, so the page must not pay a
# MIN() over recordings on every render. The TTL means such an import shows up
# without a restart.
EARLIEST_YEAR_TTL_SECONDS = 24 * 3600
# A garbage datetime_start (bad import) must not drag the year selector back to
# year 1. Recording hardware older than this is not a case we have to serve.
EARLIEST_YEAR_FLOOR = 1990
_earliest_year_cache = None      # (year, cached_at_monotonic)

# conf_column is interpolated into SQL text, so it is validated before use. It
# comes from models.conf_column (constrained by ck_models_conf_column in
# migration 0006), never from request data.
_CONF_COLUMN_RE = re.compile(r'^[a-z][a-z0-9_]*$')


# ── sampling ──────────────────────────────────────────────────────────────────

#: Verification subsets offered by the local export (destination = local folder).
#: State of a detection, from every segment cut from it whatever the model:
#:   full       consensus recorded in detection_verification_map;
#:   partial    real votes (yes/no) on its segments, but no consensus yet;
#:   unverified neither.
VERIFICATION_FILTERS = ('any', 'unverified', 'verified', 'partial', 'full')
#: Verdict among the verified: consensus when there is one, else the majority of
#: real votes. A tied partial detection has no verdict and passes only 'any'.
VERDICT_FILTERS = ('any', 'confirmed', 'rejected')

_VERIFICATION_SQL = {
    'any': None,
    'unverified': "dvm.verification_result IS NULL AND COALESCE(sv.votes, 0) = 0",
    'verified': "(dvm.verification_result IS NOT NULL OR COALESCE(sv.votes, 0) > 0)",
    'partial': "dvm.verification_result IS NULL AND COALESCE(sv.votes, 0) > 0",
    'full': "dvm.verification_result IS NOT NULL",
}
_VERDICT_SQL = {
    'any': None,
    'confirmed': ("(dvm.verification_result = 1 OR (dvm.verification_result IS NULL"
                  " AND 2 * COALESCE(sv.positive, 0) > COALESCE(sv.votes, 0)))"),
    'rejected': ("(dvm.verification_result = 0 OR (dvm.verification_result IS NULL"
                 " AND COALESCE(sv.votes, 0) > 0"
                 " AND 2 * COALESCE(sv.positive, 0) < COALESCE(sv.votes, 0)))"),
}


def build_sampling_query(n_strata, conf_column='confidence',
                         with_conf_max=False, month_mode=None,
                         with_year_range=False, with_date_range=False,
                         skip_sampled=True, verification=None, verdict=None,
                         per_recording=False):
    """Return the parameterised stratified-sampling SQL.

    Kept as a pure function (no DB, no I/O) so it is unit-testable. ``n_strata``
    is interpolated into the SQL text because it drives ``ntile()``'s argument;
    every value that depends on user input stays a bound parameter.

    ``conf_column`` is the ``detections`` column holding the score of the model
    being sampled — ``models.conf_column`` (migration 0006). Since every model's
    score lives on the detection row, one code path serves all models; passing
    the reference model's ``'confidence'`` reproduces the historical result
    exactly. The value is interpolated into SQL, so it must come from the models
    table and match ``_CONF_COLUMN_RE`` — never from request data.

    Dedup is per ``(detection_id, model_id)`` — a detection already sampled for
    THIS model is skipped, but the same biological event can still be sampled
    (and separately verified) for a different model.

    The three optional filters are *shape* switches, not values — the values
    themselves stay bound params. They exist so the SQL carries no clause the
    caller did not ask for:

    ``with_conf_max``
        add an upper confidence bound (``:conf_max``), so a run can target a
        band (e.g. 0.3–0.6) rather than "everything above the floor".
    ``month_mode``
        ``None`` — no seasonal filter. ``'range'`` — months
        ``:month_start … :month_end`` within a calendar year (Feb–Apr).
        ``'wrap'`` — the range crosses New Year (Nov–Feb), which is one
        biological season but two calendar spans, so it becomes OR, not
        BETWEEN. The caller picks the mode from the values it holds.
    ``with_year_range``
        restrict to years ``:year_start … :year_end`` **inclusive**. Written as
        half-open timestamp bounds (``>= Jan 1 of year_start``,
        ``< Jan 1 of year_end + 1``) rather than ``EXTRACT(YEAR …)`` so the
        ``datetime_start`` index still applies. The month filter cannot avoid
        ``EXTRACT`` — it is inherently non-contiguous across years — so pairing
        the two keeps the scan bounded by the years.
    ``with_date_range``
        restrict to calendar days ``:date_from … :date_to`` **inclusive** — the
        contiguous period the species dashboard (``/pam/pam_detailed``) shows,
        which month + year cannot express (Nov 2025 – Oct 2026 would also pull
        Jan–Oct 2025). Same semantics as the dashboard's
        ``DATE(r.datetime_start) BETWEEN …`` in the session time zone, but
        written as half-open bounds so the index applies.

    The last four switches serve the export to a local folder, which reads
    what the archive already holds instead of feeding the verification queue:

    ``skip_sampled``
        ``False`` drops the "already cut for this model" dedup: an export
        wants exactly the detections that were cut and verified before.
    ``verification`` / ``verdict``
        ``None`` keeps the query free of verification joins. Any value of
        ``VERIFICATION_FILTERS`` / ``VERDICT_FILTERS`` joins the vote tally
        and returns it (``consensus``, ``votes``, ``positive``) for the
        manifest; ``'any'`` joins without filtering.
    ``per_recording``
        one row per recording (its best-scoring detection that passes every
        filter), stratified by that score: the unit of a whole-file export,
        so ``:per_stratum`` counts files, not detections.

    Bind params expected by the returned SQL:
        :species_name, :location_ids (list), :conf_thr, :per_stratum,
        :seg_model_id  (the model to tag the sample with / dedup against),
        plus :conf_max / :month_start, :month_end / :year_start, :year_end /
        :date_from, :date_to for whichever optional filters were switched on.
    """
    n_strata = max(1, int(n_strata))
    if not _CONF_COLUMN_RE.match(conf_column or ''):
        raise ValueError(f"Invalid conf_column: {conf_column!r}")
    if month_mode not in (None, 'range', 'wrap'):
        raise ValueError(f"Invalid month_mode: {month_mode!r}")
    if verification is not None and verification not in VERIFICATION_FILTERS:
        raise ValueError(f"Invalid verification: {verification!r}")
    if verdict is not None and verdict not in VERDICT_FILTERS:
        raise ValueError(f"Invalid verdict: {verdict!r}")
    conf_expr = f"d.{conf_column}"
    model_join = ""
    vote_columns = ""
    with_votes = verification is not None or verdict is not None

    extra = []
    if with_votes:
        # Votes are summed over every segment of the detection, whatever the
        # model it was cut for: a verified call is a fact about the animal.
        model_join = """LEFT JOIN detection_verification_map dvm
                   ON dvm.detection_id = d.detection_id
            LEFT JOIN (
                SELECT detection_id,
                       SUM(verification_count)     AS votes,
                       SUM(positive_verifications) AS positive
                FROM segments
                WHERE detection_id IS NOT NULL
                GROUP BY detection_id
            ) sv ON sv.detection_id = d.detection_id"""
        vote_columns = (",\n                   dvm.verification_result AS consensus,"
                        "\n                   COALESCE(sv.votes, 0)    AS votes,"
                        "\n                   COALESCE(sv.positive, 0) AS positive")
        for clause in (_VERIFICATION_SQL[verification or 'any'],
                       _VERDICT_SQL[verdict or 'any']):
            if clause:
                extra.append(f"AND {clause}")
    if with_conf_max:
        extra.append(f"AND {conf_expr} <= :conf_max")
    if with_year_range:
        extra.append("AND r.datetime_start >= make_date(:year_start, 1, 1)")
        extra.append("AND r.datetime_start <  make_date(:year_end, 1, 1)"
                     " + INTERVAL '1 year'")
    if with_date_range:
        extra.append("AND r.datetime_start >= CAST(:date_from AS date)")
        extra.append("AND r.datetime_start <  CAST(:date_to AS date) + 1")
    if month_mode == 'range':
        extra.append("AND EXTRACT(MONTH FROM r.datetime_start)"
                     " BETWEEN :month_start AND :month_end")
    elif month_mode == 'wrap':
        # Nov-Feb: two calendar spans, one season.
        extra.append("AND (EXTRACT(MONTH FROM r.datetime_start) >= :month_start"
                     " OR EXTRACT(MONTH FROM r.datetime_start) <= :month_end)")
    indent = "\n              "
    extra_sql = (indent + indent.join(extra)) if extra else ""
    dedup_sql = """
              AND NOT EXISTS (
                    SELECT 1 FROM segments sg
                    WHERE sg.detection_id = d.detection_id
                      AND sg.model_id = :seg_model_id
              )""" if skip_sampled else ""
    out_columns = ("detection_id, recording_id, species_id, rec_filename,\n"
                   "               datetime_start, location_id, location_name, start_s, end_s, confidence"
                   + (", consensus, votes, positive" if with_votes else ""))

    if with_votes or per_recording or not skip_sampled:
        # Export shape. The historical query below is kept verbatim so the
        # server-upload path cannot drift.
        stratify_from = "filtered"
        best_cte = ""
        if per_recording:
            best_cte = """,
        best AS (
            SELECT DISTINCT ON (recording_id) *
            FROM filtered
            ORDER BY recording_id, confidence DESC, detection_id
        )"""
            stratify_from = "best"
        return text(f"""
        WITH filtered AS (
            SELECT d.detection_id,
                   d.recording_id,
                   d.species_id,
                   r.filename       AS rec_filename,
                   r.datetime_start,
                   r.location_id,
                   l.location_name,
                   d.start_s,
                   d.end_s,
                   {conf_expr}      AS confidence{vote_columns}
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations  l ON r.location_id  = l.location_id
            JOIN species    s ON d.species_id   = s.species_id
            {model_join}
            WHERE s.scientific_name = :species_name
              AND r.location_id = ANY(:location_ids)
              AND {conf_expr} IS NOT NULL
              AND {conf_expr} >= :conf_thr{extra_sql}{dedup_sql}
        ){best_cte},
        candidates AS (
            SELECT {stratify_from}.*,
                   ntile({n_strata}) OVER (ORDER BY confidence) AS stratum
            FROM {stratify_from}
        ),
        ranked AS (
            SELECT candidates.*,
                   row_number() OVER (PARTITION BY stratum ORDER BY random()) AS rn
            FROM candidates
        )
        SELECT {out_columns}
        FROM ranked
        WHERE rn <= :per_stratum
        ORDER BY confidence
    """)

    return text(f"""
        WITH candidates AS (
            SELECT d.detection_id,
                   d.recording_id,
                   d.species_id,
                   r.filename       AS rec_filename,
                   r.datetime_start,
                   r.location_id,
                   l.location_name,
                   d.start_s,
                   d.end_s,
                   {conf_expr}      AS confidence,
                   ntile({n_strata}) OVER (ORDER BY {conf_expr}) AS stratum
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations  l ON r.location_id  = l.location_id
            JOIN species    s ON d.species_id   = s.species_id
            {model_join}
            WHERE s.scientific_name = :species_name
              AND r.location_id = ANY(:location_ids)
              AND {conf_expr} IS NOT NULL
              AND {conf_expr} >= :conf_thr{extra_sql}
              AND NOT EXISTS (
                    SELECT 1 FROM segments sg
                    WHERE sg.detection_id = d.detection_id
                      AND sg.model_id = :seg_model_id
              )
        ),
        ranked AS (
            SELECT candidates.*,
                   row_number() OVER (PARTITION BY stratum ORDER BY random()) AS rn
            FROM candidates
        )
        SELECT detection_id, recording_id, species_id, rec_filename,
               datetime_start, location_id, location_name, start_s, end_s, confidence
        FROM ranked
        WHERE rn <= :per_stratum
        ORDER BY confidence
    """)


def run_stratified_sample(species_name, location_ids, confidence_threshold=0.1,
                          n_strata=10, sample_size=700, conn=None,
                          model_id=None, conf_column='confidence',
                          confidence_max=None, month_start=None, month_end=None,
                          year_start=None, year_end=None,
                          date_from=None, date_to=None,
                          skip_sampled=True, verification=None, verdict=None,
                          per_recording=False):
    """Draw a confidence-stratified detection sample for one species + model.

    Mirrors ``produce_random_segments.R``: split the confidence range into
    ``n_strata`` quantile bins (``ntile``) and take up to
    ``ceil(sample_size / n_strata)`` random detections per bin — so low- and
    high-confidence events are both represented. Already-uploaded detections
    (for this ``model_id``) are excluded by the query itself.

    ``model_id`` is the model the sample is drawn / tagged for, and
    ``conf_column`` is that model's score column on ``detections``
    (``models.conf_column``); it defaults to the reference model's historical
    ``'confidence'``. The chosen ``model_id`` is echoed into every result dict so
    the client sends it back on upload and the segment is tagged with it.

    Optional narrowing, each ``None`` by default so an omitted filter changes
    nothing about the query:

    ``confidence_max``
        upper bound on the model's score, for sampling a band (0.3–0.6) rather
        than everything above the floor.
    ``month_start`` / ``month_end``
        seasonal window, both required together, 1–12. **Year-agnostic**: a
        Feb–Apr window pulls every February–April in range, across all years,
        which is what a phenological sample needs and what a plain date range
        cannot express. ``month_start > month_end`` means the window crosses
        New Year (Nov–Feb) and is read as one season.
    ``year_start`` / ``year_end``
        inclusive year bounds, both required together. Cheap to apply (index on
        ``datetime_start``), so it also keeps the month filter's ``EXTRACT``
        from scanning the whole table.
    ``date_from`` / ``date_to``
        inclusive calendar days (``date`` objects), both required together:
        the exact period of the species dashboard, for sampling "what I am
        looking at". Composes with the other filters like they do with each
        other; the dashboard link sends it instead of months and years.
    ``skip_sampled`` / ``verification`` / ``verdict`` / ``per_recording``
        the local-export switches, see ``build_sampling_query``. Their
        defaults reproduce the server-upload sample exactly.

    Returns a list of plain dicts (JSON-serialisable) — one per sampled
    detection — carrying everything the browser needs to cut + label the clip.
    """
    if not location_ids:
        return []
    n_strata = max(1, int(n_strata))
    sample_size = max(1, min(int(sample_size), MAX_SAMPLE_PER_SPECIES))
    per_stratum = math.ceil(sample_size / n_strata)

    params = {
        'species_name': species_name,
        'location_ids': list(location_ids),
        'conf_thr': float(confidence_threshold),
        'per_stratum': per_stratum,
        'seg_model_id': model_id,
    }

    with_conf_max = confidence_max is not None
    if with_conf_max:
        params['conf_max'] = float(confidence_max)

    # Both bounds or neither: a half-given window would silently mean something
    # other than what the operator asked for.
    with_year_range = year_start is not None and year_end is not None
    if with_year_range:
        params['year_start'] = int(year_start)
        params['year_end'] = int(year_end)

    with_date_range = date_from is not None and date_to is not None
    if with_date_range:
        params['date_from'] = date_from
        params['date_to'] = date_to

    month_mode = None
    if month_start is not None and month_end is not None:
        month_start, month_end = int(month_start), int(month_end)
        # A full Jan–Dec window is every month, so skip the EXTRACT entirely.
        if not (month_start == 1 and month_end == 12):
            month_mode = 'range' if month_start <= month_end else 'wrap'
            params['month_start'] = month_start
            params['month_end'] = month_end

    own_conn = conn is None
    if own_conn:
        conn = get_pam_db_connection()
    try:
        rows = conn.execute(
            build_sampling_query(
                n_strata, conf_column=conf_column,
                with_conf_max=with_conf_max, month_mode=month_mode,
                with_year_range=with_year_range,
                with_date_range=with_date_range,
                skip_sampled=skip_sampled, verification=verification,
                verdict=verdict, per_recording=per_recording,
            ), params
        ).mappings().fetchall()
    finally:
        if own_conn and conn is not None:
            conn.close()

    return format_sample_plan(rows, model_id)


def format_sample_plan(rows, model_id):
    """Format detection rows for the browser cutter, including mixed-species plans."""
    # Assign a per-recording part counter, exactly like the R script, so two
    # detections from the same recording get _part1, _part2, ... .
    part_counter = defaultdict(int)
    result = []
    for row in rows:
        rec_filename = row['rec_filename']
        stem = _recording_stem(rec_filename)
        part_counter[stem] += 1
        part = part_counter[stem]
        start_s = row['start_s']
        conf = row['confidence']
        seg_filename = build_segment_filename(
            conf, rec_filename, start_s, part,
            location_name=row['location_name'],
            datetime_start=row['datetime_start'],
        )
        dt = row['datetime_start']
        result.append({
            'detection_id': int(row['detection_id']),
            'recording_id': int(row['recording_id']),
            'species_id': int(row['species_id']),
            'model_id': int(model_id) if model_id is not None else None,
            'recording_filename': ntpath.basename(rec_filename or ''),
            'location_id': row.get('location_id'),
            'segment_filename': seg_filename,
            'location_name': _location_token(rec_filename, row['location_name']),
            'start_s': float(start_s) if start_s is not None else None,
            'end_s': float(row['end_s']) if row['end_s'] is not None else None,
            'confidence': float(conf) if conf is not None else None,
            'recorded_date': dt.date().isoformat() if dt else None,
            'recorded_time': dt.time().isoformat() if dt else None,
        })
        if row.get('species_name'):
            result[-1]['species_name'] = row['species_name']
        if 'novelty' in row:
            result[-1]['novelty'] = row['novelty']
        if 'votes' in row:
            # Local export only: the manifest records what the subset was.
            consensus = row['consensus']
            result[-1].update({
                'consensus': int(consensus) if consensus is not None else None,
                'votes': int(row['votes'] or 0),
                'positive_votes': int(row['positive'] or 0),
                'verification_state': verification_state(
                    consensus, row['votes'], row['positive']),
            })
    return result


def verification_state(consensus, votes, positive):
    """One word for the manifest, matching the export filters' definitions."""
    if consensus == 1:
        return 'confirmed'
    if consensus == 0:
        return 'rejected'
    votes, positive = int(votes or 0), int(positive or 0)
    if not votes:
        return 'unverified'
    if 2 * positive > votes:
        return 'partial_confirmed'
    if 2 * positive < votes:
        return 'partial_rejected'
    return 'partial_tied'




# ──────────────────────────────────────────────────────────────────────────────
# Targeted planning: the detections of ONE time window, not a sample
#
# The stratified sampler answers "give me a representative set for measuring
# precision". The co-occurrence map asks something else: "cut exactly the
# detections behind this point in this window, so the co-occurrence can be
# confirmed". Stratifying two detections over ten quantile bins is meaningless,
# and month+year is the finest window the sampler can express, so this is a
# separate query rather than another switch on that one.
# ──────────────────────────────────────────────────────────────────────────────

#: Detection time is reconstructed, not stored, so a recording that began
#: before the window can still carry detections inside it. Mirrors
#: ``cooccurrence.RECORDING_SLACK`` — the two must agree or the page would
#: offer to cut detections the map never counted.
WINDOW_RECORDING_SLACK = "1 day"


def build_window_query(conf_column='confidence', recording_scope=None):
    """Parameterised SQL for every detection inside one time window.

    Pure function (no DB, no I/O) so it is unit-testable, like
    ``build_sampling_query``. ``conf_column`` is interpolated, so it must come
    from ``models.conf_column`` and match ``_CONF_COLUMN_RE`` — never from
    request data.

    The window is applied to the RECONSTRUCTED detection time
    (``recordings.datetime_start + detections.start_s``), exactly as the
    co-occurrence page bins it. Filtering ``datetime_start`` alone would answer
    a different question: which recordings started in the window, not which
    detections fell inside it.

    Dedup is the sampler's: a detection already cut for THIS model is skipped,
    so a second visit to the same point offers only what is missing.

    Bind params: :species_name, :location_ids (list), :conf_thr, :from_ts,
    :to_ts, :slack, :seg_model_id.
    """
    if not _CONF_COLUMN_RE.match(conf_column or ''):
        raise ValueError(f"Invalid conf_column: {conf_column!r}")
    conf_expr = f"d.{conf_column}"
    det_ts = "r.datetime_start + (d.start_s * interval '1 second')"
    if recording_scope is not None:
        conditions, _ = recording_scope.where('r')
        window_sql = " AND ".join(conditions)
        score_filter = f"ROUND(({conf_expr})::numeric, 4) >= :conf_thr"
    else:
        score_filter = f"{conf_expr} >= :conf_thr"
        window_sql = f"""
            r.datetime_start >= CAST(:from_ts AS timestamptz) - CAST(:slack AS interval)
            AND r.datetime_start < CAST(:to_ts AS timestamptz)
            AND {det_ts} >= CAST(:from_ts AS timestamptz)
            AND {det_ts} <  CAST(:to_ts AS timestamptz)
        """
    return text(f"""
        SELECT d.detection_id,
               d.recording_id,
               d.species_id,
               r.filename       AS rec_filename,
               r.datetime_start,
               r.location_id,
               l.location_name,
               d.start_s,
               d.end_s,
               {conf_expr}      AS confidence
        FROM detections d
        JOIN recordings r ON d.recording_id = r.recording_id
        JOIN locations  l ON r.location_id  = l.location_id
        JOIN species    s ON d.species_id   = s.species_id
        WHERE s.scientific_name = :species_name
          AND r.location_id = ANY(:location_ids)
          AND {conf_expr} IS NOT NULL
          AND {score_filter}
          AND ({window_sql})
          AND NOT EXISTS (
                SELECT 1 FROM segments sg
                WHERE sg.detection_id = d.detection_id
                  AND sg.model_id = :seg_model_id
          )
        ORDER BY {det_ts}, d.detection_id
        LIMIT :max_rows
    """)


#: A single window cannot legitimately hold hundreds of detections worth
#: cutting; a number this high means the caller passed a window far wider than
#: the page is for, and the browser would be asked to decode that many files.
MAX_WINDOW_SEGMENTS = 200
# A list of windows (one link for a whole set, e.g. a study's truth set): one plan may hold
# more clips; a second visit cuts what is left, because cut detections are skipped.
MAX_LIST_SEGMENTS = 600


def plan_window_segments(species_name, location_ids, from_ts, to_ts,
                         confidence_threshold=0.0, conn=None, model_id=None,
                         conf_column='confidence',
                         max_rows=MAX_WINDOW_SEGMENTS, recording_scope=None):
    """Every not-yet-cut detection of one species inside one window.

    Returns the same plain-dict plan as ``run_stratified_sample``, so the
    browser-side cutting and upload path is shared verbatim: the two pages
    differ in how the plan is chosen, not in what a plan is.
    """
    if not location_ids or (recording_scope is None and (from_ts is None or to_ts is None)):
        return []
    window_params = (recording_scope.where('r')[1] if recording_scope is not None else {
        'from_ts': from_ts, 'to_ts': to_ts, 'slack': WINDOW_RECORDING_SLACK,
    })
    own_conn = conn is None
    if own_conn:
        conn = get_pam_db_connection()
    try:
        rows = conn.execute(
            build_window_query(conf_column=conf_column, recording_scope=recording_scope),
            {
                'species_name': species_name,
                'location_ids': list(location_ids),
                'conf_thr': float(confidence_threshold),
                **window_params,
                'seg_model_id': model_id,
                'max_rows': int(max_rows),
            },
        ).mappings().fetchall()
    finally:
        if own_conn and conn is not None:
            conn.close()

    # Same per-recording part counter as the sampler, so a second detection
    # from one recording becomes _part2 and the filenames stay comparable.
    part_counter = defaultdict(int)
    result = []
    for row in rows:
        rec_filename = row['rec_filename']
        stem = _recording_stem(rec_filename)
        part_counter[stem] += 1
        start_s = row['start_s']
        conf = row['confidence']
        dt = row['datetime_start']
        result.append({
            'detection_id': int(row['detection_id']),
            'recording_id': int(row['recording_id']),
            'species_id': int(row['species_id']),
            'model_id': int(model_id) if model_id is not None else None,
            'recording_filename': ntpath.basename(rec_filename or ''),
            'location_id': row.get('location_id'),
            'segment_filename': build_segment_filename(
                conf, rec_filename, start_s, part_counter[stem],
                location_name=row['location_name'],
                datetime_start=dt,
            ),
            'location_name': _location_token(rec_filename, row['location_name']),
            'start_s': float(start_s) if start_s is not None else None,
            'end_s': float(row['end_s']) if row['end_s'] is not None else None,
            'confidence': float(conf) if conf is not None else None,
            'recorded_date': dt.date().isoformat() if dt else None,
            'recorded_time': dt.time().isoformat() if dt else None,
        })
    return result

def get_earliest_recording_year(conn=None, refresh=False):
    """Earliest year present in ``recordings.datetime_start`` (cached).

    The sample-upload page uses this global floor for its year selector.

    The value is cached in-process for ``EARLIEST_YEAR_TTL_SECONDS`` — a MIN()
    over every recording is not worth paying on each page render, and the answer
    changes only when someone imports older material. Returns the current year on
    an empty table or a DB error, which degrades to "this year only" rather than
    to a broken page.
    """
    global _earliest_year_cache
    now = time.monotonic()
    if not refresh and _earliest_year_cache is not None:
        year, cached_at = _earliest_year_cache
        if now - cached_at < EARLIEST_YEAR_TTL_SECONDS:
            return year

    this_year = datetime.now().year
    own = conn is None
    try:
        if own:
            conn = get_pam_db_connection()
        row = conn.execute(text(
            "SELECT MIN(datetime_start) AS earliest FROM recordings"
        )).fetchone()
        earliest = row.earliest if row else None
        year = earliest.year if earliest else this_year
        year = max(EARLIEST_YEAR_FLOOR, min(int(year), this_year))
        _earliest_year_cache = (year, now)
        return year
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_earliest_recording_year): {e}")
        if _earliest_year_cache is not None:
            return _earliest_year_cache[0]
        return this_year
    finally:
        if own and conn is not None:
            conn.close()


def reset_earliest_year_cache():
    """Drop the cached earliest year — for tests and after a historical import."""
    global _earliest_year_cache
    _earliest_year_cache = None


# ── filename helpers ────────────────────────────────────────────────────────

def _recording_stem(rec_filename):
    """Recording filename without its audio extension."""
    base = ntpath.basename(rec_filename or '')
    stem, _ext = os.path.splitext(base)
    return stem


def _location_token(rec_filename, location_name=None):
    """The LOCATION token used in the segment filename / stored in location_name.

    Prefer the token embedded in the canonical recording stem
    (``LOCATION_YYYYMMDD_HHMMSS``). Fall back to a sanitised location_name only
    when the recording name is non-canonical, so we always have *something*.
    """
    m = _RECORDING_STEM_RE.match(_recording_stem(rec_filename))
    if m:
        return m.group(1)
    if location_name:
        token = re.sub(r'[^A-Za-z0-9\-]+', '-', location_name).strip('-')
        return token or 'LOC'
    return 'LOC'


def build_segment_filename(confidence, rec_filename, start_s, part,
                           ext='flac', location_name=None, datetime_start=None):
    """Build the canonical segment filename.

    When the recording stem is canonical we reuse it verbatim
    (``<conf>_<stem>_sec<start>_part<N>.<ext>``) so the legacy filename linker
    re-derives the exact same recording. Otherwise we synthesise a canonical
    stem from ``location_name`` + ``datetime_start`` so the name still parses.
    """
    conf_str = f"{float(confidence):.3f}" if confidence is not None else "0.000"
    start_int = int(round(float(start_s))) if start_s is not None else 0
    stem = _recording_stem(rec_filename)

    if not _RECORDING_STEM_RE.match(stem):
        token = _location_token(rec_filename, location_name)
        if datetime_start:
            stem = f"{token}_{datetime_start:%Y%m%d}_{datetime_start:%H%M%S}"
        else:
            stem = f"{token}_00000000_000000"

    ext = ext.lower().lstrip('.')
    return f"{conf_str}_{stem}_sec{start_int}_part{int(part)}.{ext}"


# ── FLAC encoding ─────────────────────────────────────────────────────────────

def convert_wav_bytes_to_flac(wav_bytes, flac_path):
    """Encode in-memory WAV bytes to a FLAC file at ``flac_path``.

    Uses libsndfile via ``soundfile`` (cross-platform, no external ffmpeg
    dependency, and importable in tests). Raises on failure — the caller is
    responsible for turning that into a per-file error without aborting the
    whole batch.
    """
    import soundfile as sf  # local import: heavy, and keeps module import cheap
    data, samplerate = sf.read(io.BytesIO(wav_bytes), always_2d=False)
    os.makedirs(os.path.dirname(flac_path), exist_ok=True)
    sf.write(flac_path, data, samplerate, format='FLAC')
    return flac_path


# ── registration ──────────────────────────────────────────────────────────────

def register_sampled_segment(conn, *, species_id, detection_id, recording_id,
                             segment_filename, confidence, location_name,
                             recorded_date, recorded_time, file_path,
                             model_id=None):
    """Insert one sampled segment, with explicit detection/recording/model links.

    Idempotent per ``(detection_id, model_id)``: if a segment for this detection
    AND this model already exists (e.g. a double-click, or a prior sample for
    the same model) the insert is skipped and ``None`` is returned. The same
    biological event may still be sampled for a DIFFERENT model. Otherwise the
    new segment id is returned. Runs inside the caller's transaction.

    ``status`` is ``'pending'`` so the segment flows through the standard
    verification interface exactly like a ZIP-uploaded one.
    """
    dup = conn.execute(text("""
        SELECT id FROM segments
        WHERE (detection_id = :detection_id OR filename = :filename)
          AND model_id IS NOT DISTINCT FROM :model_id
        LIMIT 1
    """), {'detection_id': detection_id, 'filename': segment_filename,
           'model_id': model_id}).fetchone()
    if dup:
        current_app.logger.info(
            f"Sampled segment skipped (duplicate): detection={detection_id} "
            f"model={model_id} filename={segment_filename}")
        return None

    row = conn.execute(text("""
        INSERT INTO segments
            (species_id, filename, confidence_level, location_name,
             recorded_date, recorded_time, file_path, upload_date, status,
             recording_id, detection_id, model_id)
        VALUES
            (:species_id, :filename, :confidence, :location_name,
             :recorded_date, :recorded_time, :file_path, :upload_date, 'pending',
             :recording_id, :detection_id, :model_id)
        RETURNING id
    """), {
        'species_id': species_id,
        'filename': segment_filename,
        'confidence': confidence,
        'location_name': location_name,
        'recorded_date': recorded_date,
        'recorded_time': recorded_time,
        'file_path': file_path,
        'upload_date': datetime.now(),
        'recording_id': recording_id,
        'detection_id': detection_id,
        'model_id': model_id,
    }).fetchone()
    return row[0] if row else None


def save_and_register_segment(wav_bytes, meta, upload_directory, conn, *, isolate_file=False):
    """Encode a WAV clip to FLAC on disk and register it. Returns (status, id).

    status ∈ {'saved', 'duplicate', 'error'}. On 'duplicate' the just-written
    FLAC is removed so we don't leave orphan files. Any exception is turned into
    ('error', None) after cleanup — the route reports it per-file.
    """
    species_dir = _safe_species_dirname(meta['species_name'])
    flac_name = meta['segment_filename']
    if not flac_name.lower().endswith('.flac'):
        flac_name = os.path.splitext(flac_name)[0] + '.flac'
    directory = os.path.join(upload_directory, species_dir)
    isolated_directory = None
    final_path = None
    wrote_file = False

    try:
        if isolate_file:
            # Two uploads can carry the same canonical name. Give this attempt
            # its own path so neither encoding nor cleanup can touch another clip.
            from tempfile import mkdtemp

            os.makedirs(directory, exist_ok=True)
            isolated_directory = mkdtemp(dir=directory)
            directory = isolated_directory
        final_path = os.path.join(directory, flac_name)
        convert_wav_bytes_to_flac(wav_bytes, final_path)
        wrote_file = True
        with conn.begin():
            seg_id = register_sampled_segment(
                conn,
                species_id=meta['species_id'],
                detection_id=meta['detection_id'],
                recording_id=meta['recording_id'],
                segment_filename=flac_name,
                confidence=meta['confidence'],
                location_name=meta['location_name'],
                recorded_date=meta.get('recorded_date'),
                recorded_time=meta.get('recorded_time'),
                file_path=final_path,
                model_id=meta.get('model_id'),
            )
        if seg_id is None:
            _quiet_remove(final_path)
            return 'duplicate', None
        isolated_directory = None
        return 'saved', seg_id
    except Exception as e:
        current_app.logger.error(
            f"Error saving sampled segment {flac_name}: {e}")
        if isolated_directory is not None or wrote_file:
            _quiet_remove(final_path)
        return 'error', None
    finally:
        if isolated_directory is not None:
            try:
                os.rmdir(isolated_directory)
            except OSError:
                pass


def _safe_species_dirname(species_name):
    """Filesystem-safe species subfolder name (mirrors the ZIP path layout)."""
    name = (species_name or 'unknown').strip()
    return re.sub(r'[^\w\-. ]+', '_', name) or 'unknown'


def _quiet_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ── backfill ────────────────────────────────────────────────────────────────

# New-format segment name carries the detection start second.
_SEG_NEW_RE = re.compile(
    r'^(\d+\.\d+)_([A-Za-z0-9\-]+)_(\d{8})_(\d{6})_sec(\d+)_part(\d+)\.(wav|flac)$',
    re.IGNORECASE)
# Legacy-format name: confidence + recording stem only, no start second.
_SEG_LEGACY_RE = re.compile(
    r'^(\d+\.\d+)_([A-Za-z0-9\-]+)_(\d{8})_(\d{6})\.(wav|flac)$',
    re.IGNORECASE)


def parse_segment_filename_for_backfill(filename):
    """Extract linking keys from a segment filename.

    Returns a dict with ``recording_stem`` and either ``start_s`` (new format)
    or ``confidence`` (legacy format, used for the heuristic tier-2 match), or
    ``None`` if the name matches neither shape.
    """
    m = _SEG_NEW_RE.match(filename)
    if m:
        conf, loc, d, t, start_s, _part, _ext = m.groups()
        return {
            'recording_stem': f"{loc}_{d}_{t}",
            'start_s': int(start_s),
            'confidence': float(conf),
        }
    m = _SEG_LEGACY_RE.match(filename)
    if m:
        conf, loc, d, t, _ext = m.groups()
        return {
            'recording_stem': f"{loc}_{d}_{t}",
            'start_s': None,
            'confidence': float(conf),
        }
    return None


def backfill_segment_links(report_only=False, batch_size=500):
    """Populate segments.recording_id / detection_id for legacy rows.

    Two-tier matching per segment (only rows with detection_id IS NULL):
      * tier 1 (exact)  — new-format name → match (recording_id, species_id,
                          start_s) to a single detection.
      * tier 2 (heuristic) — legacy name (no start second) → match
                          (recording_id, species_id, round(confidence, 3)); the
                          reference confidence is near-unique within a
                          recording+species, so this usually resolves uniquely.

    A tier-2 candidate that matches more than one detection is recorded as a
    *collision* and left untouched (we can't know which detection it was).

    With ``report_only=True`` nothing is written — it returns the same stats so
    you can inspect collisions before deciding on a UNIQUE(detection_id) index.
    """
    stats = {
        'scanned': 0, 'unparseable': 0,
        'recording_unmatched': 0,
        'tier1_linked': 0, 'tier2_linked': 0,
        'tier2_collisions': 0, 'no_detection': 0,
        'collision_samples': [],
    }

    conn = get_pam_db_connection()
    try:
        recordings = conn.execute(
            text("SELECT recording_id, filename FROM recordings")).fetchall()
        rec_map = {r.filename.rsplit('.', 1)[0]: r.recording_id for r in recordings}

        segments = conn.execute(text("""
            SELECT id, filename, species_id, confidence_level
            FROM segments
            WHERE detection_id IS NULL AND status IN ('completed', 'archived', 'pending')
            ORDER BY id
        """)).fetchall()
    finally:
        conn.close()

    updates = []  # (segment_id, recording_id, detection_id)
    for seg in segments:
        stats['scanned'] += 1
        parsed = parse_segment_filename_for_backfill(seg.filename)
        if not parsed:
            stats['unparseable'] += 1
            continue
        recording_id = rec_map.get(parsed['recording_stem'])
        if recording_id is None:
            stats['recording_unmatched'] += 1
            continue

        det_conn = get_pam_db_connection()
        try:
            if parsed['start_s'] is not None:
                # tier 1 — exact
                det = det_conn.execute(text("""
                    SELECT detection_id FROM detections
                    WHERE recording_id = :rid AND species_id = :sid AND start_s = :st
                    LIMIT 1
                """), {'rid': recording_id, 'sid': seg.species_id,
                       'st': parsed['start_s']}).fetchone()
                if det:
                    updates.append((seg.id, recording_id, det.detection_id))
                    stats['tier1_linked'] += 1
                else:
                    stats['no_detection'] += 1
            else:
                # tier 2 — heuristic on rounded confidence
                cands = det_conn.execute(text("""
                    SELECT detection_id FROM detections
                    WHERE recording_id = :rid AND species_id = :sid
                      AND round(confidence::numeric, 3) = :conf
                """), {'rid': recording_id, 'sid': seg.species_id,
                       'conf': round(parsed['confidence'], 3)}).fetchall()
                if len(cands) == 1:
                    updates.append((seg.id, recording_id, cands[0].detection_id))
                    stats['tier2_linked'] += 1
                elif len(cands) > 1:
                    stats['tier2_collisions'] += 1
                    if len(stats['collision_samples']) < 25:
                        stats['collision_samples'].append(
                            {'segment_id': seg.id, 'filename': seg.filename,
                             'candidates': len(cands)})
                else:
                    stats['no_detection'] += 1
        finally:
            det_conn.close()

    if not report_only and updates:
        for i in range(0, len(updates), batch_size):
            chunk = updates[i:i + batch_size]
            wconn = get_pam_db_connection()
            try:
                with wconn.begin():
                    wconn.execute(text("""
                        UPDATE segments SET recording_id = :rid, detection_id = :did
                        WHERE id = :sid
                    """), [{'sid': s, 'rid': r, 'did': d} for (s, r, d) in chunk])
            finally:
                wconn.close()

    stats['total_linked'] = stats['tier1_linked'] + stats['tier2_linked']
    stats['report_only'] = report_only
    current_app.logger.info(f"Segment link backfill: {stats}")
    return stats
