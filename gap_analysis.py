# SPDX-License-Identifier: AGPL-3.0-only
"""Data layer of the PAM gap analysis page.

The acoustic twin of the camera-trap page (shared-ct gap_analysis.py): the
same three dimensions (effort, completeness, time) on the same EEA grid, the
same estimators, but fed from pam_db.

* ``fetch_gap_data`` reads pam_db once and returns a ``GapData``: locations,
  recording minutes per location-day and per clock hour, and for every
  (location, day, species) the best classifier score of that day.
* ``apply_threshold`` turns the scores into detection days for one confidence
  threshold. Scores are fetched down to MIN_THRESHOLD, so moving the slider
  re-filters in memory instead of scanning 26M detections again.
* ``build_grid`` / ``build_kernel`` / ``biotope_summary`` / ``cell_detail``
  are pure and turn that into map layers using gap_metrics.py.

What makes the final dataset
----------------------------
In camera traps a record is a verified series; here it is a classifier score
above a threshold, so the threshold is the main filter of the page. Human
verification refines it where it exists: a detection rejected by consensus
never counts, whatever its score, and one confirmed by consensus always counts,
whatever the threshold. On the real data (Oct 2026) verification touched about
8.7k detections out of 26M, so the threshold decides almost everything.

What counts as effort
---------------------
A recorder-day is a local calendar day (Europe/Kyiv) with at least one
recording at the location; its weight in the effort layers is the sum of
recording durations. A day with 5 minutes of recording counts as one unit, as
does a day with 12 hours: on the real data the 10th percentile was 205 minutes
per location-day, so this rarely matters.
"""
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from sqlalchemy import text

from . import gap_metrics as gm

# Local day and clock hour of a recording. Night recordings straddle midnight;
# with the default week unit that hardly matters.
LOCAL_TZ = 'Europe/Kyiv'

# Confidence slider range on the page. Scores are fetched down to the minimum.
MIN_THRESHOLD = 0.70
MAX_THRESHOLD = 1.00
DEFAULT_THRESHOLD = 0.95

# Scores are stored as REAL; 0.95 comes back as 0.9499999881. Rounding to the
# classifier's own precision (BirdNET writes 4 decimals) keeps a threshold of
# 0.95 inclusive of a score of 0.95.
SCORE_DECIMALS = 4

TAXON_GROUPS = {
    'birds': ('Aves',),
    'amphibians': ('Amphibia',),
    'bats': ('Mammalia',),
    'all': ('Aves', 'Amphibia', 'Mammalia'),
}
DEFAULT_TAXON = 'birds'

STUDY_AREA_BUFFER_M = 5000
STUDY_AREA_CLOSING_M = 10000
MAX_EMPTY_CELLS = 20000

UNIT_DAY = 'day'
UNIT_WEEK = 'week'
# Default unit: the recorder-week. Detections of one species at one recorder
# are strongly autocorrelated day to day (real data, Oct 2026, threshold 0.95:
# P(detected tomorrow | detected today) = 0.56 against a mean daily p of 0.11;
# territorial birds sing from the same spot every morning), which breaks the
# independence both Chao2 and 1 - (1 - p)^T assume. Weeks soften it.
DEFAULT_UNIT = UNIT_WEEK

# Days per unit, for the detection side (see the camera-trap twin): p is a rate
# per full-unit equivalent and exposure in a cell is recorder-days / L, so
# scattered days are not counted as full weeks.
UNIT_LENGTH_DAYS = {UNIT_DAY: 1, UNIT_WEEK: 7}

MIN_POOL_UNITS = 3
POOL_RADIUS_M = 50000


