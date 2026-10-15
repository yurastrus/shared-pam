# SPDX-License-Identifier: AGPL-3.0-only
"""Routes of the PAM gap analysis page (Beta).

Where does a non-detection mean something? Effort, completeness and time on an
EEA grid, a kernel effort surface, and per-species "could it have been
missed", for acoustic data. The camera-trap module has the same page; the
computation lives in gap_analysis.py / gap_metrics.py, these routes only parse
the page's filters, enforce access and cache.

Kept out of routes.py on purpose (it is 5.5k lines already).
"""
import threading
import time
from datetime import datetime

from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import text

from app.utils.decorators import role_required

from . import pam_bp
from . import access as pam_access
from . import gap_analysis as ga
from . import gap_metrics as gm
from .domain import _
from .utils import (_confidence_filter_sql, _confidence_value_sql, _normalize_model_mode,
                    get_institution_filter, get_models_list, get_pam_db_connection)

# Page and API are open to every signed-in user; the data each user sees is
# limited by institution access (public locations + their institutions, admin
# sees all) and sensitive species by role, the same rules as the rest of the
# module. Set a role here (e.g. 'manager') to restrict the page again, and
# guard the card in pam_home.html.
GAP_ANALYSIS_MIN_ROLE = None

GAP_CACHE_TTL_SECONDS = 600
GAP_CACHE_MAX_ENTRIES = 32
# Acoustic detection reaches a few hundred metres, and the birds' territories
# are small, so the bandwidths start lower than on the camera-trap page.
GAP_KERNEL_BANDWIDTHS_M = (500, 1000, 2500, 5000)
GAP_KERNEL_DEFAULT_M = 1000

# Buffer ring around recorders and planned points on the map (metres): roughly
# how far a recorder hears a singing bird, set by the user per species and habitat.
GAP_BUFFER_MIN_M = 50
GAP_BUFFER_MAX_M = 2000
GAP_BUFFER_DEFAULT_M = 500

_gap_cache = {}
_gap_cache_lock = threading.Lock()


def _cache_get(key, build):
    """Tiny per-process TTL cache. Every key carries the caller's access rights."""
    now = time.monotonic()
    with _gap_cache_lock:
        hit = _gap_cache.get(key)
        if hit and now - hit[0] < GAP_CACHE_TTL_SECONDS:
            return hit[1]
    value = build()
    with _gap_cache_lock:
        if len(_gap_cache) >= GAP_CACHE_MAX_ENTRIES:
            oldest = min(_gap_cache, key=lambda k: _gap_cache[k][0])
            _gap_cache.pop(oldest, None)
        _gap_cache[key] = (now, value)
    return value


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else None
    except ValueError:
        return None


def _is_admin():
    return current_user.is_authenticated and current_user.has_role('admin')


def _user_roles():
    return sorted(role.name for role in getattr(current_user, 'roles', []) or [])


def _species_condition():
    """Species visibility by role (alias ``s``), as on the other PAM pages."""
    if _is_admin():
        return 'TRUE', {}
    roles = _user_roles()
    if roles:
        return '(s.required_role IS NULL OR s.required_role = ANY(:user_roles))', {'user_roles': roles}
    return 's.required_role IS NULL', {}


def _model_choice(raw):
    """(mode, model_id) from the page's model picker: '', a model id, or 'combined'."""
    if raw == 'combined':
        return 'combined', None
    try:
        model_id = int(raw)
    except (TypeError, ValueError):
        return 'birdnet', None
    mode = _normalize_model_mode('model', model_id)
    return (mode, model_id) if mode == 'model' else ('birdnet', None)


def _request_params():
    """Validated filters shared by every gap-analysis endpoint."""
    args = request.args
    scale = args.get('scale', type=int)
    unit = args.get('unit', ga.DEFAULT_UNIT)
    taxon = args.get('taxon', ga.DEFAULT_TAXON)
    start = _parse_date(args.get('start_date'))
    end = _parse_date(args.get('end_date'))
    if not (start and end) or start > end:
        start = end = None
    institution = args.get('institution_id', type=int)
    mode, model_id = _model_choice(args.get('model', ''))
    return {
        'scale': scale if scale in gm.GRID_SCALES_M else 5000,
        'unit': unit if unit in (ga.UNIT_DAY, ga.UNIT_WEEK) else ga.DEFAULT_UNIT,
        'taxon': taxon if taxon in ga.TAXON_GROUPS else ga.DEFAULT_TAXON,
        'threshold': ga.clamp_threshold(args.get('threshold', ga.DEFAULT_THRESHOLD)),
        'species_id': args.get('species_id', type=int),
        'institution_id': institution,
        'mode': mode, 'model_id': model_id,
        'start': start, 'end': end,
    }


def _access_key():
    if _is_admin():
        return ('admin',)
    return (tuple(sorted(pam_access.allowed_institution_ids(current_user) or ())),
            tuple(_user_roles()))


def _gap_data(conn, p):
    """(cache key, GapData) for the request's filters and the user's access.

    Cached below the threshold: scores are fetched down to the slider minimum,
    so moving the slider re-filters the cached data and never hits the database.
    """
    key = ('data', _access_key(), p['institution_id'], p['start'], p['end'], p['taxon'],
           p['mode'], p['model_id'])

    def build():
        inst_condition, inst_params = get_institution_filter(
            pam_access.allowed_institution_ids(current_user), _is_admin(),
            selected_inst_id=p['institution_id'])
        species_condition, species_params = _species_condition()
        return ga.fetch_gap_data(
            conn, inst_condition, inst_params, start_date=p['start'], end_date=p['end'],
            taxon=p['taxon'], species_condition=species_condition, species_params=species_params,
            score_sql=_confidence_value_sql(p['mode'], p['model_id']),
            score_filter_sql=_confidence_filter_sql(p['mode'], p['model_id']))
    return key, _cache_get(key, build)


