# SPDX-License-Identifier: AGPL-3.0-only
"""
Automatic assignment of biotopes to PAM monitoring points from landcover.

WHY
===
Biotopes power the biotope filters on PAM dashboards, but they are assigned to
``locations`` by hand and roughly half the points have none. This fills the gap
automatically from remote sensing — the PAM counterpart of
``app/camera_traps/biotope_autoassign.py``.

HOW
===
For every point we sample the ESA WorldCover raster in a radius around the point
(default 100 m) via Google Earth Engine, build a histogram of class → pixel
count, take the top-N classes (default 3, to shrug off noise), map each class to
a biotope through ``biotope_landcover_map``, and **add** the resulting biotopes
to the point. Existing assignments are never removed — only missing biotopes are
appended (``ON CONFLICT DO NOTHING`` on the ``location_biotopes`` M2M).

PORTABILITY / GRACEFUL DEGRADATION
==================================
Self-contained: it does NOT import app.sdm or app.camera_traps (PAM is the public
shared-pam submodule with its own pam_db). Earth Engine is imported lazily, so an
installation without ``earthengine-api`` or without a GEE service-account key
keeps working — ``gee_landcover_available()`` returns False and the admin button
is hidden/disabled. Any failure during a run is caught and recorded as status
``failed``; it never breaks a request.

SCHEMA
======
Two tables in pam_db, created idempotently by ``ensure_schema()`` (there is no
committed init script — the DDL lives here and self-heals on first use):

    CREATE TABLE IF NOT EXISTS biotope_landcover_map (
        id               SERIAL PRIMARY KEY,
        worldcover_class INTEGER NOT NULL UNIQUE,
        biotope_id       INTEGER NOT NULL REFERENCES biotopes(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS pam_calculation_log (
        id                 SERIAL PRIMARY KEY,
        source_name        VARCHAR(100) NOT NULL UNIQUE,
        last_count         INTEGER NOT NULL DEFAULT 0,
        last_calculated_at TIMESTAMPTZ,
        status             VARCHAR(20) NOT NULL DEFAULT 'idle',
        started_at         TIMESTAMPTZ,
        error_message      TEXT
    );

The class → biotope mapping itself is managed directly in the DB
(``biotope_landcover_map``), not in the admin UI.

ASYNC
=====
``start_async_assign`` launches a daemon ``threading.Thread`` and returns
immediately; the admin page polls ``get_autoassign_status()``. Status lives in
``pam_calculation_log`` under ``source_name = 'biotope_autoassign'`` (a generic
keyed log, reusable by future PAM background jobs). Swappable for Celery later.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from flask import current_app
from sqlalchemy import text

from .utils import get_pam_engine


# ── Constants ──────────────────────────────────────────────────────────────

AUTOASSIGN_SOURCE = 'biotope_autoassign'
AUTOASSIGN_STUCK_MINUTES = 30

WORLDCOVER_ASSET = 'ESA/WorldCover/v200/2021'
WORLDCOVER_BAND = 'Map'
WORLDCOVER_SCALE = 10

DEFAULT_RADIUS_M = 100
DEFAULT_TOP_N = 3

#: ESA WorldCover class codes → bilingual labels.
WORLDCOVER_CLASSES: dict[int, tuple[str, str]] = {
    10:  ('Дерева (ліс)', 'Tree cover'),
    20:  ('Чагарники', 'Shrubland'),
    30:  ('Трав’яниста рослинність', 'Grassland'),
    40:  ('Рілля', 'Cropland'),
    50:  ('Забудова', 'Built-up'),
    60:  ('Оголений / розріджений ґрунт', 'Bare / sparse vegetation'),
    70:  ('Сніг та лід', 'Snow and ice'),
    80:  ('Постійні водойми', 'Permanent water bodies'),
    90:  ('Трав’янисте водно-болотне угіддя', 'Herbaceous wetland'),
    95:  ('Мангри', 'Mangroves'),
    100: ('Мохи та лишайники', 'Moss and lichen'),
}

#: General biotopes to create for landcover classes that have no good match among
#: pam_db's existing (mostly specific) biotopes: a generic "forest" (class 10),
#: bare ground (class 60 — deserts / sand / destroyed grass cover, NOT cliffs),
#: and a broad wetland (class 90 — reeds are only one kind of herbaceous wetland).
#: Everything else maps to pre-existing PAM biotopes.
DEFAULT_LANDCOVER_BIOTOPES: list[tuple[str, str]] = [
    ('Ліс', 'Forest'),
    ('Оголений ґрунт', 'Bare / sparse vegetation'),
    ('Водно-болотне угіддя', 'Wetland'),
]

#: Reference seed mapping (ESA WorldCover class → biotope name_ua) for pam_db.
#: PAM's biotope set is rich, so most classes map to existing biotopes; only the
#: three general biotopes above are added. Snow/mangroves/moss omitted as
#: irrelevant for Ukraine. Documentation for reproducing the DB seed — the live
#: mapping is in biotope_landcover_map. NB "C/г поля" begins with a Latin 'C'.
DEFAULT_SEED_BY_NAME_UA: dict[int, str] = {
    10: 'Ліс',
    20: 'Кущі',
    30: 'Лука',
    40: 'C/г поля',
    50: 'Населені пункти',
    60: 'Оголений ґрунт',
    80: 'Озера та водосховища',
    90: 'Водно-болотне угіддя',
}


# ── Schema (idempotent, self-healing) ────────────────────────────────────────

def ensure_schema() -> None:
    """Create the two supporting tables in pam_db if they don't exist. Idempotent."""
    engine = get_pam_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS biotope_landcover_map (
                id               SERIAL PRIMARY KEY,
                worldcover_class INTEGER NOT NULL UNIQUE,
                biotope_id       INTEGER NOT NULL REFERENCES biotopes(id) ON DELETE CASCADE
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pam_calculation_log (
                id                 SERIAL PRIMARY KEY,
                source_name        VARCHAR(100) NOT NULL UNIQUE,
                last_count         INTEGER NOT NULL DEFAULT 0,
                last_calculated_at TIMESTAMPTZ,
                status             VARCHAR(20) NOT NULL DEFAULT 'idle',
                started_at         TIMESTAMPTZ,
                error_message      TEXT
            )
        """))


# ── GEE availability & initialisation (self-contained) ──────────────────────

_gee_initialized = False


def _resolve_gee_key_path() -> Optional[str]:
    """Path to the GEE service-account JSON, or None. Never raises."""
    path = None
    try:
        path = current_app.config.get('GEE_SERVICE_ACCOUNT_KEY')
    except Exception:
        path = None
    if not path:
        path = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')
    if path and os.path.exists(path):
        return path
    return None


def gee_landcover_available() -> bool:
    """Cheap gate for the admin button. Never raises, never hits the network.
    True only if earthengine-api imports AND a key file is configured and exists.
    """
    try:
        import ee  # noqa: F401
    except Exception:
        return False
    return _resolve_gee_key_path() is not None


def _initialize_gee() -> None:
    """Initialise Earth Engine via the service-account key. Singleton.

    Mirrors app/sdm/adapters/gee_backend.py: do NOT pass ``project=`` — GEE reads
    project_id from the key JSON. Raises RuntimeError if the key is missing.
    """
    global _gee_initialized
    if _gee_initialized:
        return
    import ee
    key_path = _resolve_gee_key_path()
    if not key_path:
        raise RuntimeError(
            'GEE_SERVICE_ACCOUNT_KEY is not configured or the file does not '
            'exist — landcover-based biotope assignment is unavailable.'
        )
    credentials = ee.ServiceAccountCredentials(None, key_file=key_path)
    ee.Initialize(credentials=credentials)
    _gee_initialized = True


# ── Landcover sampling ──────────────────────────────────────────────────────

def get_landcover_histograms(
    points: list[tuple[int, float, float]],
    radius_m: int = DEFAULT_RADIUS_M,
    chunk_size: int = 200,
) -> dict[int, dict[int, float]]:
    """Sample the ESA WorldCover histogram in a radius around each point.

    Args:
        points: ``[(location_id, lat, lon), ...]``.
        radius_m: buffer radius in metres.
        chunk_size: locations per GEE ``reduceRegions`` request.

    Returns:
        ``{location_id: {class_code: pixel_count}}`` (empty dict if no coverage).

    Side effects:
        Initialises GEE on first call. Raises on GEE/auth failure.
    """
    import ee

    _initialize_gee()
    img = ee.Image(WORLDCOVER_ASSET).select(WORLDCOVER_BAND)

    results: dict[int, dict[int, float]] = {}
    for i in range(0, len(points), chunk_size):
        chunk = points[i:i + chunk_size]
        fc = ee.FeatureCollection([
            ee.Feature(
                ee.Geometry.Point([float(lon), float(lat)]).buffer(radius_m),
                {'loc_id': int(loc_id)},
            )
            for (loc_id, lat, lon) in chunk
        ])
        stats = img.reduceRegions(
            collection=fc,
            reducer=ee.Reducer.frequencyHistogram(),
            scale=WORLDCOVER_SCALE,
            crs='EPSG:4326',
        ).getInfo()

        for feat in stats.get('features', []):
            props = feat.get('properties', {})
            loc_id = props.get('loc_id')
            if loc_id is None:
                continue
            hist = props.get('histogram') or {}
            results[int(loc_id)] = {int(cls): float(cnt) for cls, cnt in hist.items()}
    return results


# ── Mapping helpers ──────────────────────────────────────────────────────────

def get_biotope_mapping() -> dict[int, int]:
    """Return ``{worldcover_class: biotope_id}`` from biotope_landcover_map."""
    engine = get_pam_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT worldcover_class, biotope_id FROM biotope_landcover_map')
        ).all()
    return {int(r.worldcover_class): int(r.biotope_id) for r in rows}


def set_biotope_mapping(worldcover_class: int, biotope_id: Optional[int]) -> None:
    """Upsert (or clear, when biotope_id is None) one class→biotope mapping."""
    engine = get_pam_engine()
    with engine.begin() as conn:
        if biotope_id is None:
            conn.execute(
                text('DELETE FROM biotope_landcover_map WHERE worldcover_class = :c'),
                {'c': int(worldcover_class)},
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO biotope_landcover_map (worldcover_class, biotope_id)
                    VALUES (:c, :b)
                    ON CONFLICT (worldcover_class)
                    DO UPDATE SET biotope_id = EXCLUDED.biotope_id
                """),
                {'c': int(worldcover_class), 'b': int(biotope_id)},
            )