@dataclass
class GapData:
    locations: dict = field(default_factory=dict)       # id -> {name, lat, lon, x, y}
    effort_days: dict = field(default_factory=dict)     # id -> set[date]
    effort_minutes: dict = field(default_factory=dict)  # id -> {date: minutes}
    hour_minutes: dict = field(default_factory=dict)    # id -> [24 minutes]
    scores: dict = field(default_factory=dict)          # id -> {species_id: {date: best score}}
    confirmed: dict = field(default_factory=dict)       # id -> {species_id: set[date]}
    detections: dict = field(default_factory=dict)      # id -> {species_id: set[date]}, after threshold
    species: dict = field(default_factory=dict)         # id -> {name_uk, name_en, scientific}
    biotopes: dict = field(default_factory=dict)        # location id -> [biotope_id]
    biotope_names: dict = field(default_factory=dict)   # biotope id -> (name_uk, name_en)
    threshold: float = None


# ─── Reading pam_db ───────────────────────────────────────────────────────────

def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def clamp_threshold(value):
    """Threshold from the page, clamped to the slider range and rounded to 0.01."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    if value != value:   # NaN
        return DEFAULT_THRESHOLD
    return round(min(MAX_THRESHOLD, max(MIN_THRESHOLD, value)), 2)


def fetch_gap_data(conn, inst_condition, inst_params, start_date=None, end_date=None,
                   taxon=DEFAULT_TAXON, species_condition='TRUE', species_params=None,
                   score_sql='d.confidence', score_filter_sql='d.confidence >= :confidence'):
    """Read everything the gap analysis needs in five queries.

    Args:
        conn: pam_db connection.
        inst_condition / inst_params: output of utils.get_institution_filter
            (alias ``l``: access rights plus the selected institution).
        start_date, end_date: optional ``date`` bounds, inclusive, local days.
        taxon: key of TAXON_GROUPS.
        species_condition / species_params: species visibility by role
            (alias ``s``); sensitive species are hidden the same way as on
            every other page of the module.
        score_sql / score_filter_sql: the active model's score expression and
            its ``>= :confidence`` predicate (utils._confidence_value_sql /
            _confidence_filter_sql), both on alias ``d``.
    """
    params = dict(inst_params or {})
    params.update(species_params or {})
    params.update(tz=LOCAL_TZ, confidence=MIN_THRESHOLD)
    local = "(r.datetime_start AT TIME ZONE :tz)"
    date_clause = ''
    if start_date and end_date:
        date_clause = f'AND {local}::date BETWEEN :start_date AND :end_date'
        params.update(start_date=start_date, end_date=end_date)
    loc_where = f"({inst_condition})"
    rec_where = f"{loc_where} AND r.datetime_start IS NOT NULL {date_clause}"

    data = GapData()

    # 1. Locations in scope.
    rows = conn.execute(text(f"""
        SELECT l.location_id, l.location_name, l.location_name_en, l.lat, l.lon
        FROM locations l
        WHERE {loc_where}
    """), params).fetchall()
    for lid, name, name_en, lat, lon in rows:
        if lat is None or lon is None:
            continue
        x, y = gm.to_laea(lon, lat)
        data.locations[lid] = {'name': name, 'name_en': name_en or name,
                               'lat': float(lat), 'lon': float(lon), 'x': x, 'y': y}
    if not data.locations:
        return data

    # 2. Recording minutes per location-day.
    rows = conn.execute(text(f"""
        SELECT r.location_id, {local}::date AS d, SUM(r.duration_minutes) AS minutes
        FROM recordings r
        JOIN locations l ON l.location_id = r.location_id
        WHERE {rec_where}
        GROUP BY 1, 2
    """), params).fetchall()
    for lid, d, minutes in rows:
        d = _as_date(d)
        if d is None or lid not in data.locations:
            continue
        data.effort_minutes.setdefault(lid, {})[d] = float(minutes or 0)
        data.effort_days.setdefault(lid, set()).add(d)

    # 3. Recording minutes per clock hour, for the diel layer.
    rows = conn.execute(text(f"""
        SELECT r.location_id, EXTRACT(HOUR FROM {local})::int AS h,
               SUM(r.duration_minutes) AS minutes
        FROM recordings r
        JOIN locations l ON l.location_id = r.location_id
        WHERE {rec_where}
        GROUP BY 1, 2
    """), params).fetchall()
    for lid, h, minutes in rows:
        if lid in data.locations and h is not None:
            data.hour_minutes.setdefault(lid, [0.0] * 24)[int(h)] += float(minutes or 0)

    # 4. Best score per (location, day, species), down to MIN_THRESHOLD.
    # Rejected detections never count; confirmed ones count at any threshold.
    params['classes'] = list(TAXON_GROUPS.get(taxon, TAXON_GROUPS[DEFAULT_TAXON]))
    rows = conn.execute(text(f"""
        SELECT r.location_id, {local}::date AS d, d.species_id,
               ROUND((MAX({score_sql}) FILTER (
                   WHERE dvm.verification_result IS DISTINCT FROM 0))::numeric, {SCORE_DECIMALS}) AS score,
               COALESCE(BOOL_OR(dvm.verification_result = 1), FALSE) AS confirmed
        FROM detections d
        JOIN recordings r ON r.recording_id = d.recording_id
        JOIN locations l ON l.location_id = r.location_id
        JOIN species s ON s.species_id = d.species_id
        LEFT JOIN detection_verification_map dvm ON dvm.detection_id = d.detection_id
        WHERE ({score_filter_sql} OR dvm.verification_result = 1)
          AND {rec_where}
          AND s.class = ANY(:classes)
          AND ({species_condition})
        GROUP BY 1, 2, 3
    """), params).fetchall()
    for lid, d, sid, score, confirmed in rows:
        d = _as_date(d)
        if d is None or lid not in data.locations:
            continue
        if score is not None:
            data.scores.setdefault(lid, {}).setdefault(sid, {})[d] = float(score)
        if confirmed:
            data.confirmed.setdefault(lid, {}).setdefault(sid, set()).add(d)

    species_ids = sorted({sid for per in data.scores.values() for sid in per}
                         | {sid for per in data.confirmed.values() for sid in per})
    if species_ids:
        rows = conn.execute(text("""
            SELECT species_id, common_name_uk, common_name_en, scientific_name
            FROM species WHERE species_id = ANY(:ids)
        """), {'ids': species_ids}).fetchall()
        for sid, name_uk, name_en, sci in rows:
            data.species[sid] = {'name_uk': name_uk or sci, 'name_en': name_en or sci,
                                 'scientific': sci}

    # 5. Biotopes, for the environmental summary.
    rows = conn.execute(text(f"""
        SELECT lb.location_id, b.id, b.name_ua, b.name_en
        FROM location_biotopes lb
        JOIN biotopes b ON b.id = lb.biotope_id
        JOIN locations l ON l.location_id = lb.location_id
        WHERE {loc_where}
    """), params).fetchall()
    for lid, bid, name_uk, name_en in rows:
        data.biotopes.setdefault(lid, []).append(bid)
        data.biotope_names[bid] = (name_uk, name_en)

    return data


def apply_threshold(data, threshold):
    """A view of ``data`` whose detections are the days scoring >= threshold.

    Confirmed days are added whatever their score; rejected ones were already
    left out of the scores. The view shares everything else with ``data``.
    """
    detections = {}
    for lid in set(data.scores) | set(data.confirmed):
        per = {}
        for sid, by_day in data.scores.get(lid, {}).items():
            days = {d for d, sc in by_day.items() if sc >= threshold}
            if days:
                per[sid] = days
        for sid, days in data.confirmed.get(lid, {}).items():
            per.setdefault(sid, set()).update(days)
        if per:
            detections[lid] = per
    return replace(data, detections=detections, threshold=threshold)


# ─── Sampling units ───────────────────────────────────────────────────────────

def unit_key(d, unit):
    """Bucket of a date: the day itself, or a fixed 7-day calendar block."""
    return d.toordinal() if unit == UNIT_DAY else d.toordinal() // 7


def _species_label(info, lang):
    if not info:
        return ''
    return info['name_en'] if lang == 'en' else info['name_uk']


def _location_label(loc, lang):
    return loc['name_en'] if lang == 'en' else loc['name']


def species_detection_probabilities(data, unit, min_units=1):
    """{species_id: detection probability per full unit} over the whole scope.

    Hits are units with a detection; exposure is recorder-days / unit length
    at the locations where the species was detected. Species detected in
    fewer than ``min_units`` units are left out.
    """
    length = UNIT_LENGTH_DAYS[unit]
    hits = defaultdict(int)
    exposure = defaultdict(float)
    for lid, per_species in data.detections.items():
        n_units = len(data.effort_days.get(lid, ())) / length
        for sid, days in per_species.items():
            hits[sid] += len({unit_key(d, unit) for d in days})
            exposure[sid] += n_units
    return {sid: gm.per_unit_detection_probability(hits[sid], exposure[sid])
            for sid in hits if hits[sid] >= min_units}


def local_pool(data, pool, x, y, radius_m=None):
    """Subset of ``pool`` detected at some location within ``radius_m`` of (x, y)."""
    radius_m = POOL_RADIUS_M if radius_m is None else radius_m
    r2 = radius_m * radius_m
    out = {}
    for lid, per_species in data.detections.items():
        loc = data.locations.get(lid)
        if loc is None or (loc['x'] - x) ** 2 + (loc['y'] - y) ** 2 > r2:
            continue
        for sid in per_species:
            if sid in pool:
                out[sid] = pool[sid]
    return out


def _pool(data, location_ids, unit):
    """Pool several locations into one sample: units, recorder-days, minutes, incidences."""
    units = set()
    recorder_days = []
    minutes = 0.0
    hours = [0.0] * 24
    incidence = defaultdict(set)
    for lid in location_ids:
        days = data.effort_days.get(lid, set())
        recorder_days.extend(days)
        minutes += sum(data.effort_minutes.get(lid, {}).values())
        for h, m in enumerate(data.hour_minutes.get(lid, ())):
            hours[h] += m
        units.update((lid, unit_key(d, unit)) for d in days)
        for sid, ddays in data.detections.get(lid, {}).items():
            incidence[sid].update((lid, unit_key(d, unit)) for d in ddays)
    return units, recorder_days, minutes, hours, {sid: len(u) for sid, u in incidence.items()}


def _sample_metrics(data, location_ids, unit, res_m=None, pool=None):
    units, recorder_days, minutes, hours, incidence = _pool(data, location_ids, unit)
    T = len(units)
    rd = len(recorder_days)
    exposure = rd / UNIT_LENGTH_DAYS[unit]
    rich = gm.richness_stats(incidence.values(), T)
    temporal = gm.temporal_coverage(recorder_days)
    diel = gm.diel_coverage(hours)
    rd_per_unit = rd / T if T else 0
    extra_units = gm.units_to_coverage(list(incidence.values()), T, 0.95)
    sensitivity = gm.pool_sensitivity(pool or {}, exposure, incidence.keys())
    rec_hours = minutes / 60
    out = {
        'n_locations': len(location_ids),
        'recorder_days': rd,
        'hours': round(rec_hours, 1),
        'T': T,
        'exposure_units': round(exposure, 2),
        'S_obs': rich['S_obs'],
        'Q1': rich['Q1'],
        'Q2': rich['Q2'],
        'chao2': _r(rich['chao2'], 2),
        'coverage': _r(rich['coverage'], 3),
        'completeness': _r(rich['completeness'], 3),
        'estimable': rich['estimable'],
        'unstable': rich['unstable'],
        'extra_recorder_days_cov95': (None if extra_units is None
                                      else int(round(extra_units * rd_per_unit))),
        'months': temporal['months'],
        'months_covered': temporal['months_covered'],
        'seasons_covered': temporal['seasons_covered'],
        'year_first': temporal['years'][0] if temporal['years'] else None,
        'year_last': temporal['years'][-1] if temporal['years'] else None,
        'n_years': len(temporal['years']),
        'temporal_score': _r(temporal['score'], 3),
        'diel_hours': diel['hours_covered'],
        'diel_night_hours': diel['night_covered'],
        'diel_score': _r(diel['score'], 3),
        'pool_sensitivity': _r(sensitivity, 3),
    }
    if res_m:
        es = gm.effort_score(rec_hours, res_m)
        out['effort_score'] = _r(es, 3)
        out['composite'] = _r(gm.composite_score(es, sensitivity, temporal['score']), 3)
    return out, incidence


def _r(value, ndigits):
    return None if value is None else round(value, ndigits)


# ─── Map layers ───────────────────────────────────────────────────────────────

def study_area(data):
    """Buffered and closed hull of the locations, a shapely geometry in EPSG:3035."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    pts = [Point(loc['x'], loc['y']).buffer(STUDY_AREA_BUFFER_M, 16)
           for loc in data.locations.values()]
    if not pts:
        return None
    area = unary_union(pts)
    return area.buffer(STUDY_AREA_CLOSING_M, 8).buffer(-STUDY_AREA_CLOSING_M, 8)


