"""Plan new clips for verification gaps across the accessible PAM archive."""

from collections import Counter
from heapq import heapify, heappop, heappush
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .pam_segment_sampling import _CONF_COLUMN_RE, format_sample_plan
from .verification_priorities import POSITIVE_CONSENSUS, PRIORITY_CONFIDENCE, RECORDED_MONTH

SEARCH_BUDGET_SECONDS = 24


def _scopes(row):
    species = row['species_id']
    yield (species,)
    for institution in row['institution_ids'] or ():
        yield (species, institution)
        if row['recorded_month'] is not None:
            yield (species, institution, row['recorded_month'])


def prioritize_candidates(candidates, history, size, *, balance_species, balance_institutions):
    filled = {scope for row in history for scope in _scopes(row)}
    used_recordings = set()
    by_species, by_institution = Counter(), Counter()
    plan = []

    def rank(row):
        species = row['species_id']
        month = row['recorded_month']
        institutions = row['institution_ids'] or (None,)
        territory_rank, count, territory = min(
            (
                -2 if institution is not None and (species, institution) not in filled
                else -1 if institution is not None and month is not None
                and (species, institution, month) not in filled else 0,
                by_institution[institution] if balance_institutions else 0,
                institution or 0,
            )
            for institution in institutions
        )
        novelty = 3 if (species,) not in filled else -territory_rank
        plausible = row['confidence'] >= PRIORITY_CONFIDENCE
        return (
            (0 if plausible else 1) if novelty else (2 if plausible else 3),
            -novelty, not row['is_redbook'],
            by_species[species] if balance_species else 0, count,
            -row['confidence'], row['detection_id'], territory,
        )

    # Gains can only drop as gaps are filled, so update heap entries lazily.
    heap = [(rank(row), index) for index, row in enumerate(candidates)]
    heapify(heap)
    while heap and len(plan) < size:
        previous, index = heappop(heap)
        row = candidates[index]
        recording = (row['species_id'], row['recording_id'])
        if recording in used_recordings:
            continue
        current = rank(row)
        if previous != current:
            heappush(heap, (current, index))
            continue
        plan.append({**row, 'novelty': -current[1]})
        filled.update(_scopes(row))
        used_recordings.add(recording)
        by_species[row['species_id']] += 1
        for institution in row['institution_ids'] or (None,):
            by_institution[institution] += 1
    return plan


def _candidate_query(conf_column, with_upper_bound):
    if not _CONF_COLUMN_RE.fullmatch(conf_column):
        raise ValueError('Invalid confidence column')
    confidence = f'd.{conf_column}'
    upper_bound = f'AND {confidence} < :upper' if with_upper_bound else ''
    return text(f"""
        WITH eligible_locations AS (
            SELECT l.location_id, l.location_name,
                   array_agg(li.institution_id ORDER BY li.institution_id)
                       FILTER (WHERE li.institution_id IS NOT NULL) AS institution_ids
            FROM locations l
            LEFT JOIN location_institutions li ON li.location_id = l.location_id
                AND (:is_admin OR li.institution_id = ANY(:managed_ids))
                AND (:all_institutions OR li.institution_id = :institution_id)
            WHERE (:all_locations OR l.location_id = ANY(:location_ids))
            GROUP BY l.location_id, l.location_name
            HAVING (:is_admin AND :all_institutions) OR COUNT(li.institution_id) > 0
        ), eligible_recordings AS (
            SELECT r.recording_id, r.filename, r.location_id, r.datetime_start,
                   l.location_name, l.institution_ids
            FROM eligible_locations l JOIN recordings r USING (location_id)
        ), eligible_species AS (
            SELECT s.species_id, s.scientific_name, s.redbook2021 IS NOT NULL AS is_redbook
            FROM species s
            WHERE (:species_name = '' OR s.scientific_name = :species_name)
              AND (:class_name = '' OR s.class = :class_name)
              AND (:order_name = '' OR s.order_rank = :order_name)
              AND (:family_name = '' OR s.family = :family_name)
              AND (:genus_name = '' OR s.genus = :genus_name)
              AND (:is_admin OR s.required_role IS NULL OR s.required_role = ANY(:roles))
        ), registered AS (
            SELECT DISTINCT sg.species_id, sg.recording_id
            FROM segments sg
            WHERE sg.recording_id IS NOT NULL
        ), best_per_recording AS (
            SELECT DISTINCT ON (d.species_id, d.recording_id)
                   d.detection_id, d.recording_id, d.species_id,
                   s.scientific_name AS species_name, s.is_redbook,
                   r.filename AS rec_filename, r.location_id, r.location_name,
                   r.datetime_start, r.institution_ids, d.start_s, d.end_s,
                   CAST(EXTRACT(MONTH FROM (
                       r.datetime_start + COALESCE(d.start_s, 0) * INTERVAL '1 second'
                   ) AT TIME ZONE 'Europe/Kyiv') AS integer) AS recorded_month,
                   {confidence} AS confidence
            FROM detections d
            JOIN eligible_recordings r USING (recording_id)
            JOIN eligible_species s USING (species_id)
            LEFT JOIN registered already ON already.species_id = d.species_id
                AND already.recording_id = d.recording_id
            WHERE {confidence} >= :floor {upper_bound}
              AND already.recording_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM segments sg WHERE sg.detection_id = d.detection_id
              )
            ORDER BY d.species_id, d.recording_id, {confidence} DESC, d.detection_id
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY species_id, institution_ids, recorded_month
                ORDER BY confidence DESC, detection_id
            ) AS position
            FROM best_per_recording
        )
        SELECT * FROM ranked WHERE position <= :sample_size
    """)