# ── Core assignment ───────────────────────────────────────────────────────────

def select_biotopes_from_histogram(
    histogram: dict[int, float],
    mapping: dict[int, int],
    top_n: int,
) -> list[int]:
    """Pick up to ``top_n`` biotope ids from a landcover histogram.

    Classes ordered by pixel count (desc); each translated to a biotope via
    ``mapping``. Unmapped classes (noise) are skipped, biotopes de-duplicated,
    at most ``top_n`` returned — so ``top_n`` counts biotopes, not raw classes.
    """
    ordered = sorted(histogram.items(), key=lambda kv: kv[1], reverse=True)
    biotope_ids: list[int] = []
    for cls, _cnt in ordered:
        bid = mapping.get(cls)
        if bid is not None and bid not in biotope_ids:
            biotope_ids.append(bid)
        if len(biotope_ids) >= top_n:
            break
    return biotope_ids


def assign_biotopes(
    radius_m: int = DEFAULT_RADIUS_M,
    top_n: int = DEFAULT_TOP_N,
    only_missing_locations: bool = False,
) -> dict:
    """Assign biotopes to PAM points from landcover. Additive — never removes.

    Args:
        radius_m: sampling radius around each point.
        top_n: how many biotopes (from the most-abundant mapped classes) to
            assign per point. Unmapped noise classes are skipped.
        only_missing_locations: when True, process only points with no biotopes.

    Returns:
        Summary dict. Raises GEE errors for the async wrapper to record.
    """
    ensure_schema()
    mapping = get_biotope_mapping()
    if not mapping:
        return {
            'locations_processed': 0, 'locations_updated': 0, 'links_added': 0,
            'locations_no_data': 0,
            'note': 'Не налаштовано жодної відповідності клас лендковеру → біотоп '
                    '(таблиця biotope_landcover_map порожня).',
        }

    engine = get_pam_engine()
    with engine.connect() as conn:
        if only_missing_locations:
            loc_rows = conn.execute(text("""
                SELECT l.location_id, l.lat, l.lon
                  FROM locations l
                 WHERE l.lat IS NOT NULL AND l.lon IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM location_biotopes lb
                        WHERE lb.location_id = l.location_id
                   )
            """)).all()
        else:
            loc_rows = conn.execute(text("""
                SELECT location_id, lat, lon
                  FROM locations
                 WHERE lat IS NOT NULL AND lon IS NOT NULL
            """)).all()

    points = [(int(r.location_id), float(r.lat), float(r.lon)) for r in loc_rows]
    if not points:
        return {
            'locations_processed': 0, 'locations_updated': 0, 'links_added': 0,
            'locations_no_data': 0, 'note': 'Немає точок для обробки.',
        }

    histograms = get_landcover_histograms(points, radius_m=radius_m)

    locations_updated = 0
    links_added = 0
    locations_no_data = 0

    engine = get_pam_engine()
    with engine.begin() as conn:
        for loc_id, _lat, _lon in points:
            hist = histograms.get(loc_id) or {}
            if not hist:
                locations_no_data += 1
                continue

            biotope_ids = select_biotopes_from_histogram(hist, mapping, top_n)
            if not biotope_ids:
                locations_no_data += 1
                continue

            res = conn.execute(
                text("""
                    INSERT INTO location_biotopes (location_id, biotope_id)
                    SELECT :loc, b FROM unnest(CAST(:bids AS integer[])) AS b
                    ON CONFLICT (location_id, biotope_id) DO NOTHING
                """),
                {'loc': loc_id, 'bids': biotope_ids},
            )
            added = res.rowcount or 0
            if added > 0:
                locations_updated += 1
                links_added += added

    return {
        'locations_processed': len(points),
        'locations_updated': locations_updated,
        'links_added': links_added,
        'locations_no_data': locations_no_data,
        'note': (f'Оброблено {len(points)} точок: оновлено {locations_updated}, '
                 f'додано {links_added} звʼязків, без даних лендковеру '
                 f'{locations_no_data}.'),
    }


