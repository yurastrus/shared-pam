-- SPDX-License-Identifier: AGPL-3.0-only
-- Migration 0004 — detection_verification_map keyed on authoritative detection_id.
--
-- PURPOSE
--   The verification charts (pam_detailed via get_time_scatter_data) read
--   detection_verification_map (dvm). Historically dvm was filled by fragile
--   matching: a filename-stem batch linker AND this trigger's heuristic fallback
--   (datetime + species + start_s≈ OR confidence≈, with NO location constraint,
--   LIMIT 1). That produced, on prod: 1855 verified segments missing from dvm,
--   162 rows pointing to the wrong detection, 133 mapped to a detection at a
--   DIFFERENT location.
--
--   Since the sample-upload path (migration 0002) and the legacy backfill both set
--   segments.detection_id explicitly and unambiguously, dvm must simply follow
--   that column. This migration rewrites the dvm bookkeeping inside
--   update_segment_stats() to upsert by segments.detection_id and drops the
--   heuristic fallback entirely. The segment-stats/consensus half is unchanged
--   from migration 0003.
--
-- SAFETY
--   Idempotent (CREATE OR REPLACE). No data modified by this file. A one-time
--   full rebuild of dvm from segments.detection_id is done separately
--   (scripts/rebuild_dvm.py) — this only fixes go-forward trigger behaviour.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0004_dvm_authoritative_link.sql

BEGIN;

CREATE OR REPLACE FUNCTION public.update_segment_stats()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    consensus_threshold decimal := 2.0/3.0;
    v_segment_id INTEGER;
    v_total_count INTEGER;
    v_pos_count INTEGER;
    v_consensus_result INTEGER := NULL;
    v_detection_id BIGINT;
BEGIN
    v_segment_id := COALESCE(NEW.segment_id, OLD.segment_id);

    -- Count ONLY meaningful votes (0/1); "unknown" (2) and NULL are excluded
    -- so they never move a segment toward consensus.
    SELECT COUNT(*) FILTER (WHERE verification_result IN (0, 1)),
           COUNT(*) FILTER (WHERE verification_result = 1)
    INTO v_total_count, v_pos_count
    FROM segment_verifications
    WHERE segment_id = v_segment_id;

    IF v_total_count >= 2 THEN
        IF (v_pos_count::decimal / v_total_count) >= consensus_threshold THEN
            v_consensus_result := 1;
        ELSIF (v_pos_count::decimal / v_total_count) <= (1 - consensus_threshold) THEN
            v_consensus_result := 0;
        END IF;
    END IF;

    -- Update SEGMENTS stats + status. Never override a 'discarded' segment
    -- back to 'pending' (discard is a terminal state set by the app).
    UPDATE segments
    SET
        verification_count = v_total_count,
        positive_verifications = v_pos_count,
        status = CASE
            WHEN v_consensus_result IS NOT NULL THEN 'completed'
            WHEN status = 'discarded' THEN 'discarded'
            ELSE 'pending'
        END
    WHERE id = v_segment_id;

    -- detection_verification_map: keyed on the AUTHORITATIVE segments.detection_id
    -- (set at sample-upload / legacy backfill). No heuristic filename/datetime
    -- matching. If the segment has no detection link yet, there is nothing to map.
    SELECT detection_id INTO v_detection_id FROM segments WHERE id = v_segment_id;

    IF v_detection_id IS NOT NULL THEN
        INSERT INTO detection_verification_map
            (detection_id, segment_id, verification_result, positive_votes)
        VALUES
            (v_detection_id, v_segment_id, v_consensus_result, v_pos_count)
        ON CONFLICT (detection_id) DO UPDATE SET
            positive_votes = EXCLUDED.positive_votes,
            verification_result = EXCLUDED.verification_result,
            segment_id = EXCLUDED.segment_id;
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$function$;

COMMIT;