def plan_gap_sample(conn, *, access_sql, params, sample_size, model_id, conf_column):
    deadline = monotonic() + SEARCH_BUDGET_SECONDS
    conn.execute(text("SET LOCAL statement_timeout = '24s'"))
    history = conn.execute(text(f"""
        WITH records AS (
            SELECT seg.species_id, {RECORDED_MONTH} AS recorded_month, r.location_id
            FROM segments seg
            JOIN species s ON s.species_id = seg.species_id
            LEFT JOIN recordings r ON r.recording_id = seg.recording_id
            LEFT JOIN detections d ON d.detection_id = seg.detection_id
            WHERE ({access_sql}) AND (:is_admin OR s.required_role IS NULL OR s.required_role = ANY(:roles))
              AND (:species_name = '' OR s.scientific_name = :species_name)
              AND ((seg.status = 'completed' AND seg.verification_count >= 2 AND {POSITIVE_CONSENSUS})
                   OR (seg.status = 'pending' AND seg.confidence_level >= :plausible))
        )
        SELECT h.species_id, h.recorded_month,
               array_agg(DISTINCT li.institution_id)
                   FILTER (WHERE li.institution_id IS NOT NULL) AS institution_ids
        FROM records h LEFT JOIN location_institutions li ON li.location_id = h.location_id
        GROUP BY h.species_id, h.recorded_month
    """), {**params, 'plausible': PRIORITY_CONFIDENCE}).mappings().all()
    candidates = []
    plan = []
    limited = False
    upper = None
    for floor in (PRIORITY_CONFIDENCE, .65, .45, .20, 0.0):
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            limited = True
            break
        try:
            # Keep candidates already found if a broader confidence band times out.
            with conn.begin_nested():
                conn.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                             {'timeout': f'{remaining_ms}ms'})
                candidates.extend(conn.execute(
                    _candidate_query(conf_column, upper is not None),
                    {**params, 'floor': floor, 'upper': upper, 'sample_size': sample_size},
                ).mappings().all())
        except DBAPIError as error:
            if getattr(error.orig, 'pgcode', None) != '57014':
                raise
            limited = True
            break
        plan = prioritize_candidates(
            candidates, history, sample_size,
            balance_species=not params['species_name'], balance_institutions=params['all_institutions'],
        )
        if len(plan) == sample_size and all(row['novelty'] for row in plan):
            break
        upper = floor
    return format_sample_plan(plan, model_id), limited