# ── Status row (pam_calculation_log) ──────────────────────────────────────────

def _ensure_log_row(conn) -> None:
    conn.execute(
        text("""
            INSERT INTO pam_calculation_log (source_name, last_count, status)
            VALUES (:src, 0, 'idle')
            ON CONFLICT (source_name) DO NOTHING
        """),
        {'src': AUTOASSIGN_SOURCE},
    )


def try_start_autoassign_run() -> bool:
    """Atomic compare-and-set to claim the run across gunicorn workers.

    Returns True if this call claimed the run; False if one is already running.
    The stuck-cutoff is computed in SQL to avoid tz-naive/aware mismatches with
    the TIMESTAMPTZ column.
    """
    engine = get_pam_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO pam_calculation_log
                    (source_name, last_count, status, started_at, error_message)
                VALUES
                    (:src, 0, 'running', NOW(), NULL)
                ON CONFLICT (source_name) DO UPDATE
                   SET status = 'running',
                       started_at = NOW(),
                       error_message = NULL
                 WHERE pam_calculation_log.status IS DISTINCT FROM 'running'
                    OR pam_calculation_log.started_at IS NULL
                    OR pam_calculation_log.started_at
                        < NOW() - make_interval(mins => :mins)
            """),
            {'src': AUTOASSIGN_SOURCE, 'mins': AUTOASSIGN_STUCK_MINUTES},
        )
        return (result.rowcount or 0) == 1


def _finish_autoassign_run(status: str, error_message: Optional[str] = None,
                           last_count: Optional[int] = None) -> None:
    engine = get_pam_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pam_calculation_log
                   SET status = :st,
                       error_message = :err,
                       last_count = COALESCE(:cnt, last_count),
                       last_calculated_at = CASE
                           WHEN :st = 'completed' THEN NOW()
                           ELSE last_calculated_at
                       END
                 WHERE source_name = :src
            """),
            {'src': AUTOASSIGN_SOURCE, 'st': status,
             'err': (error_message[:500] if error_message else None),
             'cnt': last_count},
        )


