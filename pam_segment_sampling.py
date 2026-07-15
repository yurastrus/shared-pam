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
import os
import re
import math
import shutil
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


# ── sampling ──────────────────────────────────────────────────────────────────

def build_sampling_query(n_strata):
    """Return the parameterised stratified-sampling SQL.

    Kept as a pure function (no DB, no I/O) so it is unit-testable. ``n_strata``
    is interpolated into the SQL text because it drives ``ntile()``'s argument;
    every value that depends on user input stays a bound parameter.

    Bind params expected by the returned SQL:
        :species_name, :location_ids (list), :conf_thr, :per_stratum
    """
    n_strata = max(1, int(n_strata))
    return text(f"""
        WITH candidates AS (
            SELECT d.detection_id,
                   d.recording_id,
                   d.species_id,
                   r.filename       AS rec_filename,
                   r.datetime_start,
                   l.location_name,
                   d.start_s,
                   d.end_s,
                   d.confidence,
                   ntile({n_strata}) OVER (ORDER BY d.confidence) AS stratum
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations  l ON r.location_id  = l.location_id
            JOIN species    s ON d.species_id   = s.species_id
            WHERE s.scientific_name = :species_name
              AND r.location_id = ANY(:location_ids)
              AND d.confidence IS NOT NULL
              AND d.confidence >= :conf_thr
              AND NOT EXISTS (
                    SELECT 1 FROM segments sg
                    WHERE sg.detection_id = d.detection_id
              )
        ),
        ranked AS (
            SELECT candidates.*,
                   row_number() OVER (PARTITION BY stratum ORDER BY random()) AS rn
            FROM candidates
        )
        SELECT detection_id, recording_id, species_id, rec_filename,
               datetime_start, location_name, start_s, end_s, confidence
        FROM ranked
        WHERE rn <= :per_stratum
        ORDER BY confidence
    """)


def run_stratified_sample(species_name, location_ids, confidence_threshold=0.1,
                          n_strata=10, sample_size=700, conn=None):
    """Draw a confidence-stratified detection sample for one species.

    Mirrors ``produce_random_segments.R``: split the confidence range into
    ``n_strata`` quantile bins (``ntile``) and take up to
    ``ceil(sample_size / n_strata)`` random detections per bin — so low- and
    high-confidence events are both represented. Already-uploaded detections are
    excluded by the query itself.

    Returns a list of plain dicts (JSON-serialisable) — one per sampled
    detection — carrying everything the browser needs to cut + label the clip.
    """
    if not location_ids:
        return []
    n_strata = max(1, int(n_strata))
    sample_size = max(1, min(int(sample_size), MAX_SAMPLE_PER_SPECIES))
    per_stratum = math.ceil(sample_size / n_strata)

    own_conn = conn is None
    if own_conn:
        conn = get_pam_db_connection()
    try:
        rows = conn.execute(build_sampling_query(n_strata), {
            'species_name': species_name,
            'location_ids': list(location_ids),
            'conf_thr': float(confidence_threshold),
            'per_stratum': per_stratum,
        }).mappings().fetchall()
    finally:
        if own_conn and conn is not None:
            conn.close()

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
            'recording_filename': rec_filename,
            'segment_filename': seg_filename,
            'location_name': _location_token(rec_filename, row['location_name']),
            'start_s': float(start_s) if start_s is not None else None,
            'end_s': float(row['end_s']) if row['end_s'] is not None else None,
            'confidence': float(conf) if conf is not None else None,
            'recorded_date': dt.date().isoformat() if dt else None,
            'recorded_time': dt.time().isoformat() if dt else None,
        })
    return result


# ── filename helpers ────────────────────────────────────────────────────────

def _recording_stem(rec_filename):
    """Recording filename without its audio extension."""
    base = os.path.basename(rec_filename or '')
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
                             recorded_date, recorded_time, file_path):
    """Insert one sampled segment, with explicit detection/recording links.

    Idempotent per detection: if a segment for this ``detection_id`` already
    exists (e.g. a double-click, or it was uploaded via the old path and
    backfilled) the insert is skipped and ``None`` is returned. Otherwise the
    new segment id is returned. Runs inside the caller's transaction.

    ``status`` is ``'pending'`` so the segment flows through the standard
    verification interface exactly like a ZIP-uploaded one.
    """
    dup = conn.execute(text("""
        SELECT id FROM segments
        WHERE detection_id = :detection_id
           OR filename = :filename
        LIMIT 1
    """), {'detection_id': detection_id, 'filename': segment_filename}).fetchone()
    if dup:
        current_app.logger.info(
            f"Sampled segment skipped (duplicate): detection={detection_id} "
            f"filename={segment_filename}")
        return None

    row = conn.execute(text("""
        INSERT INTO segments
            (species_id, filename, confidence_level, location_name,
             recorded_date, recorded_time, file_path, upload_date, status,
             recording_id, detection_id)
        VALUES
            (:species_id, :filename, :confidence, :location_name,
             :recorded_date, :recorded_time, :file_path, :upload_date, 'pending',
             :recording_id, :detection_id)
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
    }).fetchone()
    return row[0] if row else None


def save_and_register_segment(wav_bytes, meta, upload_directory, conn):
    """Encode a WAV clip to FLAC on disk and register it. Returns (status, id).

    status ∈ {'saved', 'duplicate', 'error'}. On 'duplicate' the just-written
    FLAC is removed so we don't leave orphan files. Any exception is turned into
    ('error', None) after cleanup — the route reports it per-file.
    """
    species_dir = _safe_species_dirname(meta['species_name'])
    flac_name = meta['segment_filename']
    if not flac_name.lower().endswith('.flac'):
        flac_name = os.path.splitext(flac_name)[0] + '.flac'
    final_path = os.path.join(upload_directory, species_dir, flac_name)

    wrote_file = False
    try:
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
            )
        if seg_id is None:
            _quiet_remove(final_path)
            return 'duplicate', None
        return 'saved', seg_id
    except Exception as e:
        current_app.logger.error(
            f"Error saving sampled segment {flac_name}: {e}")
        if wrote_file:
            _quiet_remove(final_path)
        return 'error', None


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