def empty_cells(area, occupied, res_m, limit=MAX_EMPTY_CELLS):
    """Grid cells intersecting the study area that hold no effort.

    Returns (cells, truncated). Enumeration is per connected part of the area,
    so scattered reserves do not drag in the whole country's bounding box.
    """
    from shapely.geometry import box
    from shapely.prepared import prep
    if area is None or area.is_empty:
        return [], False
    parts = list(getattr(area, 'geoms', [area]))
    out = []
    seen = set(occupied)
    for part in parts:
        p = prep(part)
        minx, miny, maxx, maxy = part.bounds
        x = (minx // res_m) * res_m
        while x < maxx:
            y = (miny // res_m) * res_m
            while y < maxy:
                key = (int(x), int(y))
                if key not in seen and p.intersects(box(x, y, x + res_m, y + res_m)):
                    seen.add(key)
                    out.append(key)
                    if len(out) >= limit:
                        return out, True
                y += res_m
            x += res_m
    return out, False


def build_grid(data, res_m, unit=DEFAULT_UNIT, focus_species_id=None, lang='uk',
               include_empty=True):
    """Grid layer: one feature per cell with recordings, plus empty cells of the study area.

    ``data`` must already carry detections (``apply_threshold``). Returns a
    dict with ``cells``, ``incidence`` (cell code → {species_id: incidence},
    kept server-side for the cell detail) and ``summary``.
    """
    by_cell = defaultdict(list)
    for lid, loc in data.locations.items():
        if lid not in data.effort_days:
            continue
        by_cell[gm.cell_origin(loc['x'], loc['y'], res_m)].append(lid)

    p_by_species = species_detection_probabilities(data, unit)
    focus_p = p_by_species.get(focus_species_id) if focus_species_id else None
    pool = species_detection_probabilities(data, unit, MIN_POOL_UNITS)

    cells = []
    incidence_by_code = {}
    for (x0, y0), lids in by_cell.items():
        code = gm.eea_cell_code(x0, y0, res_m)
        cell_pool = local_pool(data, pool, x0 + res_m / 2, y0 + res_m / 2)
        metrics, incidence = _sample_metrics(data, lids, unit, res_m, cell_pool)
        incidence_by_code[code] = {'incidence': incidence, 'T': metrics['T'],
                                   'recorder_days': metrics['recorder_days'],
                                   'hours': metrics['hours'],
                                   'location_ids': sorted(lids)}
        top = sorted(incidence.items(), key=lambda kv: -kv[1])
        metrics.update(
            code=code, status='data',
            ring=gm.cell_polygon_wgs84(x0, y0, res_m),
            species=[_species_label(data.species.get(sid), lang) for sid, _ in top],
            likely_missed=likely_missed(data, cell_pool, incidence, metrics['exposure_units'], lang),
            pool_size=len(cell_pool),
        )
        if focus_species_id:
            detected = focus_species_id in incidence
            metrics['focus_detected'] = detected
            metrics['focus_confidence'] = 1.0 if detected else _r(
                gm.detection_confidence(focus_p, metrics['exposure_units']), 3)
        cells.append(metrics)

    truncated = False
    if include_empty:
        area = study_area(data)
        empties, truncated = empty_cells(area, set(by_cell), res_m)
        for x0, y0 in empties:
            cells.append({'code': gm.eea_cell_code(x0, y0, res_m), 'status': 'empty',
                          'ring': gm.cell_polygon_wgs84(x0, y0, res_m),
                          'recorder_days': 0, 'hours': 0, 'composite': 0, 'effort_score': 0})

    all_lids = [lid for lids in by_cell.values() for lid in lids]
    total, _ = _sample_metrics(data, all_lids, unit, pool=pool)
    unit_days = UNIT_LENGTH_DAYS[unit]

    def _detectability(sid, p):
        need = gm.units_for_confidence(p, 0.95)
        return {'species_id': sid,
                'name': _species_label(data.species.get(sid), lang),
                'p_unit': _r(p, 4),
                'recorder_days_for_95': None if need is None else int(round(need * unit_days))}

    summary = {
        'scale_m': res_m, 'unit': unit, 'threshold': data.threshold,
        'n_cells_with_data': len(by_cell),
        'n_empty_cells': sum(1 for c in cells if c['status'] == 'empty'),
        'empty_truncated': truncated,
        'total': total,
        'threshold_effect': threshold_effect(data),
        'focus': _detectability(focus_species_id, focus_p) if focus_species_id else None,
        # Hardest to detect first: this is the list that sets how much effort
        # a cell needs before its silence means anything.
        'pool': [_detectability(sid, p) for sid, p in sorted(pool.items(), key=lambda kv: kv[1])],
        'species_options': species_options(data, lang),
    }
    return {'cells': cells, 'incidence': incidence_by_code, 'summary': summary}


def threshold_effect(data):
    """Species and detection-days kept at the current threshold vs the slider minimum.

    The slider trades false positives against missed species, and both ends
    move the completeness estimates: a low threshold adds spurious singletons
    (one-off misidentifications), which inflate Chao2 and depress coverage.
    """
    kept_species = set()
    kept_days = 0
    for per in data.detections.values():
        kept_species.update(per)
        kept_days += sum(len(days) for days in per.values())
    floor_species = set()
    floor_days = 0
    for lid in set(data.scores) | set(data.confirmed):
        per = data.scores.get(lid, {})
        conf = data.confirmed.get(lid, {})
        for sid in set(per) | set(conf):
            days = set(per.get(sid, {})) | conf.get(sid, set())
            floor_species.add(sid)
            floor_days += len(days)
    return {'species': len(kept_species), 'detection_days': kept_days,
            'species_at_min': len(floor_species), 'detection_days_at_min': floor_days,
            'min_threshold': MIN_THRESHOLD}


def likely_missed(data, pool, incidence, exposure, lang='uk', limit=3):
    """Pool species not detected here, least likely to have been detected first."""
    rows = [(gm.detection_confidence(p, exposure), sid) for sid, p in pool.items() if sid not in incidence]
    rows.sort()
    return [{'name': _species_label(data.species.get(sid), lang), 'confidence': round(c, 3)}
            for c, sid in rows[:limit]]


def species_options(data, lang='uk'):
    """Species detected in scope, most detection-days first, for the focus picker."""
    counts = defaultdict(int)
    for per_species in data.detections.values():
        for sid, days in per_species.items():
            counts[sid] += len(days)
    return [{'id': sid, 'name': _species_label(data.species.get(sid), lang),
             'scientific': data.species.get(sid, {}).get('scientific'),
             'detection_days': n}
            for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def build_kernel(data, bandwidth_m):
    """Kernel effort surface as pixel features (ring + recording hours per km²)."""
    points = [(loc['x'], loc['y'], sum(data.effort_minutes.get(lid, {}).values()) / 60)
              for lid, loc in data.locations.items()]
    pixel, surface = gm.kernel_effort_surface(points, bandwidth_m)
    home_range = round(gm.home_range_km2(bandwidth_m), 1)
    if not surface:
        return {'pixel_m': pixel, 'bandwidth_m': bandwidth_m, 'pixels': [], 'max': 0,
                'home_range_km2': home_range}
    peak = max(surface.values())
    floor = peak * 1e-3   # drop the far tails, they only cost bytes
    pixels = [{'ring': gm.cell_polygon_wgs84(x0, y0, pixel, ndigits=5), 'value': round(v, 3)}
              for (x0, y0), v in surface.items() if v >= floor]
    return {'pixel_m': pixel, 'bandwidth_m': bandwidth_m, 'pixels': pixels, 'max': round(peak, 3),
            'home_range_km2': home_range}


def location_points(data, lang='uk'):
    """Recorder locations with their effort and richness, for the point layer."""
    out = []
    for lid, loc in data.locations.items():
        days = data.effort_days.get(lid, set())
        if not days:
            continue
        diel = gm.diel_coverage(data.hour_minutes.get(lid, ()))
        out.append({'id': lid, 'name': _location_label(loc, lang), 'lat': loc['lat'], 'lon': loc['lon'],
                    'recorder_days': len(days),
                    'hours': round(sum(data.effort_minutes.get(lid, {}).values()) / 60, 1),
                    'diel_hours': diel['hours_covered'],
                    'S_obs': len(data.detections.get(lid, {})),
                    'first': min(days).isoformat(), 'last': max(days).isoformat()})
    return out


def biotope_summary(data, unit=DEFAULT_UNIT, lang='uk'):
    """Effort and completeness pooled by biotope (a location counts in each of its biotopes)."""
    members = defaultdict(list)
    for lid in data.effort_days:
        for bid in data.biotopes.get(lid, []) or [None]:
            members[bid].append(lid)
    rows = []
    pool = species_detection_probabilities(data, unit, MIN_POOL_UNITS)
    for bid, lids in members.items():
        metrics, _ = _sample_metrics(data, lids, unit, pool=pool)
        if bid is None:
            name = None
        else:
            uk, en = data.biotope_names.get(bid, ('?', '?'))
            name = en if lang == 'en' else uk
        metrics.update(biotope_id=bid, name=name)
        metrics.pop('months', None)
        rows.append(metrics)
    rows.sort(key=lambda r: -r['hours'])
    return rows


def cell_detail(grid_result, code, data, lang='uk'):
    """Accumulation curve, diel profile and species list for one cell of a built grid."""
    entry = grid_result['incidence'].get(code)
    if entry is None:
        return None
    incidence = entry['incidence']
    T = entry['T']
    ys = list(incidence.values())
    rich = gm.richness_stats(ys, T)
    rd_per_unit = entry['recorder_days'] / T if T else 0
    curve = [{'units': u, 'recorder_days': int(round(u * rd_per_unit)), 'S': s}
             for u, s in gm.accumulation_curve(ys, T)]
    species = [{'name': _species_label(data.species.get(sid), lang),
                'scientific': data.species.get(sid, {}).get('scientific'),
                'units': n}
               for sid, n in sorted(incidence.items(), key=lambda kv: -kv[1])]
    hours = [0.0] * 24
    for lid in entry['location_ids']:
        for h, m in enumerate(data.hour_minutes.get(lid, ())):
            hours[h] += m
    locations = [{'id': lid, 'name': _location_label(data.locations[lid], lang),
                  'recorder_days': len(data.effort_days.get(lid, ())),
                  'hours': round(sum(data.effort_minutes.get(lid, {}).values()) / 60, 1)}
                 for lid in entry['location_ids'] if lid in data.locations]
    return {'code': code, 'T': T, 'recorder_days': entry['recorder_days'],
            'hours': entry['hours'],
            'S_obs': rich['S_obs'], 'chao2': _r(rich['chao2'], 2),
            'coverage': _r(rich['coverage'], 3), 'estimable': rich['estimable'],
            'unstable': rich['unstable'],
            'diel_minutes': [round(m, 1) for m in hours],
            'curve': curve, 'species': species, 'locations': locations}