def get_autoassign_status() -> dict:
    """Current state for admin-page polling."""
    ensure_schema()
    engine = get_pam_engine()
    with engine.begin() as conn:
        _ensure_log_row(conn)
        row = conn.execute(
            text("""
                SELECT status, started_at, last_calculated_at, last_count, error_message
                  FROM pam_calculation_log
                 WHERE source_name = :src
            """),
            {'src': AUTOASSIGN_SOURCE},
        ).first()

    if row is None:
        return {'status': 'idle', 'started_at': None, 'last_calculated_at': None,
                'last_count': None, 'error_message': None}
    return {
        'status': row.status or 'idle',
        'started_at': row.started_at.isoformat() if row.started_at else None,
        'last_calculated_at': row.last_calculated_at.isoformat() if row.last_calculated_at else None,
        'last_count': row.last_count,
        'error_message': row.error_message,
    }


# ── Async wrapper ─────────────────────────────────────────────────────────────

def _run_autoassign_in_thread(app, radius_m: int, top_n: int,
                              only_missing_locations: bool) -> None:
    """Background thread body. Runs outside the HTTP context."""
    with app.app_context():
        try:
            summary = assign_biotopes(
                radius_m=radius_m,
                top_n=top_n,
                only_missing_locations=only_missing_locations,
            )
            _finish_autoassign_run(
                'completed',
                error_message=summary.get('note'),
                last_count=summary.get('links_added'),
            )
            current_app.logger.info(f'[pam-biotope-autoassign] done: {summary}')
        except Exception as e:
            current_app.logger.exception('[pam-biotope-autoassign] background run crashed')
            try:
                _finish_autoassign_run('failed', str(e))
            except Exception:
                pass


def start_async_assign(radius_m: int = DEFAULT_RADIUS_M,
                       top_n: int = DEFAULT_TOP_N,
                       only_missing_locations: bool = False) -> bool:
    """Start a background biotope auto-assignment. Returns IMMEDIATELY.

    Returns True if a run was started, False if one is already in progress.
    """
    ensure_schema()
    if not try_start_autoassign_run():
        return False

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_autoassign_in_thread,
        args=(app, radius_m, top_n, only_missing_locations),
        name='pam-biotope-autoassign',
        daemon=True,
    ).start()
    current_app.logger.info(
        f'[pam-biotope-autoassign] started (radius={radius_m}m, top_n={top_n}, '
        f'only_missing={only_missing_locations})'
    )
    return True
