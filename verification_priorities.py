"""Rank PAM verification candidates using confirmed presence and recording context."""

from sqlalchemy import text


PRIORITY_CONFIDENCE = 0.85
MAX_QUEUE_SKIPS = 100
PRIORITY_ORDER = (
    'plausible DESC, similar_rejected, novelty DESC, is_redbook DESC, '
    'votes_needed, confidence_level DESC NULLS LAST, segment_id'
)
PENDING_FOR_USER = (
    "seg.status = 'pending'",
    "NOT EXISTS (SELECT 1 FROM segment_verifications sv "
    "WHERE sv.segment_id = seg.id AND sv.user_id = :user_id)",
)


def ranking_ctes(access_sql, conditions=PENDING_FOR_USER, *, filter_territories=False):
    """Build candidate rankings without narrowing historical evidence to the queue filters."""
    # Institution membership is the territory grouping used by the PAM UI.
    # A point may belong to several territories. Pick the strongest gain.
    territory_filter = 'AND li.institution_id = ANY(:institution_ids)' if filter_territories else ''
    visibility = f"({access_sql}) AND (:is_admin OR s.required_role IS NULL OR s.required_role = ANY(:roles))"
    recorded_month = """
        CAST(EXTRACT(MONTH FROM COALESCE(
            (r.datetime_start + COALESCE(d.start_s, 0) * INTERVAL '1 second')
                AT TIME ZONE 'Europe/Kyiv', CAST(seg.recorded_date AS timestamp)
        )) AS integer)
    """
    return f"""
        WITH eligible_segments AS (
            SELECT seg.id, seg.species_id, seg.recording_id,
                   seg.verification_count, seg.positive_verifications,
                   seg.confidence_level, r.location_id,
                   {recorded_month} AS recorded_month,
                   s.redbook2021 IS NOT NULL AS is_redbook
            FROM segments seg
            JOIN species s ON s.species_id = seg.species_id
            LEFT JOIN recordings r ON r.recording_id = seg.recording_id
            LEFT JOIN detections d ON d.detection_id = seg.detection_id
            WHERE {visibility} AND {' AND '.join(conditions)}
        ), eligible_species AS (
            SELECT DISTINCT species_id FROM eligible_segments
        ), completed_segments AS (
            SELECT seg.species_id, seg.recording_id, r.location_id,
                   {recorded_month} AS recorded_month,
                   seg.verification_count, seg.positive_verifications
            FROM eligible_species es
            JOIN segments seg ON seg.species_id = es.species_id
            JOIN species s ON s.species_id = seg.species_id
            LEFT JOIN recordings r ON r.recording_id = seg.recording_id
            LEFT JOIN detections d ON d.detection_id = seg.detection_id
            WHERE {visibility} AND seg.status = 'completed' AND seg.verification_count >= 2
        ), confirmed AS (
            SELECT species_id, location_id, recorded_month
            FROM completed_segments
            WHERE 3 * positive_verifications >= 2 * verification_count
        ), confirmed_species AS (
            SELECT DISTINCT species_id FROM confirmed
        ), confirmed_territories AS (
            SELECT c.species_id, li.institution_id,
                   array_agg(DISTINCT c.recorded_month) FILTER (
                       WHERE c.recorded_month IS NOT NULL
                   ) AS confirmed_months
            FROM confirmed c
            JOIN location_institutions li ON li.location_id = c.location_id
            GROUP BY c.species_id, li.institution_id
        ), rejected_recordings AS (
            SELECT DISTINCT species_id, recording_id
            FROM completed_segments
            WHERE 3 * positive_verifications <= verification_count
              AND recording_id IS NOT NULL
        ), candidates AS (
            SELECT seg.id AS segment_id, seg.species_id, seg.recorded_month,
                   seg.confidence_level, territory.name_uk, territory.name_en,
                   CASE WHEN cs.species_id IS NULL THEN 3
                        ELSE COALESCE(territory.novelty, 0) END AS novelty,
                   COALESCE(seg.confidence_level >= :confidence, FALSE) AS plausible,
                   rejected.recording_id IS NOT NULL AS similar_rejected,
                   seg.is_redbook,
                   GREATEST(1, 2 - COALESCE(seg.verification_count, 0),
                            2 * COALESCE(seg.verification_count, 0) -
                            3 * COALESCE(seg.positive_verifications, 0)) AS votes_needed
            FROM eligible_segments seg
            LEFT JOIN confirmed_species cs ON cs.species_id = seg.species_id
            LEFT JOIN rejected_recordings rejected ON rejected.species_id = seg.species_id
                AND rejected.recording_id = seg.recording_id
            LEFT JOIN LATERAL (
                SELECT i.name_uk, i.name_en,
                       CASE WHEN ct.institution_id IS NULL THEN 2
                            WHEN seg.recorded_month IS NOT NULL AND NOT COALESCE(
                                seg.recorded_month = ANY(ct.confirmed_months), FALSE
                            ) THEN 1 ELSE 0 END AS novelty
                FROM location_institutions li
                JOIN institutions i ON i.id = li.institution_id
                LEFT JOIN confirmed_territories ct ON ct.institution_id = i.id
                     AND ct.species_id = seg.species_id
                WHERE li.location_id = seg.location_id {territory_filter}
                ORDER BY novelty DESC, i.id
                LIMIT 1
            ) territory ON TRUE
        )
    """


def recommendations(conn, access_sql, params):
    return conn.execute(text(f"""
        {ranking_ctes(access_sql)}, ranked AS (
            SELECT *, COUNT(*) OVER (PARTITION BY species_id) AS remaining,
                   ROW_NUMBER() OVER (PARTITION BY species_id ORDER BY {PRIORITY_ORDER}) AS species_rank
            FROM candidates
        )
        SELECT * FROM ranked WHERE species_rank = 1
        ORDER BY {PRIORITY_ORDER}
    """), {**params, 'confidence': PRIORITY_CONFIDENCE}).fetchall()