def _thresholded(conn, p):
    data_key, data = _gap_data(conn, p)
    key = ('thr', data_key, p['threshold'])
    return key, _cache_get(key, lambda: ga.apply_threshold(data, p['threshold']))


def _gap_grid(conn, p):
    data_key, data = _thresholded(conn, p)
    key = ('grid', data_key, p['scale'], p['unit'], p['species_id'], g.lang_code)
    return data, _cache_get(key, lambda: ga.build_grid(
        data, p['scale'], unit=p['unit'], focus_species_id=p['species_id'], lang=g.lang_code))


def _accessible_institutions(conn):
    """Institutions with locations the user may see, for the scope picker."""
    inst_condition, params = get_institution_filter(
        pam_access.allowed_institution_ids(current_user), _is_admin())
    rows = conn.execute(text(f"""
        SELECT DISTINCT i.id, i.name_uk, i.name_en
        FROM institutions i
        JOIN location_institutions li ON li.institution_id = i.id
        JOIN locations l ON l.location_id = li.location_id
        WHERE {inst_condition}
        ORDER BY i.name_uk
    """), params).fetchall()
    return [{'id': r.id, 'name': (r.name_en if g.lang_code == 'en' and r.name_en else r.name_uk)}
            for r in rows]


@pam_bp.route('/<lang_code>/pam/gap-analysis')
def pam_gap_analysis_page(lang_code):
    """Gap analysis page: where the acoustic data are thin, and in what sense."""
    g.lang_code = lang_code
    if GAP_ANALYSIS_MIN_ROLE:
        return login_required(role_required(GAP_ANALYSIS_MIN_ROLE)(_render_gap_page))(lang_code)
    return login_required(_render_gap_page)(lang_code)


def _render_gap_page(lang_code):
    conn = None
    try:
        conn = get_pam_db_connection()
        return render_template(
            'pam_gap_analysis.html',
            institutions=_accessible_institutions(conn),
            models=get_models_list(),
            grid_scales=gm.GRID_SCALES_M,
            effort_targets=gm.EFFORT_TARGET_HOURS,
            kernel_bandwidths=[(bw, round(gm.home_range_km2(bw), 1)) for bw in GAP_KERNEL_BANDWIDTHS_M],
            kernel_default=GAP_KERNEL_DEFAULT_M,
            buffer_min=GAP_BUFFER_MIN_M, buffer_max=GAP_BUFFER_MAX_M,
            buffer_default=GAP_BUFFER_DEFAULT_M,
            min_units=gm.MIN_UNITS_FOR_ESTIMATE,
            pool_radius_km=ga.POOL_RADIUS_M // 1000,
            threshold_min=ga.MIN_THRESHOLD, threshold_max=ga.MAX_THRESHOLD,
            threshold_default=ga.DEFAULT_THRESHOLD,
        )
    except Exception as e:
        current_app.logger.error(f"Error loading PAM gap analysis page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки."), 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn is not None:
            conn.close()


def _json_endpoint(lang_code, name, produce):
    """Run ``produce(conn, params)`` with connection cleanup and uniform errors."""
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        return produce(conn, _request_params())
    except Exception as e:
        current_app.logger.error(f"Error in PAM gap analysis {name} API: {e}", exc_info=True)
        return jsonify({'error': 'Server error'}), 500
    finally:
        if conn is not None:
            conn.close()


@pam_bp.route('/<lang_code>/api/pam/gap-analysis/grid')
@login_required
def api_pam_gap_grid(lang_code):
    """Grid layer (cells with metrics + empty cells), summary and location points."""
    def produce(conn, p):
        data, grid = _gap_grid(conn, p)
        return jsonify({'summary': grid['summary'], 'cells': grid['cells'],
                        'locations': ga.location_points(data, g.lang_code)})
    return _json_endpoint(lang_code, 'grid', produce)


@pam_bp.route('/<lang_code>/api/pam/gap-analysis/cell/<code>')
@login_required
def api_pam_gap_cell(lang_code, code):
    """Accumulation curve, diel profile, species and locations of one grid cell."""
    def produce(conn, p):
        data, grid = _gap_grid(conn, p)
        detail = ga.cell_detail(grid, code, data, lang=g.lang_code)
        if detail is None:
            return jsonify({'error': 'Cell not found'}), 404
        return jsonify(detail)
    return _json_endpoint(lang_code, 'cell', produce)


@pam_bp.route('/<lang_code>/api/pam/gap-analysis/kernel')
@login_required
def api_pam_gap_kernel(lang_code):
    """Kernel effort surface (recording hours per km²) for the chosen bandwidth."""
    def produce(conn, p):
        bandwidth = request.args.get('bandwidth', type=int)
        if bandwidth not in GAP_KERNEL_BANDWIDTHS_M:
            bandwidth = GAP_KERNEL_DEFAULT_M
        data_key, data = _gap_data(conn, p)
        return jsonify(_cache_get(('kernel', data_key, bandwidth),
                                  lambda: ga.build_kernel(data, bandwidth)))
    return _json_endpoint(lang_code, 'kernel', produce)


@pam_bp.route('/<lang_code>/api/pam/gap-analysis/biotopes')
@login_required
def api_pam_gap_biotopes(lang_code):
    """Effort and completeness pooled by biotope (gaps in environmental space)."""
    def produce(conn, p):
        data_key, data = _thresholded(conn, p)
        rows = _cache_get(('biotopes', data_key, p['unit'], g.lang_code),
                          lambda: ga.biotope_summary(data, p['unit'], g.lang_code))
        return jsonify({'rows': rows})
    return _json_endpoint(lang_code, 'biotopes', produce)
