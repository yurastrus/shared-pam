# SPDX-License-Identifier: AGPL-3.0-only
import re
from flask import current_app
from sqlalchemy import text
from .utils import get_pam_db_connection
import traceback
import math
import time

def link_verifications_to_detections(full_resync=False):
    """
    Link verification segments to PAM detections.

    Connection management fully redesigned: each batch is processed on its own
    fresh connection, guaranteeing the absence of transaction errors.
    """
    BATCH_SIZE = 200
    segments_to_process = []
    recordings_map = {}
    conn_setup = None

    try:
        # === STEP 1: Setup — fetch data outside the loop. ===
        # A separate connection is used and closed immediately.
        current_app.logger.info("Step 1: Fetching initial data...")
        conn_setup = get_pam_db_connection()
        if full_resync:
            with conn_setup.begin():
                conn_setup.execute(text("TRUNCATE TABLE detection_verification_map RESTART IDENTITY"))
            current_app.logger.info("Mapping table truncated.")
        
        if full_resync:
            query_sql = "SELECT id, filename, species_id, verification_count, positive_verifications FROM segments WHERE status IN ('completed', 'archived') ORDER BY id"
        else:
            query_sql = """
                SELECT s.id, s.filename, s.species_id, s.verification_count, s.positive_verifications
                FROM segments s LEFT JOIN detection_verification_map dvm ON s.id = dvm.segment_id
                WHERE s.status IN ('completed', 'archived') AND dvm.segment_id IS NULL ORDER BY s.id
            """
        segments_to_process = conn_setup.execute(text(query_sql)).fetchall()
        all_recordings = conn_setup.execute(text("SELECT recording_id, filename FROM recordings")).fetchall()
        recordings_map = {rec.filename.rsplit('.', 1)[0]: rec.recording_id for rec in all_recordings}
    finally:
        if conn_setup:
            conn_setup.close()
        current_app.logger.info("Step 1 finished. Setup connection closed.")

    if not segments_to_process:
        return "No new segments to link."

    # === STEP 2: Batch processing with short transactions. ===
    total_segments = len(segments_to_process)
    total_linked = 0
    total_batches = math.ceil(total_segments / BATCH_SIZE)
    current_app.logger.info(f"Step 2: Starting to process {total_segments} segments in {total_batches} batches.")

    for i in range(0, total_segments, BATCH_SIZE):
        batch_num = (i // BATCH_SIZE) + 1
        batch = segments_to_process[i:i + BATCH_SIZE]
        conn_batch = None
        
        try:
            # === PER-BATCH: new connection and new transaction. ===
            conn_batch = get_pam_db_connection()
            with conn_batch.begin():
                start_time = time.time()
                current_app.logger.info(f"--- Processing Batch {batch_num}/{total_batches} ---")
                
                # Batch processing logic (unchanged).
                data_to_insert = []
                # ... (all parsing, key-lookup logic, etc.)
                detection_keys, parsed_batch = set(), []
                for segment in batch:
                    match = re.match(r'^(\d+\.\d+)_([A-Za-z0-9\-]+)_(\d{8})_(\d{6})_sec(\d+)_part(\d+)\.(wav|flac)$', segment.filename, re.IGNORECASE)
                    if not match: continue
                    _, location, date_str, time_str, start_s_str, _, _ = match.groups()
                    original_rec_name = f"{location}_{date_str}_{time_str}"
                    recording_id = recordings_map.get(original_rec_name)
                    if recording_id:
                        key = (recording_id, segment.species_id, int(start_s_str))
                        detection_keys.add(key)
                        parsed_batch.append({"segment": segment, "lookup_key": key})

                detections_map = {}
                if detection_keys:
                    values_str = ", ".join(f"({r}, {s}, {t})" for r, s, t in detection_keys)
                    detections_query = text(f"SELECT detection_id, recording_id, species_id, start_s FROM detections WHERE (recording_id, species_id, start_s) IN ({values_str})")
                    found_detections = conn_batch.execute(detections_query).fetchall()
                    for det in found_detections:
                        detections_map[(det.recording_id, det.species_id, det.start_s)] = det.detection_id

                for item in parsed_batch:
                    detection_id = detections_map.get(item['lookup_key'])
                    if not detection_id: continue
                    segment = item['segment']
                    consensus_threshold = 2.0 / 3.0
                    verification_ratio = segment.positive_verifications / segment.verification_count if segment.verification_count > 0 else 0
                    
                    verification_result = -1
                    if verification_ratio >= consensus_threshold: verification_result = 1
                    elif verification_ratio <= (1 - consensus_threshold): verification_result = 0
                    
                    if verification_result != -1:
                        data_to_insert.append({"detection_id": detection_id, "segment_id": segment.id, "verification_result": verification_result})

                if data_to_insert:
                    insert_query = text("""
                        INSERT INTO detection_verification_map (detection_id, segment_id, verification_result)
                        VALUES (:detection_id, :segment_id, :verification_result)
                        ON CONFLICT (detection_id, segment_id) DO UPDATE SET verification_result = EXCLUDED.verification_result;
                    """)
                    conn_batch.execute(insert_query, data_to_insert)
                    
                    batch_linked_count = len(data_to_insert)
                    total_linked += batch_linked_count
                    processing_time = time.time() - start_time
                    current_app.logger.info(f"Batch {batch_num}/{total_batches} committed. Linked: {batch_linked_count}. Time: {processing_time:.2f}s")
        except Exception as e:
            current_app.logger.error(f"Failed to process batch {batch_num}. Skipping. Error: {e}")
            current_app.logger.error(traceback.format_exc())
        finally:
            # Guarantee the connection for this batch is closed.
            if conn_batch:
                conn_batch.close()
    
    summary = f"Total processed: {total_segments}. Total successfully linked: {total_linked}."
    current_app.logger.info(summary)
    return summary