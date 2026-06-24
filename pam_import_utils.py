# SPDX-License-Identifier: AGPL-3.0-only
"""
PAM detection import utilities.

Designed to be extensible: add new classifiers/formats by subclassing
BaseDetectionImporter and registering in IMPORTERS.

Two species-resolution modes are supported (``species_lookup_mode``):
  * ``'scientific'`` — the file carries a scientific name; species are upserted
    by ``scientific_name`` (BirdNET Analyzer CSV).
  * ``'common'``     — the file carries only an English common name (Raven
    Selection Tables from BirdNET Analyzer / Chirpity). Species are resolved
    against existing ``species.common_name_en``; unmatched rows are skipped and
    reported (new species are never created, since there is no scientific name).

Model accounting: every import is tagged with a ``model_id`` (from the
``models`` table). Detections stay one row per biological event
``(recording_id, species_id, start_s, end_s)``; each contributing model gets a
row in ``detection_models`` with its own confidence. ``detections.confidence``
keeps the REFERENCE model's (BirdNET 2.4) confidence and is never overwritten by
another model — so BirdNET threshold filtering / evaluation stay intact.
"""

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from typing import Optional
import re
import os
import csv
import io
from datetime import datetime

from sqlalchemy import text


@dataclass
class DetectionRow:
    recording_filename: str
    start_s: float
    end_s: float
    scientific_name: Optional[str] = None
    common_name_en: Optional[str] = None
    confidence: Optional[float] = None


def _parse_datetime_from_filename(filename: str) -> Optional[datetime]:
    """Extract recording datetime from an audio filename: PREFIX_YYYYMMDD_HHMMSS.ext."""
    m = re.search(r'_(\d{8})_(\d{6})\.', filename, re.IGNORECASE)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
        except ValueError:
            pass
    return None


def _basename_from_path(filepath: str) -> str:
    """Normalise a (possibly Windows) path to its basename."""
    return os.path.basename((filepath or '').replace('\\', '/').strip())


