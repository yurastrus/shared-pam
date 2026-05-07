"""
PAM detection import utilities.

Designed to be extensible: add new classifiers by subclassing BaseDetectionImporter
and registering in IMPORTERS.
"""

from abc import ABC, abstractmethod
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
    scientific_name: str
    recording_filename: str
    start_s: float
    end_s: float
    common_name_en: Optional[str] = None
    confidence: Optional[float] = None


class BaseDetectionImporter(ABC):
    """Base class for classifier-specific CSV parsers."""

    name: str   # display name shown in UI
    key: str    # identifier used in API calls

    @abstractmethod
    def parse_csv(self, content: str) -> list:
        """Parse CSV text and return list of DetectionRow."""
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

    name = 'BirdNET'
    key = 'birdnet'

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
                filepath = (r.get(cols['filepath']) or '').strip()
                # BirdNET uses Windows backslashes; normalise to basename
                filename = os.path.basename(filepath.replace('\\', '/'))
                if not filename:
                    continue

                sci = (r.get(cols['scientific_name']) or '').strip()
                if not sci:
                    continue

                start_s = float(r[cols['start_s']])
                end_s   = float(r[cols['end_s']])

                confidence = None
                if cols['confidence']:
                    raw = (r.get(cols['confidence']) or '').strip()
                    if raw:
                        try:
                            c = float(raw)
                            if 0.0 <= c <= 1.0:
                                confidence = c
                        except ValueError:
                            pass

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
        # Filename pattern: PREFIX_YYYYMMDD_HHMMSS.ext
        m = re.search(r'_(\d{8})_(\d{6})\.', filename, re.IGNORECASE)
        if m:
            try:
                return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
            except ValueError:
                pass
        return None


IMPORTERS = {
    'birdnet': BirdNETImporter(),
}

_DETECTION_BATCH_SIZE = 300


class PAMImportProcessor:
    """
    Processes uploaded CSV files and inserts detections into the PAM database.

    Usage:
        processor = PAMImportProcessor(get_pam_engine(), location_id, IMPORTERS['birdnet'])
        stats = processor.process_batch(request.files.getlist('files'))
    """

    def __init__(self, engine, location_id: int, importer: BaseDetectionImporter):
        self.engine = engine
        self.location_id = location_id
        self.importer = importer
        self.stats = {
            'files_processed': 0,
            'files_empty': 0,
            'files_failed': 0,
            'recordings_new': 0,
            'recordings_existing': 0,
            'detections_inserted': 0,
            'detections_duplicate': 0,
            'species_count': 0,
        }

    def process_batch(self, files) -> dict:
        """Process a list of FileStorage objects and return accumulated stats."""
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
            return self.stats

        conn = None
        try:
            conn = self.engine.connect()
            with conn.begin():
                species_ids = self._upsert_species(conn, rows_by_filename)
                for filename, rows in rows_by_filename.items():
                    self._process_recording(conn, filename, rows, species_ids)
        finally:
            if conn:
                conn.close()

        return self.stats

    def _upsert_species(self, conn, rows_by_filename: dict) -> dict:
        species_map = {}
        for rows in rows_by_filename.values():
            for r in rows:
                if r.scientific_name not in species_map:
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

    def _process_recording(self, conn, filename: str, rows: list, species_ids: dict):
        dt = self.importer.parse_datetime(filename)

        result = conn.execute(text("""
            INSERT INTO recordings (filename, location_id, datetime_start)
            VALUES (:fn, :loc, :dt)
            ON CONFLICT (filename) DO UPDATE
                SET location_id    = EXCLUDED.location_id,
                    datetime_start = COALESCE(EXCLUDED.datetime_start, recordings.datetime_start)
            RETURNING recording_id, (xmax = 0) AS was_inserted
        """), {'fn': filename, 'loc': self.location_id, 'dt': dt})

        row = result.fetchone()
        if not row:
            return

        recording_id, was_inserted = row[0], row[1]
        if was_inserted:
            self.stats['recordings_new'] += 1
        else:
            self.stats['recordings_existing'] += 1

        detections = [
            {'rec': recording_id, 'sp': species_ids[r.scientific_name],
             'start': r.start_s, 'end': r.end_s, 'conf': r.confidence}
            for r in rows if r.scientific_name in species_ids
        ]

        if not detections:
            return

        for i in range(0, len(detections), _DETECTION_BATCH_SIZE):
            batch = detections[i:i + _DETECTION_BATCH_SIZE]
            inserted = self._insert_detections_batch(conn, batch)
            self.stats['detections_inserted'] += inserted
            self.stats['detections_duplicate'] += len(batch) - inserted

    def _insert_detections_batch(self, conn, batch: list) -> int:
        """
        Insert a batch of detections as a single SQL statement with CTE
        to reliably count actually-inserted rows (vs ON CONFLICT DO NOTHING skips).
        """
        placeholders = []
        params = {}
        for i, d in enumerate(batch):
            placeholders.append(f"(:rec{i}, :sp{i}, :s{i}, :e{i}, :c{i})")
            params[f'rec{i}'] = d['rec']
            params[f'sp{i}']  = d['sp']
            params[f's{i}']   = d['start']
            params[f'e{i}']   = d['end']
            params[f'c{i}']   = d['conf']

        sql = text(f"""
            WITH ins AS (
                INSERT INTO detections (recording_id, species_id, start_s, end_s, confidence)
                VALUES {', '.join(placeholders)}
                ON CONFLICT DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) FROM ins
        """)
        result = conn.execute(sql, params)
        return result.fetchone()[0]
