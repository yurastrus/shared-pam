-- SPDX-License-Identifier: AGPL-3.0-only
-- Migration 0003 — "don't know" verification votes + discard status.
--
-- PURPOSE
--   Support a 4th verification answer, "Не знаю" (unknown), stored as
--   segment_verifications.verification_result = 2 (0=no, 1=yes, 2=unknown).
--   Unknown votes:
--     * hide the segment from that user (a row already excludes it from the
--       next-segment queue) — handled in app code, no schema needed;
--     * must NOT count toward consensus (else 3 "unknown" would look like a
--       unanimous "no" and wrongly complete the segment as rejected);
--     * when a segment collects enough unknowns and has no real yes/no vote,
--       the app marks it status='discarded' so it stops being served.
--
-- CHANGES
--   1. Widen segments_status_check to allow 'discarded'.
--   2. Rewrite the update_segment_stats() trigger so verification_count and the
--      consensus math count ONLY real votes (verification_result IN (0,1)) —
--      unknown/NULL rows are ignored. Everything else in the trigger is
--      unchanged (detection_verification_map linking, status transitions).
--
-- SAFETY
--   Idempotent. The CHECK is only widened (existing values still valid). The
--   trigger is CREATE OR REPLACE. No data is modified.
--
-- HOW TO APPLY (psql)
--   psql "$PAM_DATABASE_URL" -f 0003_verification_unknown.sql

BEGIN;

-- 1. Allow the new 'discarded' status.
ALTER TABLE segments DROP CONSTRAINT IF EXISTS segments_status_check;
ALTER TABLE segments ADD CONSTRAINT segments_status_check
    CHECK ((status)::text = ANY (ARRAY[
        'pending'::text, 'completed'::text, 'archived'::text, 'discarded'::text
    ]));

-- 2. Consensus counts only real (0/1) votes; unknown (2) / NULL are ignored.
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
    v_detection_id INTEGER;
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

    -- detection_verification_map bookkeeping (unchanged logic).
    UPDATE detection_verification_map
    SET
        positive_votes = v_pos_count,
        verification_result = v_consensus_result
    WHERE segment_id = v_segment_id;

    IF NOT FOUND AND v_total_count > 0 THEN
        SELECT d.detection_id INTO v_detection_id
        FROM segments s
        JOIN detections d ON s.species_id = d.species_id
        JOIN recordings r ON d.recording_id = r.recording_id
        WHERE s.id = v_segment_id
          AND s.recorded_date = DATE(r.datetime_start)
          AND s.recorded_time = r.datetime_start::time
          AND (
              (s.start_s IS NOT NULL AND ABS(d.start_s - s.start_s) < 1.0)
              OR
              (ABS(d.confidence - s.confidence_level) < 0.001)
          )
        LIMIT 1;

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
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$function$;

COMMIT;