def _parse_confidence(raw: Optional[str]) -> Optional[float]:
    """Parse a confidence/score string; return a float in [0, 1] or None."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        c = float(raw)
    except ValueError:
        return None
    return c if 0.0 <= c <= 1.0 else None


class BaseDetectionImporter(ABC):
    """Base class for classifier/format-specific parsers."""

    name: str   # display name shown in UI
    key: str    # identifier used in API calls
    # How the processor maps rows to species:
    #   'scientific' — by DetectionRow.scientific_name (creates species)
    #   'common'     — by DetectionRow.common_name_en against existing species
    species_lookup_mode: str = 'scientific'

    @abstractmethod
    def parse_csv(self, content: str) -> list:
        """Parse file text and return list of DetectionRow."""
        pass

    @abstractmethod
    def parse_datetime(self, filename: str) -> Optional[datetime]:
        """Extract recording datetime from audio filename."""
        pass

    def is_empty_content(self, content: str) -> bool:
        lines = [l for l in content.splitlines() if l.strip()]
        return len(lines) <= 1


class BirdNETImporter(BaseDetectionImporter):
    """
    Parses BirdNET Analyzer output CSV files.
    Handles both current format (spaces in headers) and older R-exported format (dots).
    """

    name = 'BirdNET CSV'
    key = 'birdnet'
    species_lookup_mode = 'scientific'

    _COLUMNS = {
        'start_s':         ['Start (s)',       'Start..s.',       'start_s'],
        'end_s':           ['End (s)',          'End..s.',         'end_s'],
        'scientific_name': ['Scientific name',  'Scientific.name', 'scientific_name'],
        'common_name_en':  ['Common name',      'Common.name',     'common_name_en'],
        'confidence':      ['Confidence',       'confidence'],
        'filepath':        ['File',             'filepath',        'filename'],
    }

    def _resolve_columns(self, headers: list) -> dict:
        return {
            field: next((c for c in candidates if c in headers), None)
            for field, candidates in self._COLUMNS.items()
        }

    def parse_csv(self, content: str) -> list:
        reader = csv.DictReader(io.StringIO(content))
        headers = list(reader.fieldnames or [])
        cols = self._resolve_columns(headers)

        for required in ('start_s', 'end_s', 'scientific_name', 'filepath'):
            if cols[required] is None:
                raise ValueError(
                    f"Cannot find required column '{required}' in headers: {headers}"
                )

        rows = []
        for r in reader:
            try:
                filename = _basename_from_path(r.get(cols['filepath']))
                if not filename:
                    continue

                sci = (r.get(cols['scientific_name']) or '').strip()
                if not sci:
                    continue

                start_s = float(r[cols['start_s']])
                end_s   = float(r[cols['end_s']])

                confidence = _parse_confidence(r.get(cols['confidence'])) if cols['confidence'] else None

                common = None
                if cols['common_name_en']:
                    raw = (r.get(cols['common_name_en']) or '').strip()
                    if raw:
                        common = raw

                rows.append(DetectionRow(
                    scientific_name=sci,
                    common_name_en=common,
                    start_s=start_s,
                    end_s=end_s,
                    confidence=confidence,
                    recording_filename=filename,
                ))
            except (ValueError, TypeError, KeyError):
                continue

        return rows

    def parse_datetime(self, filename: str) -> Optional[datetime]:
        return _parse_datetime_from_filename(filename)


class RavenSelectionTableImporter(BaseDetectionImporter):
    """
    Parses Raven Selection Table exports (tab-separated .txt).

    Both BirdNET Analyzer and Chirpity export this format, so one parser covers
    both. The only structural difference is that BirdNET adds a 'Species Code'
    column; Chirpity does not. Neither carries a scientific name, so species are
    resolved by common name (``species_lookup_mode = 'common'``).

    Notes:
      * A single Raven table may span many recordings — the recording is taken
        per-row from the 'Begin Path' column (basename of the .wav).
      * BirdNET emits "nocall" rows (no detection); these are skipped.
      * Confidence ('Confidence' or 'Score') is already in [0, 1] for both tools.
    """

    name = 'Raven Selection Table'
    key = 'raven'
    species_lookup_mode = 'common'

    _COLUMNS = {
        'begin_time':     ['Begin Time (s)'],
        'end_time':       ['End Time (s)'],
        'file_offset':    ['File Offset (s)'],
        'common_name_en': ['Common Name', 'Common name'],
        'confidence':     ['Confidence', 'Score'],
        'filepath':       ['Begin Path', 'Begin File'],
    }

    _SKIP_LABELS = {'nocall', 'no call', 'noise'}

    def _resolve_columns(self, headers: list) -> dict:
        return {
            field: next((c for c in candidates if c in headers), None)
            for field, candidates in self._COLUMNS.items()
        }

    def parse_csv(self, content: str) -> list:
        reader = csv.DictReader(io.StringIO(content), delimiter='\t')
        headers = list(reader.fieldnames or [])
        cols = self._resolve_columns(headers)

        for required in ('begin_time', 'end_time', 'common_name_en', 'filepath'):
            if cols[required] is None:
                raise ValueError(
                    f"Cannot find required column '{required}' in Raven headers: {headers}"
                )

        rows = []
        for r in reader:
            try:
                filename = _basename_from_path(r.get(cols['filepath']))
                if not filename:
                    continue

                common = (r.get(cols['common_name_en']) or '').strip()
                if not common or common.lower() in self._SKIP_LABELS:
                    continue

                # 'Begin/End Time (s)' are positions in Raven's concatenated
                # analysis stream — for a multi-file table they run across the
                # whole batch. The within-recording offset is 'File Offset (s)',
                # which matches the convention used by existing BirdNET
                # detections (0, 3, 6, …). Use the file offset for start_s and
                # carry the selection's duration over to end_s. Rounding kills
                # float noise (e.g. 10006.916000000001) so the values stay
                # float4-stable and the detection upsert keys match cleanly.
                begin = float(r[cols['begin_time']])
                end   = float(r[cols['end_time']])
                duration = round(end - begin, 3)
                if cols['file_offset'] and (r.get(cols['file_offset']) or '').strip():
                    start_s = round(float(r[cols['file_offset']]), 3)
                else:
                    start_s = round(begin, 3)
                end_s = round(start_s + duration, 3)

                confidence = _parse_confidence(r.get(cols['confidence'])) if cols['confidence'] else None

                rows.append(DetectionRow(
                    scientific_name=None,
                    common_name_en=common,
                    start_s=start_s,
                    end_s=end_s,
                    confidence=confidence,
                    recording_filename=filename,
                ))
            except (ValueError, TypeError, KeyError):
                continue

        return rows

    def parse_datetime(self, filename: str) -> Optional[datetime]:
        return _parse_datetime_from_filename(filename)


IMPORTERS = {
    'birdnet': BirdNETImporter(),
    'raven':   RavenSelectionTableImporter(),
}

_DETECTION_BATCH_SIZE = 300


class PAMImportProcessor:
    """
    Processes uploaded detection files and inserts them into the PAM database,
    tagging every detection with the importing model.

    Usage:
        processor = PAMImportProcessor(
            get_pam_engine(), location_id, IMPORTERS['raven'],
            model_id=2, reference_model_id=1)
        stats = processor.process_batch(request.files.getlist('files'))
    """

    def __init__(self, engine, location_id: int, importer: BaseDetectionImporter,
                 duration_minutes=5, model_id: Optional[int] = None,
                 reference_model_id: Optional[int] = None,
                 confidence_threshold: float = 0.0):
        self.engine = engine
        self.location_id = location_id
        self.importer = importer
        # Duration of a single audio file (min) — uniform for the entire import
        # batch, set in the pam/import form (default 5). Stored in
        # recordings.duration_minutes.
        self.duration_minutes = duration_minutes
        # Which model produced these detections, and which model_id is the
        # reference (BirdNET 2.4) that owns detections.confidence.
        self.model_id = model_id
        self.reference_model_id = reference_model_id
        # Minimum confidence to import; rows below it are dropped before
        # insertion (set in the pam/import form, default 0.1 at the route layer).
        # Filtering uses each row's own confidence, regardless of model. Rows
        # with NULL confidence are always kept (no value to compare).
        self.confidence_threshold = confidence_threshold
        self.stats = {
            'files_processed': 0,
            'files_empty': 0,
            'files_failed': 0,
            'recordings_new': 0,
            'recordings_existing': 0,
            'detections_inserted': 0,      # new biological events (rows in detections)
            'detections_duplicate': 0,     # events that already existed
            'detections_filtered': 0,      # rows dropped below the confidence threshold
            'model_links_new': 0,          # new rows in detection_models
            'model_links_existing': 0,     # detection_models rows already present (refreshed)
            'species_count': 0,
            'rows_skipped_unknown_species': 0,  # rows whose common name matched no species
        }
        # common-name -> count, for reporting which labels were dropped
        self._skipped_species = Counter()

    @property
    def _is_reference_model(self) -> bool:
        return (self.model_id is not None
                and self.model_id == self.reference_model_id)

    def _species_key(self, row: DetectionRow):
        if self.importer.species_lookup_mode == 'common':
            return (row.common_name_en or '').strip().lower()
        return row.scientific_name

    def process_batch(self, files) -> dict:
        """Process a list of FileStorage objects and return accumulated stats."""
        if self.model_id is None:
            raise ValueError("model_id is required to import detections")

        rows_by_filename = {}

        for f in files:
            try:
                content = f.read().decode('utf-8', errors='replace')
                if self.importer.is_empty_content(content):
                    self.stats['files_empty'] += 1
                    continue
                rows = self.importer.parse_csv(content)
                if not rows:
                    self.stats['files_empty'] += 1
                    continue
                for row in rows:
                    rows_by_filename.setdefault(row.recording_filename, []).append(row)
                self.stats['files_processed'] += 1
            except Exception:
                self.stats['files_failed'] += 1

        if not rows_by_filename:
            return self._finalise_stats()

        conn = None
        try:
            conn = self.engine.connect()
            with conn.begin():
                species_ids = self._resolve_species(conn, rows_by_filename)
                for filename, rows in rows_by_filename.items():
                    self._process_recording(conn, filename, rows, species_ids)
        finally:
            if conn:
                conn.close()

        return self._finalise_stats()

    def _finalise_stats(self) -> dict:
        self.stats['skipped_species'] = dict(self._skipped_species)
        return self.stats

    # ── species resolution ──────────────────────────────────────────────────

    def _resolve_species(self, conn, rows_by_filename: dict) -> dict:
        if self.importer.species_lookup_mode == 'common':
            return self._resolve_species_by_common_name(conn, rows_by_filename)
        return self._upsert_species(conn, rows_by_filename)

    def _upsert_species(self, conn, rows_by_filename: dict) -> dict:
        """Scientific-name mode: create/keep species, return {scientific_name: id}."""
        species_map = {}
        for rows in rows_by_filename.values():
            for r in rows:
                if r.scientific_name and r.scientific_name not in species_map:
                    species_map[r.scientific_name] = r.common_name_en

        if not species_map:
            return {}

        conn.execute(text("""
            INSERT INTO species (scientific_name, common_name_en)
            VALUES (:sci, :com)
            ON CONFLICT (scientific_name) DO UPDATE
                SET common_name_en = COALESCE(species.common_name_en, EXCLUDED.common_name_en)
        """), [{'sci': k, 'com': v} for k, v in species_map.items()])

        result = conn.execute(
            text("SELECT scientific_name, species_id FROM species WHERE scientific_name = ANY(:names)"),
            {'names': list(species_map.keys())}
        )
        ids = {row[0]: row[1] for row in result}
        self.stats['species_count'] += len(ids)
        return ids

    def _resolve_species_by_common_name(self, conn, rows_by_filename: dict) -> dict:
        """Common-name mode: resolve against existing species ONLY (never create).

        Returns {lower(common_name_en): species_id}. Common names with no match
        are simply absent from the map; such rows are skipped and reported.
        """
        wanted = set()
        for rows in rows_by_filename.values():
            for r in rows:
                cn = (r.common_name_en or '').strip()
                if cn:
                    wanted.add(cn.lower())

        if not wanted:
            return {}

        result = conn.execute(text("""
            SELECT lower(common_name_en) AS cn, species_id
            FROM species
            WHERE common_name_en IS NOT NULL
              AND lower(common_name_en) = ANY(:names)
        """), {'names': list(wanted)})
        # If two species shared a common name we'd get duplicates; keep the
        # first deterministically (lowest species_id wins via ORDER not needed
        # because such collisions don't exist in this catalogue, but be safe).
        ids = {}
        for row in result:
            ids.setdefault(row.cn, row.species_id)
        self.stats['species_count'] += len(ids)
        return ids

    # ── recording + detection insertion ──────────────────────────────────────

    def _process_recording(self, conn, filename: str, rows: list, species_ids: dict):
        dt = self.importer.parse_datetime(filename)

        result = conn.execute(text("""
            INSERT INTO recordings (filename, location_id, datetime_start, duration_minutes)
            VALUES (:fn, :loc, :dt, :dur)
            ON CONFLICT (filename) DO UPDATE
                SET location_id     = EXCLUDED.location_id,
                    datetime_start  = COALESCE(EXCLUDED.datetime_start, recordings.datetime_start),
                    duration_minutes = EXCLUDED.duration_minutes
            RETURNING recording_id, (xmax = 0) AS was_inserted
        """), {'fn': filename, 'loc': self.location_id, 'dt': dt,
               'dur': self.duration_minutes})

        row = result.fetchone()
        if not row:
            return

        recording_id, was_inserted = row[0], row[1]
        if was_inserted:
            self.stats['recordings_new'] += 1
        else:
            self.stats['recordings_existing'] += 1

        detections = []
        for r in rows:
            key = self._species_key(r)
            sp = species_ids.get(key)
            if sp is None:
                self.stats['rows_skipped_unknown_species'] += 1
                self._skipped_species[(r.common_name_en or r.scientific_name or '').strip()] += 1
                continue
            # Drop low-confidence detections before insertion. NULL confidence is
            # kept (no value to compare against the threshold).
            if r.confidence is not None and r.confidence < self.confidence_threshold:
                self.stats['detections_filtered'] += 1
                continue
            detections.append({'rec': recording_id, 'sp': sp,
                               'start': r.start_s, 'end': r.end_s, 'conf': r.confidence})

        if not detections:
            return

        for i in range(0, len(detections), _DETECTION_BATCH_SIZE):
            batch = detections[i:i + _DETECTION_BATCH_SIZE]
            new_events, dup_events, new_links, existing_links = \
                self._insert_detections_batch(conn, batch)
            self.stats['detections_inserted'] += new_events
            self.stats['detections_duplicate'] += dup_events
            self.stats['model_links_new'] += new_links
            self.stats['model_links_existing'] += existing_links

    def _insert_detections_batch(self, conn, batch: list):
        """
        Two-phase, idempotent insert for one batch:
          1. upsert detections by the real unique key
             (recording_id, species_id, start_s, end_s) → get detection_id;
          2. upsert detection_models (detection_id, model_id, confidence).

        detections.confidence is only written for the reference model (BirdNET
        2.4); other models never touch it. Per-model confidence always lands in
        detection_models. Returns
        (new_events, dup_events, new_links, existing_links).
        """
        is_ref = self._is_reference_model

        # Dedup within the batch by natural key — Postgres forbids an
        # ON CONFLICT DO UPDATE from affecting the same row twice in one
        # statement. Keep the max confidence among duplicates.
        deduped = {}
        for d in batch:
            k = (d['rec'], d['sp'], float(d['start']), float(d['end']))
            cur = deduped.get(k)
            if cur is None or (d['conf'] is not None and (cur is None or cur < d['conf'])):
                deduped[k] = d['conf']
        keys = list(deduped.keys())

        # ── Phase 1: detections ──
        placeholders, params = [], {}
        for i, (rec, sp, s, e) in enumerate(keys):
            placeholders.append(f"(:rec{i}, :sp{i}, :s{i}, :e{i}, :c{i})")
            params[f'rec{i}'] = rec
            params[f'sp{i}']  = sp
            params[f's{i}']   = s
            params[f'e{i}']   = e
            # On a NEW row: reference model writes its confidence; others write
            # NULL (the reference model has not seen this event).
            params[f'c{i}']   = deduped[(rec, sp, s, e)] if is_ref else None

        # On CONFLICT: reference refreshes confidence; others keep the existing
        # value (no-op update so the row is still RETURNED with its id).
        set_clause = ("confidence = EXCLUDED.confidence" if is_ref
                      else "confidence = detections.confidence")

        det_sql = text(f"""
            INSERT INTO detections (recording_id, species_id, start_s, end_s, confidence)
            VALUES {', '.join(placeholders)}
            ON CONFLICT (recording_id, species_id, start_s, end_s) DO UPDATE
                SET {set_clause}
            RETURNING detection_id, recording_id, species_id, start_s, end_s, (xmax = 0) AS was_inserted
        """)
        det_rows = conn.execute(det_sql, params).fetchall()

        id_map = {}
        new_events = 0
        for r in det_rows:
            id_map[(r.recording_id, r.species_id, float(r.start_s), float(r.end_s))] = r.detection_id
            if r.was_inserted:
                new_events += 1
        dup_events = len(det_rows) - new_events

        # ── Phase 2: detection_models ──
        dm_placeholders, dm_params = [], {}
        for i, k in enumerate(keys):
            did = id_map.get(k)
            if did is None:
                continue
            dm_placeholders.append(f"(:d{i}, :m{i}, :dc{i})")
            dm_params[f'd{i}']  = did
            dm_params[f'm{i}']  = self.model_id
            dm_params[f'dc{i}'] = deduped[k]

        new_links = existing_links = 0
        if dm_placeholders:
            dm_sql = text(f"""
                WITH up AS (
                    INSERT INTO detection_models (detection_id, model_id, confidence)
                    VALUES {', '.join(dm_placeholders)}
                    ON CONFLICT (detection_id, model_id) DO UPDATE
                        SET confidence = EXCLUDED.confidence
                    RETURNING (xmax = 0) AS was_inserted
                )
                SELECT count(*) FILTER (WHERE was_inserted), count(*) FROM up
            """)
            new_links, total_links = conn.execute(dm_sql, dm_params).fetchone()
            existing_links = total_links - new_links

        return new_events, dup_events, new_links, existing_links
