# SPDX-License-Identifier: AGPL-3.0-only
import calendar
import csv
import io
import math
import os
import threading
from datetime import date, datetime, timedelta, timezone

# Third-party libraries (Data Science & Audio).
import librosa
import librosa.display
import numpy as np
import pandas as pd
import requests
from suncalc import get_times

# Matplotlib (backend must be set before importing pyplot).
import matplotlib
matplotlib.use('Agg')  # Prevents GUI window attempts on a headless server.
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# Flask and extensions.
from flask import current_app
from flask_login import current_user

# SQLAlchemy
from sqlalchemy import create_engine, table, text, column
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy.pool import QueuePool, StaticPool

# Local modules.
from app.models import User

# Global variable for the engine singleton.
_pam_engine = None
_engine_lock = threading.Lock()

_geodata_engine = None
_geodata_lock = threading.Lock()

def get_pam_engine():
    """
    Create and return a SQLAlchemy engine with a correctly configured connection pool.
    Uses the singleton pattern to avoid creating multiple engines.
    """
    global _pam_engine
    
    if _pam_engine is None:
        with _engine_lock:
            if _pam_engine is None:
                _pam_engine = create_engine(
                    current_app.config['PAM_DATABASE_URI'],
                    poolclass=QueuePool,
                    pool_size=5,           # Max 5 connections in the pool.
                    max_overflow=10,       # Max 10 overflow connections.
                    pool_timeout=30,       # Connection wait timeout.
                    pool_recycle=300,      # Recycle connections every 5 minutes.
                    pool_pre_ping=True,    # Test connections before use.
                    echo=False
                )
    
    return _pam_engine


def get_user_pam_stats(user_id):
    """Return personal PAM statistics for a user (read-only).

    Returns a dict: verifications (total), positive (confirmed = 1),
    positive_rate (%), species_count (unique species in verified segments).
    """
    engine = get_pam_engine()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN sv.verification_result = 1 THEN 1 END) AS positive,
                   COUNT(DISTINCT s.species_id) AS species
            FROM segment_verifications sv
            JOIN segments s ON s.id = sv.segment_id
            WHERE sv.user_id = :uid
        """), {"uid": user_id}).fetchone()
    total = (r.total if r else 0) or 0
    positive = (r.positive if r else 0) or 0
    return {
        'verifications': total,
        'positive': positive,
        'positive_rate': round(positive * 100.0 / total, 1) if total else 0.0,
        'species_count': (r.species if r else 0) or 0,
    }


def get_pam_db_connection():
    """Create and return a PAM database connection (uses connection pool)."""
    engine = get_pam_engine()
    return engine.connect()

def get_geodata_engine():
    """Create and return a singleton engine for the geodata database."""
    global _geodata_engine
    if _geodata_engine is None:
        with _geodata_lock:
            if _geodata_engine is None:
                _geodata_engine = create_engine(
                    current_app.config['GEODATA_DATABASE_URI'],
                    pool_size=5,
                    max_overflow=10,
                    pool_timeout=30,
                    pool_recycle=300,
                    pool_pre_ping=True
                )
    return _geodata_engine

def get_geodata_db_connection():
    """Return a connection to the GEODATA database."""
    engine = get_geodata_engine()
    return engine.connect()

def get_institution_filter(user_inst_ids=None, is_admin=False, selected_inst_id=None):
    """
    Generate a SQL condition for access-rights filtering AND selected institutions.
    selected_inst_id can be a string '1,2,3', a number 2, or a list [1, 2].
    """
    # 1. Base access-rights condition.
    if is_admin:
        base_condition = "1=1"
        params = {}
    elif not user_inst_ids:
        base_condition = "l.visibility_level = 0"
        params = {}
    else:
        base_condition = """
            (l.visibility_level = 0 OR EXISTS (
                SELECT 1 FROM location_institutions li_perm 
                WHERE li_perm.location_id = l.location_id 
                AND li_perm.institution_id = ANY(:user_inst_ids)
            ))
        """
        params = {"user_inst_ids": user_inst_ids}

    # 2. Additional condition for selected institutions (when set via filter).
    if selected_inst_id:
        # Normalise to a list of ints.
        if isinstance(selected_inst_id, str):
            ids = [int(i) for i in selected_inst_id.split(',') if i.strip().isdigit()]
        elif isinstance(selected_inst_id, (int, float)):
            ids = [int(selected_inst_id)]
        else:
            ids = [int(i) for i in selected_inst_id]

        if ids:
            base_condition += """
                AND EXISTS (
                    SELECT 1 FROM location_institutions li_sel 
                    WHERE li_sel.location_id = l.location_id 
                    AND li_sel.institution_id = ANY(:selected_inst_id)
                )
            """
            params['selected_inst_id'] = ids

    return base_condition, params

def calculate_sun_times_simple(date_obj, longitude, latitude):
    """
    Simplified sunrise/sunset time calculation.
    Converts results to UTC+2 (Kyiv time).
    """
    try:
        # Constants.
        CIVIL_ZENITH = 90.833
        
        # Day of year.
        day_of_year = date_obj.timetuple().tm_yday
        
        # Solar declination.
        P = math.asin(.39795 * math.cos(.98563 * (day_of_year - 173) * math.pi / 180))
        
        # Latitude in radians.
        lat_rad = latitude * math.pi / 180
        
        # Hour-angle calculation.
        argument = -math.tan(lat_rad) * math.tan(P)
        
        if argument < -1:
            argument = -1
        elif argument > 1:
            argument = 1
            
        hour_angle = 24 * math.acos(argument) / (2 * math.pi)
        
        # Times in decimal hours (relative to UTC).
        sunrise_hour = 12 - hour_angle - longitude / 15
        sunset_hour = 12 + hour_angle - longitude / 15

        # 1. Create naive datetime objects in UTC.
        utc_sunrise = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=sunrise_hour)
        utc_sunset = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=sunset_hour)

        # 2. Target timezone (UTC+2).
        kyiv_tz = timezone(timedelta(hours=2))

        # 3. Make timezone-aware objects using UTC as the source timezone.
        aware_utc_sunrise = utc_sunrise.replace(tzinfo=timezone.utc)
        aware_utc_sunset = utc_sunset.replace(tzinfo=timezone.utc)

        # 4. Convert to the target timezone.
        local_sunrise = aware_utc_sunrise.astimezone(kyiv_tz)
        local_sunset = aware_utc_sunset.astimezone(kyiv_tz)
        
        return {
            'sunrise': local_sunrise,
            'sunset': local_sunset
        }
        
    except Exception as e:
        current_app.logger.error(f"Error calculating sun times: {e}")
        # Fallback values (also timezone-aware).
        kyiv_tz = timezone(timedelta(hours=2))
        sunrise_time = (datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=6)).replace(tzinfo=kyiv_tz)
        sunset_time = (datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=18)).replace(tzinfo=kyiv_tz)
        return {
            'sunrise': sunrise_time,
            'sunset': sunset_time
        }

def get_available_species(lang_code):
    """Return the list of PAM species accessible to the current user."""
    conn = None
    try:
        conn = get_pam_db_connection()
        base_query_select = "SELECT scientific_name, common_name_en, common_name_uk, required_role FROM species"
        query_filter = "WHERE required_role IS NULL"
        params = {}
        
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            # Get all role names for this user.
            user_roles = [role.name for role in current_user.roles]
            
            # Admin sees everything — no restrictions.
            if 'admin' in user_roles:
                query_filter = ""  # Remove all restrictions.
            
            # For other users with roles, build a dynamic filter.
            elif user_roles:  # Check that the roles list is not empty.
                query_filter = "WHERE required_role IS NULL OR required_role IN :roles"
                # SQLAlchemy/psycopg2 handles the list correctly for the IN operator.
                params['roles'] = tuple(user_roles)
        
        full_query = f"{base_query_select} {query_filter} ORDER BY scientific_name"
        db_result = conn.execute(text(full_query), params).mappings().fetchall()
        
        formatted_species = []
        for species in db_result:
            display_name = species['scientific_name']
            if lang_code == 'uk' and species['common_name_uk']:
                display_name = f"{species['common_name_uk']} ({species['scientific_name']})"
            elif lang_code == 'en' and species['common_name_en']:
                display_name = f"{species['common_name_en']} ({species['scientific_name']})"
            
            formatted_species.append({'value': species['scientific_name'], 'text': display_name})
            
        formatted_species.sort(key=lambda x: x['text'])
        return formatted_species
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_available_species): {e}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_error:
                current_app.logger.error(f"Error closing connection: {close_error}")

# ──────────────────────────────────────────────────────────────────────────────
# Dashboard model switcher (Task B): BirdNET 2.4 / a specific model / combined.
#
# detections is one row per biological event (recording_id, species_id, start_s,
# end_s); detections.confidence holds the BirdNET-2.4 reference confidence, and
# detection_models(detection_id, model_id, confidence) stores every model's own
# score. The three modes differ ONLY in which confidence is filtered/displayed —
# all dashboard queries use a flat confidence threshold, so the switch is a
# swappable WHERE predicate (and SELECT expression) rather than new JOINs.
#   'birdnet'  – reference model: detections.confidence (DEFAULT; SQL unchanged)
#   'model'    – a specific model_id: its detection_models.confidence
#   'combined' – any model: the best (MAX) detection_models.confidence per event
# EXISTS keeps COUNT()/aggregations correct (no row fan-out from the join).
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_model_mode(mode, model_id, params=None):
    """Validate (mode, model_id) and bind :model_id into params when relevant.

    Returns the effective mode, falling back to 'birdnet' for anything invalid
    or for 'model' without a model_id — so callers always get safe behavior.
    """
    if mode == 'combined':
        return 'combined'
    if mode == 'model' and model_id is not None:
        if params is not None:
            params['model_id'] = model_id
        return 'model'
    return 'birdnet'


def _confidence_filter_sql(mode='birdnet', model_id=None, alias='d'):
    """WHERE predicate filtering detections by confidence for the active mode.

    Consumes the already-bound :confidence param; 'model' mode also uses
    :model_id (bind it via _normalize_model_mode first).
    """
    if mode == 'model' and model_id is not None:
        return (f"EXISTS (SELECT 1 FROM detection_models dm "
                f"WHERE dm.detection_id = {alias}.detection_id "
                f"AND dm.model_id = :model_id AND dm.confidence >= :confidence)")
    if mode == 'combined':
        return (f"EXISTS (SELECT 1 FROM detection_models dm "
                f"WHERE dm.detection_id = {alias}.detection_id "
                f"AND dm.confidence >= :confidence)")
    return f"{alias}.confidence >= :confidence"


_CONSENSUS_THRESHOLD = 2.0 / 3.0  # mirrors update_segment_stats() (migration 0004)


def _verification_display_status(consensus_result, total_votes, positive_votes):
    """Map a detection's verification state to a chart display status.

    Precedence:
      1. An AUTHORITATIVE result already recorded in
         detection_verification_map.verification_result (``consensus_result``)
         wins — this covers both a ≥2-vote consensus and the legacy
         hand-verified segments imported as a single authoritative vote (see
         rebuild_dvm / migration 0004). These render in the dark shades because
         the rest of the system (evaluation, etc.) treats them as ground truth.
      2. Otherwise (dvm NULL) derive a PROVISIONAL status from the live segment
         vote tally so an in-app reviewer's not-yet-consensual work is still
         visible — a single such vote gets a light shade; a genuine ≥2-vote
         conflict stays blue. A ≥2-vote consensus here also covers the rare case
         of a dvm row the trigger has not upserted yet.

    total_votes counts only meaningful votes (0/1); "unknown" (2) / skips are
    excluded by update_segment_stats when it fills these columns.

    Returns one of:
      'consensus_confirmed' — dvm=1, or dvm NULL & ≥2 votes ≥2/3 positive  (dark green)
      'consensus_rejected'  — dvm=0, or dvm NULL & ≥2 votes ≤1/3 positive  (dark red)
      'single_confirmed'    — dvm NULL & exactly 1 positive vote            (light green)
      'single_rejected'     — dvm NULL & exactly 1 negative vote            (light red)
      'unverified'          — no votes, or a dvm-NULL ≥2-vote conflict      (blue)
    """
    if consensus_result == 1:
        return 'consensus_confirmed'
    if consensus_result == 0:
        return 'consensus_rejected'
    total = total_votes or 0
    pos = positive_votes or 0
    if total >= 2:
        ratio = pos / total
        if ratio >= _CONSENSUS_THRESHOLD:
            return 'consensus_confirmed'
        if ratio <= (1 - _CONSENSUS_THRESHOLD):
            return 'consensus_rejected'
        return 'unverified'  # genuine conflict — no consensus, treated as unverified
    if total == 1:
        return 'single_confirmed' if pos == 1 else 'single_rejected'
    return 'unverified'


def _confidence_value_sql(mode='birdnet', model_id=None, alias='d'):
    """SELECT expression for the confidence value to DISPLAY in the active mode."""
    if mode == 'model' and model_id is not None:
        return (f"(SELECT dm.confidence FROM detection_models dm "
                f"WHERE dm.detection_id = {alias}.detection_id AND dm.model_id = :model_id)")
    if mode == 'combined':
        return (f"(SELECT MAX(dm.confidence) FROM detection_models dm "
                f"WHERE dm.detection_id = {alias}.detection_id)")
    return f"{alias}.confidence"


def get_models_list():
    """Return classifier models for the dashboard model switcher (Task B).

    Each item: {'model_id', 'label', 'is_reference'}; is_reference marks the
    BirdNET 2.4 reference model whose confidence lives in detections.confidence.
    Returns [] if the models table is empty/absent, so callers can hide the switch.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        rows = conn.execute(text(
            "SELECT model_id, name, version FROM models ORDER BY model_id"
        )).fetchall()
        return [
            {'model_id': r.model_id,
             'label': (f"{r.name} {r.version}".strip() if r.version else r.name),
             'is_reference': (r.name == 'BirdNET' and (r.version or '') == '2.4')}
            for r in rows
        ]
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_models_list): {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def get_reference_model_id(conn=None):
    """model_id of the BirdNET 2.4 reference model, or None if not seeded.

    The reference is the model whose confidence lives in detections.confidence;
    it is the default everywhere a model must be chosen (sampling, evaluation
    view) so behaviour is unchanged when the user makes no explicit choice.
    """
    own = conn is None
    try:
        if own:
            conn = get_pam_db_connection()
        row = conn.execute(text(
            "SELECT model_id FROM models WHERE name = 'BirdNET' AND version = '2.4'"
        )).fetchone()
        return int(row[0]) if row else None
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_reference_model_id): {e}")
        return None
    finally:
        if own and conn is not None:
            conn.close()


def get_filtered_detections(species_name, start_date=None, end_date=None, confidence=0.0, location_ids=None, biotope_ids=None, institution_id=None, mode='birdnet', model_id=None):
    """Return filtered detections, including verification status for each one."""
    conn = None
    try:
        conn = get_pam_db_connection()
        params = {'species_name': species_name, 'confidence': confidence}
        mode = _normalize_model_mode(mode, model_id, params)

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        joins = """
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
            LEFT JOIN segments seg ON dvm.segment_id = seg.id
        """
        conditions = [
                    "s.scientific_name = :species_name",
                    _confidence_filter_sql(mode, model_id),
                    inst_condition
                    ]

        if start_date:
            conditions.append("DATE(r.datetime_start) >= :start_date")
            params['start_date'] = start_date
        if end_date:
            conditions.append("DATE(r.datetime_start) <= :end_date")
            params['end_date'] = end_date
        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids
        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids
            
        where_clause = " AND ".join(conditions)

        query_sql = f"""
            SELECT
                r.datetime_start,
                {_confidence_value_sql(mode, model_id)} AS confidence,
                dvm.verification_result,
                seg.verification_count,
                seg.positive_verifications
            FROM detections d
            JOIN species s ON d.species_id = s.species_id
            {joins}
            WHERE {where_clause}
            ORDER BY r.datetime_start
        """

        db_result = conn.execute(text(query_sql), params).mappings().fetchall()
        return [
            {
                'datetime': r['datetime_start'].strftime('%Y-%m-%d %H:%M:%S'),
                'confidence': r['confidence'],
                'verification_result': r['verification_result'],
                'verification_status': _verification_display_status(
                    r['verification_result'], r['verification_count'], r['positive_verifications'])
            }
            for r in db_result if r['datetime_start']
        ]
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_filtered_detections): {e}")
        return []
    finally:
        if conn is not None:
            conn.close()

def get_daily_detection_counts(species_name, start_date, end_date, confidence, location_ids=None, biotope_ids=None, excel_exp=False, institution_id=None, mode='birdnet', model_id=None):
    conn = None
    try:
        conn = get_pam_db_connection()
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=365)

        params = {
            'species_name': species_name,
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj
        }
        mode = _normalize_model_mode(mode, model_id, params)

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        joins = "JOIN recordings r ON d.recording_id = r.recording_id JOIN locations l ON r.location_id = l.location_id"
        conditions = [
            "s.scientific_name = :species_name",
            _confidence_filter_sql(mode, model_id),
            "DATE(r.datetime_start) >= :start_date",
            "DATE(r.datetime_start) <= :end_date",
            inst_condition
        ]

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids
        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids
        
        where_clause = " AND ".join(conditions)

        query_sql = f"""
            SELECT DATE(r.datetime_start) as detection_date, COUNT(d.detection_id) as detection_count 
            FROM detections d 
            JOIN species s ON d.species_id = s.species_id 
            {joins}
            WHERE {where_clause}
            GROUP BY DATE(r.datetime_start) 
            ORDER BY detection_date
        """
        
        db_result = conn.execute(text(query_sql), params).mappings().fetchall()
        if not db_result:
            return {'dates': [], 'counts': []}

        if excel_exp:
            df = pd.DataFrame(db_result)
            df['detection_date'] = pd.to_datetime(df['detection_date'])
            df = df.set_index('detection_date')
            date_range = pd.date_range(start=start_date_obj, end=end_date_obj, freq='D')
            df = df.reindex(date_range, fill_value=0)
            
            return {
                'dates': df.index.strftime('%Y-%m-%d').tolist(), 
                'counts': df['detection_count'].tolist()
            }
        else:
            dates = [row['detection_date'].strftime('%Y-%m-%d') for row in db_result]
            counts = [row['detection_count'] for row in db_result]
            
            return {
                'dates': dates, 
                'counts': counts
            }
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_daily_detection_counts): {e}")
        return {'dates': [], 'counts': []}
    finally:
        if conn is not None:
            conn.close()

def get_time_scatter_data(species_name, start_date, end_date, confidence, location_ids=None, biotope_ids=None, excel_exp=False, institution_id=None, mode='birdnet', model_id=None):
    """Return scatter data for daily activity chart, including verification status per point."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=30)
        
        params = {
            'species_name': species_name, 
            'confidence': confidence, 
            'start_date': start_date_obj, 
            'end_date': end_date_obj
        }

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)
        
        mode = _normalize_model_mode(mode, model_id, params)

        joins = """
            JOIN locations l ON r.location_id = l.location_id
            LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
            LEFT JOIN segments seg ON dvm.segment_id = seg.id
        """
        conditions = [
            "s.scientific_name = :species_name",
            _confidence_filter_sql(mode, model_id),
            "DATE(r.datetime_start) >= :start_date",
            "DATE(r.datetime_start) <= :end_date",
            inst_condition
        ]

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids
        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids

        where_clause = " AND ".join(conditions)

        # 1. Base fields always required.
        select_fields = [
            "r.datetime_start",
            "l.lat",
            "l.lon",
            "dvm.verification_result",
            "seg.verification_count",
            "seg.positive_verifications"
        ]

        # 2. Optional extra fields.
        if excel_exp:
            select_fields.append(f"{_confidence_value_sql(mode, model_id)} AS confidence")

        # 3. Build the column string.
        columns_str = ", ".join(select_fields)

        # 4. Assemble the query.
        query_sql = f"""
            SELECT 
                {columns_str}
            FROM detections d
            JOIN species s ON d.species_id = s.species_id
            JOIN recordings r ON d.recording_id = r.recording_id
            {joins}
            WHERE {where_clause}
            ORDER BY r.datetime_start
        """
        conn.execute(text("SET work_mem = '128MB';"))
        conn.execute(text("SET jit = off;"))
        result_proxy = conn.execute(text(query_sql), params)

        #db_result = conn.execute(text(query_sql), params).mappings().fetchall()
        db_result = result_proxy.mappings().all()

        if not db_result:
            return {'detections': [], 'sun_times': []}
        
        detections = [{
            'date': r['datetime_start'].strftime('%Y-%m-%d'),
            'time': r['datetime_start'].strftime('%H:%M:%S'),
            'confidence': r.get('confidence', None),
            'verification_result': r['verification_result'],
            'verification_status': _verification_display_status(
                r['verification_result'], r['verification_count'], r['positive_verifications'])
            } for r in db_result if r['datetime_start']]
        
        lat, lon = (float(db_result[0]['lat']), float(db_result[0]['lon'])) if db_result else (49.0, 32.0)
        
        sun_times = []
        current_date_loop = start_date_obj
        while current_date_loop <= end_date_obj:
            times = calculate_sun_times_simple(current_date_loop, lon, lat)
            if 'sunrise' in times and 'sunset' in times:
                sun_times.append({
                    'date': current_date_loop.strftime('%Y-%m-%d'), 
                    'sunrise': times['sunrise'].strftime('%H:%M:%S'), 
                    'sunset': times['sunset'].strftime('%H:%M:%S')
                })
            current_date_loop += timedelta(days=1)
        
        return {'detections': detections, 'sun_times': sun_times}
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_time_scatter_data): {e}", exc_info=True)
        return {'detections': [], 'sun_times': []}
    finally:
        if conn is not None:
            conn.close()

def get_species_summary(species_name, start_date=None, end_date=None, confidence=0.0, location_id=None, location_ids=None, biotope_ids=None, min_detections=1, institution_id=None, mode='birdnet', model_id=None):
    conn = None
    try:
        conn = get_pam_db_connection()

        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=30)
        total_days_in_period = (end_date_obj - start_date_obj).days + 1

        params = {
            'species_name': species_name,
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj,
            'min_detections': min_detections
        }

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        mode = _normalize_model_mode(mode, model_id, params)

        base_joins = """
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN species s ON d.species_id = s.species_id
            JOIN locations l ON r.location_id = l.location_id
        """
        base_conditions = f"""
            WHERE s.scientific_name = :species_name
              AND {_confidence_filter_sql(mode, model_id)}
              AND DATE(r.datetime_start) >= :start_date
              AND DATE(r.datetime_start) <= :end_date
              AND {inst_condition}
        """

        # CTE to filter locations satisfying the min_detections threshold.
        with_clause = f"""
            WITH valid_locations AS (
                SELECT l.location_id
                {base_joins}
                {base_conditions}
                GROUP BY l.location_id
                HAVING COUNT(d.detection_id) >= :min_detections
            )
        """
        location_filter_condition = " AND l.location_id IN (SELECT location_id FROM valid_locations)"

        joins = ""
        conditions = []
        if location_id is not None:
            conditions.append("l.location_id = :location_id")
            params['location_id'] = location_id
        elif location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids
        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids
        
        condition_sql = (" AND " + " AND ".join(conditions)) if conditions else ""

        # Total detections for the species (on filtered locations only).
        sql_total = f"""
            {with_clause}
            SELECT COUNT(*) as cnt
            {base_joins}
            {joins}
            {base_conditions}
            {condition_sql}
            {location_filter_condition}
        """
        total_detections = conn.execute(text(sql_total), params).scalar() or 0

        # Unique locations count.
        sql_locs = f"""
            {with_clause}
            SELECT COUNT(DISTINCT l.location_id) as cnt
            {base_joins}
            {joins}
            {base_conditions}
            {condition_sql}
            {location_filter_condition}
        """
        unique_locations = conn.execute(text(sql_locs), params).scalar() or 0

        # Days with detections.
        sql_days = f"""
            {with_clause}
            SELECT COUNT(DISTINCT DATE(r.datetime_start)) as cnt
            {base_joins}
            {joins}
            {base_conditions}
            {condition_sql}
            {location_filter_condition}
        """
        days_with_detections = conn.execute(text(sql_days), params).scalar() or 0

        # Total detections across all species (for percentage calculation).
        sql_all_species = f"""
            SELECT COUNT(*) as cnt
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            {joins}
            WHERE {_confidence_filter_sql(mode, model_id)}
              AND DATE(r.datetime_start) >= :start_date
              AND DATE(r.datetime_start) <= :end_date
              {condition_sql}
        """
        params_all_species = params.copy()
        del params_all_species['species_name']
        del params_all_species['min_detections']
        total_detections_all_species = conn.execute(text(sql_all_species), params_all_species).scalar() or 0

        percent_from_all = (total_detections / total_detections_all_species * 100) if total_detections_all_species else 0
        percent_days_with_detections = (days_with_detections / total_days_in_period * 100) if total_days_in_period else 0

        return {
            'species_name': species_name,
            'start_date': start_date_obj.isoformat(),
            'end_date': end_date_obj.isoformat(),
            'confidence': confidence,
            'location_id': location_id,
            'total_detections': total_detections,
            'unique_locations': unique_locations,
            'percent_from_all': round(percent_from_all, 2),
            'days_with_detections': days_with_detections,
            'total_days_in_period': total_days_in_period,
            'percent_days_with_detections': round(percent_days_with_detections, 2)
        }

    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_species_summary): {e}", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()

def get_unique_detection_points(lang_code, species_name, start_date=None, end_date=None, confidence=0.0, location_ids=None, biotope_ids=None, min_detections=1, institution_id=None, mode='birdnet', model_id=None):
    conn = None
    try:
        conn = get_pam_db_connection()
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=30)

        params = {
            'species_name': species_name,
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj,
            'min_detections': min_detections
        }

        user_inst_ids = []
        is_admin = False
        if current_user.is_authenticated:
            # Collect IDs of all institutions the user belongs to.
            user_inst_ids = [inst.id for inst in current_user.institutions]
            is_admin = current_user.has_role('admin')

        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        mode = _normalize_model_mode(mode, model_id, params)

        joins = "LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id"
        conditions = [
            "s.scientific_name = :species_name",
            _confidence_filter_sql(mode, model_id),
            "DATE(r.datetime_start) >= :start_date",
            "DATE(r.datetime_start) <= :end_date",
            inst_condition
        ]

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids
        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT 
                l.location_id, 
                l.location_name, 
                l.location_name_en, 
                l.lat, 
                l.lon,
                COUNT(d.detection_id) as detection_count,
                MAX(dvm.verification_result) = 1 as is_verified
            FROM detections d
            JOIN species s ON d.species_id = s.species_id
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            {joins}
            WHERE {where_clause}
            GROUP BY l.location_id, l.location_name, l.location_name_en, l.lat, l.lon
            HAVING COUNT(d.detection_id) >= :min_detections
        """

        db_result = conn.execute(text(sql), params).mappings().fetchall()

        points = []
        for row in db_result:
            display_name = row['location_name']
            if lang_code == 'en' and row['location_name_en']:
                display_name = row['location_name_en']
            
            points.append({
                'location_id': row['location_id'],
                'location_name': display_name,
                'lat': float(row['lat']),
                'lon': float(row['lon']),
                'detection_count': row['detection_count'],
                'is_verified': row['is_verified']
            })

        return points

    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_unique_detection_points): {e}")
        return []
    finally:
        if conn is not None:
            conn.close()

def get_species_ranking(lang_code, start_date=None, end_date=None, confidence=0.0, min_detections=1, location_ids=None, biotope_ids=None, tax_filters=None, institution_id=None, mode='birdnet', model_id=None):
    """Return a ranked species table with detection counts."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=365)
        
        params = {
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj,
            'min_detections': min_detections
        }
        
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)
        
        # Base JOINs always required.
        from_clause = """
            FROM species s
            JOIN detections d ON s.species_id = d.species_id
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
        """
        
        # Additional JOINs for filters.
        if biotope_ids:
            from_clause += " JOIN location_biotopes lb ON l.location_id = lb.location_id"

        # Build WHERE conditions.
        mode = _normalize_model_mode(mode, model_id, params)
        conditions = [
            _confidence_filter_sql(mode, model_id),
            "DATE(r.datetime_start) >= :start_date",
            "DATE(r.datetime_start) <= :end_date",
            inst_condition
        ]

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids

        if biotope_ids:
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids

        # Access filter.
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            user_roles = [role.name for role in current_user.roles]
            
            # Admin sees everything — no role restrictions added.
            if 'admin' not in user_roles:
                # For other users, allow species with no role OR a role the user has.
                # SQLAlchemy handles :user_roles as a list for the IN operator.
                conditions.append("(s.required_role IS NULL OR s.required_role IN :user_roles)")
                params['user_roles'] = tuple(user_roles)
        else:
            # Unauthenticated users: only species with no role restriction.
            conditions.append("s.required_role IS NULL")
        

        if tax_filters:
            for key, value in tax_filters.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value

        where_clause = "WHERE " + " AND ".join(conditions)
        
        
        sql = f"""
            SELECT 
                s.scientific_name,
                s.common_name_en,
                s.common_name_uk,
                COUNT(d.detection_id) as detection_count
            {from_clause}
            {where_clause}
            GROUP BY s.species_id, s.scientific_name, s.common_name_en, s.common_name_uk
            HAVING COUNT(d.detection_id) >= :min_detections
            ORDER BY detection_count DESC
        """
        
        db_result = conn.execute(text(sql), params).mappings().fetchall()
        
        ranking = []
        for i, row in enumerate(db_result, 1):
            display_name = row['scientific_name']
            if lang_code == 'uk' and row['common_name_uk']:
                display_name = f"{row['common_name_uk']} ({row['scientific_name']})"
            elif lang_code == 'en' and row['common_name_en']:
                display_name = f"{row['common_name_en']} ({row['scientific_name']})"
            
            ranking.append({
                'rank': i,
                'scientific_name': row['scientific_name'],
                'display_name': display_name,
                'detection_count': row['detection_count']
            })
        
        return ranking
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_species_ranking): {e}", exc_info=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_error:
                current_app.logger.error(f"Error closing connection: {close_error}")

def get_overview_statistics(lang_code, start_date=None, end_date=None, confidence=0.75, min_detections=1, location_ids=None, biotope_ids=None, tax_filters=None, institution_id=None, mode='birdnet', model_id=None):
    """Return overall statistics for the overview page, filtered by locations and biotopes."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=365)
        
        params = {
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj,
            'min_detections': min_detections
        }

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)
        mode = _normalize_model_mode(mode, model_id, params)

        # Build dynamic query parts.
        joins = ""
        conditions = []

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids

        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids

        if tax_filters:
            for key, value in tax_filters.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value

        condition_sql = (" AND " + " AND ".join(conditions)) if conditions else ""

        access_conditions = []
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            user_roles = [role.name for role in current_user.roles]

            if 'admin' not in user_roles:
                access_conditions.append("(s.required_role IS NULL OR s.required_role IN :user_roles)")
                params['user_roles'] = tuple(user_roles)
        else:
            access_conditions.append("s.required_role IS NULL")

        # Build access_filter string to append to SQL queries.
        # Empty string for admins (no access conditions).
        access_filter = ("WHERE " + " AND ".join(access_conditions)) if access_conditions else ""
        
        # Total detections.
        total_detections_sql = f"""
            SELECT COUNT(r.recording_id)
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN species s ON d.species_id = s.species_id
            JOIN locations l ON r.location_id = l.location_id
            {joins}
            {access_filter}
            AND {_confidence_filter_sql(mode, model_id)}
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
            AND {inst_condition}
            {condition_sql}
        """
        total_detections = conn.execute(text(total_detections_sql), params).scalar() or 0
        
        # Unique species count.
        unique_species_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT s.species_id
                FROM species s
                JOIN detections d ON s.species_id = d.species_id
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN locations l ON r.location_id = l.location_id
                {joins}
                {access_filter}
                AND {_confidence_filter_sql(mode, model_id)}
                AND DATE(r.datetime_start) >= :start_date
                AND DATE(r.datetime_start) <= :end_date
                AND {inst_condition}
                {condition_sql}
                GROUP BY s.species_id
                HAVING COUNT(r.recording_id) >= :min_detections
            ) as filtered_species
        """
        unique_species = conn.execute(text(unique_species_sql), params).scalar() or 0
        
        # Locations count.
        locations_count_sql = f"""
            SELECT COUNT(DISTINCT l.location_id)
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            JOIN species s ON d.species_id = s.species_id
            {joins}
            {access_filter}
            AND {_confidence_filter_sql(mode, model_id)}
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
            AND {inst_condition}
            {condition_sql}
        """
        locations_count = conn.execute(text(locations_count_sql), params).scalar() or 0

        active_days_sql = f"""
            SELECT COUNT(DISTINCT DATE(r.datetime_start))
            FROM recordings r
            JOIN locations l ON r.location_id = l.location_id
            WHERE {inst_condition}
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
        """
        active_days_count = conn.execute(text(active_days_sql), params).scalar() or 0
        
        return {
            'total_detections': total_detections,
            'unique_species': unique_species,
            'locations_count': locations_count,
            'period_days': active_days_count
        }
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_overview_statistics): {e}")
        return {'total_detections': 0, 'unique_species': 0, 'locations_count': 0, 'period_days': 0}
    finally:
        if conn:
            conn.close()

def get_locations_for_map(lang_code, start_date=None, end_date=None, confidence=0.75, location_ids=None, biotope_ids=None, min_detections=1, tax_filters=None, institution_id=None, mode='birdnet', model_id=None):
    """Return location data for the map, filtered by locations, biotopes, and min detections."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        end_date_obj = date.fromisoformat(end_date) if end_date else date.today()
        start_date_obj = date.fromisoformat(start_date) if start_date else end_date_obj - timedelta(days=365)
        
        params = {
            'confidence': confidence,
            'start_date': start_date_obj,
            'end_date': end_date_obj,
            'min_detections': min_detections
        }

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)
        mode = _normalize_model_mode(mode, model_id, params)

        joins = ""
        conditions = []

        if location_ids:
            conditions.append("l.location_id = ANY(:location_ids)")
            params['location_ids'] = location_ids

        if biotope_ids:
            joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
            conditions.append("lb.biotope_id = ANY(:biotope_ids)")
            params['biotope_ids'] = biotope_ids

        if tax_filters:
            for key, value in tax_filters.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value

        condition_sql = (" AND " + " AND ".join(conditions)) if conditions else ""

        access_conditions = []
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            user_roles = [role.name for role in current_user.roles]

            if 'admin' not in user_roles:
                access_conditions.append("(s.required_role IS NULL OR s.required_role IN :user_roles)")
                params['user_roles'] = tuple(user_roles)
        else:
            access_conditions.append("s.required_role IS NULL")

        # Build access_filter to append to SQL queries.
        access_filter = ("WHERE " + " AND ".join(access_conditions)) if access_conditions else ""
        
        sql = f"""
            SELECT 
                l.location_id,
                l.location_name,
                l.location_name_en,
                l.lat,
                l.lon,
                COUNT(d.detection_id) as detection_count, -- Changed from r.recording_id for accuracy
                COUNT(DISTINCT s.species_id) as species_count
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            JOIN species s ON d.species_id = s.species_id
            {joins}
            {access_filter}
            AND {_confidence_filter_sql(mode, model_id)}
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
            AND {inst_condition}
            {condition_sql}
            GROUP BY l.location_id, l.location_name, l.location_name_en, l.lat, l.lon
            HAVING COUNT(d.detection_id) >= :min_detections -- <-- CHANGED
            ORDER BY detection_count DESC
        """
        
        db_result = conn.execute(text(sql), params).mappings().fetchall()
        
        locations = []
        for row in db_result:
            location_display_name = row['location_name']
            if lang_code == 'en' and row['location_name_en']:
                location_display_name = row['location_name_en']

            locations.append({
                'location_id': row['location_id'],
                'location_name': location_display_name,
                'lat': float(row['lat']),
                'lon': float(row['lon']),
                'detection_count': row['detection_count'],
                'species_count': row['species_count']
            })
        
        return locations
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_locations_for_map): {e}")
        return []
    finally:
        if conn:
            conn.close()

def generate_spectrogram_image(audio_path, spectrogram_type='linear', force_regenerate=False):
    """
    Generate a spectrogram (Mel or Linear) with fixed margins for JS sync.

    Args:
        audio_path (str): Path to the audio file.
        spectrogram_type (str): 'mel' (default) or 'linear'.
        force_regenerate (bool): Whether to overwrite an existing file.
    """
    try:
        base_path, _ = os.path.splitext(audio_path)
        spectrogram_path = f"{base_path}.png"

        if not force_regenerate and os.path.exists(spectrogram_path):
            return True

        if not os.path.exists(audio_path):
            print(f"Audio file not found: {audio_path}")
            return False

        # 1. Load audio.
        y, sr = librosa.load(audio_path, sr=None)
        duration = librosa.get_duration(y=y, sr=sr)
        
        # 2. Canvas setup (HD DPI).
        fig_width = max(6, duration * 1.2)
        fig = plt.figure(figsize=(fig_width, 3), dpi=100)
        
        # Hard-coded margins to sync with the JS player.
        ax = fig.add_axes([0.12, 0.20, 0.864, 0.75])
        
        # Shared settings for HD quality.
        hop_length = 256  # Small hop for smooth time resolution.
        fmax = 8000        # Upper frequency cap.
        vmin = -70         # Silence threshold (suppresses background noise).

        S_DB = None
        y_axis_mode = 'mel'

        # 3. Spectrogram type selection.
        if spectrogram_type == 'linear':
            # --- LINEAR (STFT) ---
            # High n_fft for detailed thin frequency lines.
            n_fft = 4096 
            D = librosa.stft(y, n_fft=n_fft, hop_length=hop_length)
            S_DB = librosa.amplitude_to_db(np.abs(D), ref=np.max)
            y_axis_mode = 'linear'
            
        else:
            # --- MEL (default) ---
            # n_mels=256 for high vertical resolution.
            n_fft = 2048
            n_mels = 256
            S = librosa.feature.melspectrogram(
                y=y, sr=sr, n_fft=n_fft, hop_length=hop_length, 
                n_mels=n_mels, fmax=fmax
            )
            S_DB = librosa.power_to_db(S, ref=np.max)
            y_axis_mode = 'mel'

        # 4. Render.
        librosa.display.specshow(
            S_DB, 
            sr=sr, 
            hop_length=hop_length, 
            x_axis='time', 
            y_axis=y_axis_mode,  # Automatically selects the correct scale.
            ax=ax, 
            fmax=fmax, 
            cmap='magma',         # High-contrast colour map.
            vmin=vmin
        )
        
        # Axis formatting.
        ax.set_xlabel("sec")
        ax.set_ylabel("kHz")
        
        # Formatter to convert Hz → kHz (works for both linear and mel).
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: '{:.1f}'.format(x/1000)))

        # Save.
        plt.savefig(spectrogram_path, transparent=False)
        plt.close(fig)

        return True

    except Exception as e:
        print(f"Error generating spectrogram: {e}")
        return False

def get_occurrence_data(filters, limit=None):
    """Fetch occurrence data, optimised with UNION ALL for Smart Filter and timestamp index comparisons."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # 1. Fetch filter parameters.
        export_mode = filters.get('export_mode', 'standard')
        agg_type = filters.get('aggregation', 'none')
        try:
            agg_minutes = int(filters.get('aggregation_minutes', 60))
        except (ValueError, TypeError):
            agg_minutes = 60

        # --- DATE OPTIMISATION ---
        # Use timestamp bounds so SQL can use the datetime_start index.
        start_date_str = filters.get('start_date')
        end_date_str = filters.get('end_date')
        
        params = {
            'start_ts': f"{start_date_str} 00:00:00",
            'end_ts': f"{end_date_str} 23:59:59",
            # Smart Filter: ignore the confidence slider (set to 0), otherwise use its value.
            'confidence': 0.0 if export_mode == 'smart_filter' else float(filters.get('confidence', 0))
        }

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        # Multi-select institution filter from UI (list of ints or None/empty)
        selected_inst_ids = filters.get('institution_ids') or None
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids
        )
        params.update(inst_params)

        # --- TAXONOMIC FILTERS ---
        taxo_conditions = []
        if filters.get('species_ids'):
            taxo_conditions.append("s.species_id IN :species_ids")
            params['species_ids'] = tuple(filters['species_ids'])
        elif filters.get('genus'):
            taxo_conditions.append("s.genus = :genus")
            params['genus'] = filters['genus']
        elif filters.get('family'):
            taxo_conditions.append("s.family = :family")
            params['family'] = filters['family']
        elif filters.get('order'):
            taxo_conditions.append("s.order_rank = :order")
            params['order'] = filters['order']
        elif filters.get('class'):
            taxo_conditions.append("s.class = :class")
            params['class'] = filters['class']

        taxo_where = " AND " + " AND ".join(taxo_conditions) if taxo_conditions else ""

        # --- CTE: VERIFIERS ---
        # Used to retrieve user names.
        cte_verifiers = """
            WITH Verifiers AS (
                SELECT segment_id, STRING_AGG(user_id::text, '|') as verifier_user_ids
                FROM segment_verifications GROUP BY segment_id
            )
        """

        # Column list, identical for all parts of the UNION.
        select_columns = """
            d.detection_id, d.confidence, 
            s.scientific_name, s.kingdom, s.phylum, s.class, s.order_rank, s.family, s.genus, s.establishment_means, 
            r.datetime_start, r.recording_id, r.location_id,
            l.lat, l.lon, l.location_name, l.state_province,
            dvm.verification_result as consensus_result, dvm.positive_votes,
            eval.p0_95_threshold
        """

        base_sql = ""

        # =========================================================================
        # SQL QUERY CONSTRUCTION LOGIC
        # =========================================================================
        
        if export_mode == 'smart_filter':
            # --- SMART FILTER MODE (OPTIMISED) ---
            # Split into two queries:
            # 1. Clearly verified (fast path via dvm).
            # 2. Smart auto (fast path via evaluation + detections).
            
            # Part 1: Verified only.
            sql_part_verified = f"""
                SELECT {select_columns}, v.verifier_user_ids
                FROM detection_verification_map dvm
                JOIN detections d ON dvm.detection_id = d.detection_id
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN species s ON d.species_id = s.species_id
                JOIN locations l ON r.location_id = l.location_id
                LEFT JOIN segments seg ON dvm.segment_id = seg.id
                LEFT JOIN Verifiers v ON seg.id = v.segment_id
                LEFT JOIN evaluation eval ON s.species_id = eval.species_id AND eval.is_current = TRUE
                WHERE 
                    r.datetime_start BETWEEN :start_ts AND :end_ts
                    AND (dvm.verification_result = 1 OR dvm.positive_votes >= 1) -- Positive verification
                    AND (dvm.verification_result IS NULL OR dvm.verification_result != 0) -- Not rejected
                    AND {inst_condition}
                    {taxo_where}
            """

            # Part 2: High confidence auto-detections only.
            # Explicitly exclude rows already in dvm (to avoid duplicates with Part 1).
            sql_part_smart = f"""
                SELECT {select_columns}, NULL::text as verifier_user_ids
                FROM detections d
                JOIN evaluation eval ON d.species_id = eval.species_id AND eval.is_current = TRUE
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN species s ON d.species_id = s.species_id
                JOIN locations l ON r.location_id = l.location_id
                LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
                WHERE 
                    r.datetime_start BETWEEN :start_ts AND :end_ts
                    AND {inst_condition}
                    {taxo_where}
                    -- Smart filter conditions:
                    AND eval.total_samples >= 200
                    AND d.confidence >= eval.p0_95_threshold
                    -- Exclude rows that have any verification history (they are either in Part 1 or rejected)
                    AND (dvm.detection_id IS NULL OR (
                        (dvm.verification_result IS NULL OR dvm.verification_result = 0)
                        AND (dvm.positive_votes IS NULL OR dvm.positive_votes = 0)
                    ))
                    -- Safeguard: do not show explicitly rejected ones
                    AND (dvm.verification_result IS NULL OR dvm.verification_result != 0)
            """

            # Merge the two parts.
            base_sql = f"{sql_part_verified} UNION ALL {sql_part_smart}"

        else:
            # --- STANDARD MODES ---
            mode_condition = ""
            if export_mode == 'verified_only':
                mode_condition = " AND dvm.positive_votes >= 1 AND (dvm.verification_result != 0 OR dvm.verification_result IS NULL)"
            elif export_mode == 'compleated_only':
                mode_condition = " AND dvm.verification_result = 1"
            else:
                # Standard / Combined
                mode_condition = " AND (dvm.verification_result = 1 OR dvm.verification_result IS NULL)"

            base_sql = f"""
                SELECT {select_columns}, v.verifier_user_ids
                FROM detections d
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN species s ON d.species_id = s.species_id
                JOIN locations l ON r.location_id = l.location_id
                LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
                LEFT JOIN segments seg ON dvm.segment_id = seg.id
                LEFT JOIN Verifiers v ON seg.id = v.segment_id
                LEFT JOIN evaluation eval ON s.species_id = eval.species_id AND eval.is_current = TRUE
                WHERE r.datetime_start BETWEEN :start_ts AND :end_ts
                  AND {inst_condition}
                  AND d.confidence >= :confidence
                  {mode_condition}
                  {taxo_where}
            """

        # =========================================================================
        # AGGREGATION AND LIMITS
        # =========================================================================
        
        final_sql = ""
        count_sql = ""

        # Apply aggregation if requested (one record per day/hour).
        if agg_type in ['location_day', 'location_time']:
            if agg_type == 'location_day':
                partition_expr = "DATE(datetime_start)"
            else:
                params['agg_seconds'] = agg_minutes * 60
                partition_expr = "FLOOR(EXTRACT(EPOCH FROM datetime_start) / :agg_seconds)"

            # Wrap base_sql in a CTE for ranking.
            # The UNION result becomes the data source.
            final_sql = f"""
                {cte_verifiers},
                RawData AS ({base_sql}),
                RankedData AS (
                    SELECT *,
                        ROW_NUMBER() OVER(
                            PARTITION BY scientific_name, location_id, {partition_expr}
                            ORDER BY 
                                CASE WHEN verifier_user_ids IS NOT NULL THEN 0 ELSE 1 END ASC, -- Verified first
                                confidence DESC -- Then highest confidence
                        ) as rn
                    FROM RawData
                )
                SELECT * FROM RankedData WHERE rn = 1 ORDER BY datetime_start
            """
            
            # Count query for aggregated mode.
            count_sql = f"""
                {cte_verifiers},
                RawData AS ({base_sql}),
                RankedData AS (
                    SELECT 
                        ROW_NUMBER() OVER(
                            PARTITION BY scientific_name, location_id, {partition_expr}
                        ) as rn
                    FROM RawData
                )
                SELECT COUNT(*) FROM RankedData WHERE rn = 1
            """
        else:
            # No aggregation — return data as-is.
            final_sql = f"{cte_verifiers} {base_sql} ORDER BY datetime_start"
            count_sql = f"{cte_verifiers} SELECT COUNT(*) FROM ({base_sql}) as total_rows"

        # =========================================================================
        # QUERY EXECUTION
        # =========================================================================
        
        # 1. Count total rows.
        total_count = conn.execute(text(count_sql), params).scalar() or 0
        
        # 2. Fetch data (with limit if requested).
        if limit:
            final_sql += " LIMIT :limit"
            params['limit'] = limit
            
        db_result = conn.execute(text(final_sql), params).mappings().fetchall()
        
        # =========================================================================
        # POST-PROCESSING (map users & format CSV dicts)
        # =========================================================================
        
        # Collect user IDs for name mapping.
        all_user_ids = set()
        for row in db_result:
            if row['verifier_user_ids']:
                ids = [int(uid) for uid in row['verifier_user_ids'].split('|')]
                all_user_ids.update(ids)
        
        user_map = {}
        if all_user_ids:
            users = User.query.filter(User.id.in_(list(all_user_ids))).all()
            user_map = {u.id: u.full_name for u in users}

        institution_code = filters.get('institution_code', 'RSNR')
        detector_name = filters.get('detector_name', 'BirdNET 2.4')
        occurrence_data = []
        
        for row in db_result:
            try: specific_epithet = row['scientific_name'].split(' ', 1)[1]
            except IndexError: specific_epithet = None

            verifier_ids_str = row['verifier_user_ids']
            has_positive_verification = verifier_ids_str is not None
            
            p95 = row.get('p0_95_threshold')
            
            if has_positive_verification:
                ids = [int(uid) for uid in verifier_ids_str.split('|')]
                names = [user_map.get(uid, f'User #{uid}') for uid in ids]
                identifiedBy = " | ".join(names)
                basisOfRecord = 'MachineObservation'
                identificationVerificationStatus = 'verified'
                identificationRemarks = f'Confirmed by human expert(s). Original {detector_name} confidence: {round(row["confidence"], 2)}'
            else:
                identifiedBy = detector_name
                basisOfRecord = 'MachineObservation'
                
                # Remarks logic for Smart Filter.
                if export_mode == 'smart_filter' and p95 and row['confidence'] >= p95:
                    identificationVerificationStatus = 'unverified'  # Technically still MachineObservation.
                    identificationRemarks = (f"High confidence detection by {detector_name} (Conf: {round(row['confidence'], 2)} "
                                             f">= Species 95% Threshold: {round(p95, 2)}).")
                else:
                    identificationVerificationStatus = 'unverified'
                    identificationRemarks = f"Automatic detection by {detector_name}, confidence: {round(row['confidence'], 2)}"

            occurrence_data.append({
                'occurrenceID': f"URN:acmon:{institution_code}:detection:{row['detection_id']}",
                'basisOfRecord': basisOfRecord,
                'identificationVerificationStatus': identificationVerificationStatus,
                'identifiedBy': identifiedBy,
                'identificationRemarks': identificationRemarks,
                'occurrenceStatus': 'present',
                'institutionCode': institution_code,
                'scientificName': row['scientific_name'], 'kingdom': row['kingdom'], 'phylum': row['phylum'],
                'class': row['class'], 'order': row['order_rank'], 'family': row['family'], 'genus': row['genus'],
                'specificEpithet': specific_epithet, 'establishmentMeans': row['establishment_means'],
                'eventDate': row['datetime_start'].strftime('%Y-%m-%d'),
                'eventTime': row['datetime_start'].strftime('%H:%M:%S'), 'countryCode': 'UA',
                'stateProvince': row['state_province'], 'locality': row['location_name'],
                'decimalLatitude': float(row['lat']), 'decimalLongitude': float(row['lon']),
                'geodeticDatum': 'WGS84',
                'coordinateUncertaintyInMeters': 20,
                'georeferenceSources': 'GPS (smartphone)',
                'recordedBy': 'Automated acoustic recording station'
            })
            
        return {'data': occurrence_data, 'total_count': total_count}

    except Exception as e:
        current_app.logger.error(f"Error getting occurrence data: {e}", exc_info=True)
        raise
    finally:
        if conn: conn.close()

# List of Open-Meteo parameters to fetch (easy to extend).
OPEN_METEO_PARAMS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",      # Mean temperature.
    "precipitation_sum",        # Precipitation.
    "wind_speed_10m_max",       # Wind gusts.
    "wind_speed_10m_mean",      # Mean wind speed.
    "relative_humidity_2m_mean",  # Relative humidity.
    "surface_pressure_mean"
]

def get_weather_data(start_date, end_date, lat, lon):
    """
    Fetch weather data from the geodata database or Open-Meteo.
    Caching logic:
    1. Data older than 7 days is treated as "historical" and is not refreshed.
    2. Data from the last 7 days is refreshed IF the last update was > 12 hours ago.
    3. Uses PostGIS for nearest-point fallback.
    """
    # --- Settings ---
    COORD_PRECISION = 2            # Coordinate precision (decimal places).
    STABILITY_THRESHOLD_DAYS = 7   # "Unstable" data period in days.
    CACHE_FRESHNESS_HOURS = 24     # How often to refresh unstable data (hours).
    # --------------------

    lat_fixed = round(float(lat), COORD_PRECISION)
    lon_fixed = round(float(lon), COORD_PRECISION)
    
    conn = None
    try:
        conn = get_geodata_db_connection()
        
        s_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        e_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        delta = e_date - s_date
        required_dates = {(s_date + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(delta.days + 1)}
        
        # Cut-off date after which data is considered archived and immutable.
        cutoff_date = (datetime.now() - timedelta(days=STABILITY_THRESHOLD_DAYS)).date()
        
        # Point in time after which recent data is considered stale and needs a refresh.
        refresh_cutoff_time = datetime.now() - timedelta(hours=CACHE_FRESHNESS_HOURS)

        # 2. Smart existence check.
        # Accept a cached row ONLY IF:
        # (A) it is old and stable (date <= cutoff_date)
        # OR
        # (B) it is recent AND was updated very recently (updated_at >= refresh_cutoff_time).
        existing_valid_query = text("""
            SELECT date FROM weather.daily_archive 
            WHERE latitude = :lat AND longitude = :lon 
            AND date BETWEEN :start_date AND :end_date
            AND (
                date <= :cutoff_date
                OR
                updated_at >= :refresh_time
            )
        """)
        
        result = conn.execute(existing_valid_query, {
            'lat': lat_fixed, 
            'lon': lon_fixed,
            'start_date': start_date, 
            'end_date': end_date,
            'cutoff_date': cutoff_date,
            'refresh_time': refresh_cutoff_time
        }).fetchall()
        
        existing_dates = {row[0].strftime('%Y-%m-%d') for row in result}
        missing_dates = required_dates - existing_dates
        
        # 3. Try to refresh via API (only for missing/stale dates).
        if missing_dates:
            try:
                current_app.logger.info(f"Weather: Fetching {len(missing_dates)} dates (expired or missing) for ({lat_fixed}, {lon_fixed})")
                
                url = "https://archive-api.open-meteo.com/v1/archive"
                api_params = {
                    "latitude": lat_fixed,
                    "longitude": lon_fixed,
                    "start_date": start_date,
                    "end_date": end_date,
                    "daily": OPEN_METEO_PARAMS,
                    "timezone": "auto"
                }
                
                response = requests.get(url, params=api_params, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    daily = data.get('daily', {})
                    time_list = daily.get('time', [])
                    
                    values_to_insert = []
                    
                    for i, date_str in enumerate(time_list):
                        # Insert only dates absent from the "valid" list
                        # (brand-new dates or stale ones with an old updated_at).
                        if date_str in existing_dates:
                            continue
                            
                        metrics_json = {}
                        for param in OPEN_METEO_PARAMS:
                            val = daily.get(param)
                            if val and len(val) > i and val[i] is not None:
                                metrics_json[param] = val[i]
                        
                        values_to_insert.append({
                            'date': date_str,
                            'latitude': lat_fixed,
                            'longitude': lon_fixed,
                            'metrics': metrics_json
                        })
                    
                    # 4. Upsert into the database.
                    if values_to_insert:
                        weather_table = table("daily_archive",
                            column("date"),
                            column("latitude"),
                            column("longitude"),
                            column("metrics", JSONB),
                            column("updated_at"),
                            schema="weather"
                        )
                        
                        stmt = insert(weather_table).values(values_to_insert)
                        
                        do_update_stmt = stmt.on_conflict_do_update(
                            constraint='pk_weather_archive',
                            set_={
                                'metrics': stmt.excluded.metrics,
                                'updated_at': datetime.now()  # Refresh timestamp to extend cache life.
                            }
                        )
                        conn.execute(do_update_stmt)
                        conn.commit()
                else:
                    current_app.logger.error(f"Open-Meteo API Error {response.status_code}")
            
            except Exception as api_err:
                current_app.logger.error(f"Weather API Connection Failed: {api_err}")

        # 5. Final data fetch.
        final_query = text("""
            SELECT date, metrics
            FROM weather.daily_archive
            WHERE latitude = :lat AND longitude = :lon 
            AND date BETWEEN :start_date AND :end_date
            ORDER BY date
        """)
        
        final_result = conn.execute(final_query, {
            'lat': lat_fixed, 
            'lon': lon_fixed,
            'start_date': start_date, 
            'end_date': end_date
        }).mappings().fetchall()
        
        # PostGIS fallback (if the API failed and data is incomplete).
        expected_days = (e_date - s_date).days + 1
        
        if len(final_result) < expected_days:
            current_app.logger.warning(f"Weather: Exact match incomplete. Using PostGIS fallback.")
            
            fallback_search_query = text("""
                SELECT latitude, longitude
                FROM weather.daily_archive
                WHERE date BETWEEN :start_date AND :end_date
                GROUP BY latitude, longitude, geom
                ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) ASC
                LIMIT 1
            """)
            
            nearest_point = conn.execute(fallback_search_query, {
                'lat': lat_fixed,
                'lon': lon_fixed,
                'start_date': start_date,
                'end_date': end_date
            }).fetchone()
            
            if nearest_point:
                fallback_lat, fallback_lon = nearest_point.latitude, nearest_point.longitude
                current_app.logger.info(f"Weather Fallback: Found data at ({fallback_lat}, {fallback_lon})")
                
                final_result = conn.execute(final_query, {
                    'lat': fallback_lat, 
                    'lon': fallback_lon,
                    'start_date': start_date, 
                    'end_date': end_date
                }).mappings().fetchall()

        # Format results.
        processed_data = []
        for row in final_result:
            item = {'date': row['date'].strftime('%Y-%m-%d')}
            if row['metrics']:
                item.update(row['metrics'])
            processed_data.append(item)
            
        return processed_data
        
    except Exception as e:
        current_app.logger.error(f"Weather Util Critical Error: {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()


# ── Location coverage calendar (Idea 10 / #37) ───────────────────────────────
#
# Day coverage metric = SUM OF RECORDING DURATIONS per day (recordings.duration_minutes
# / 60 = actual recording hours). All existing recordings are 5 min (default 5);
# duration is set in the pam/import form at import time.

# "Good" threshold in actual recording hours per day (agreed with the user).
COVERAGE_GOOD_HOURS = 6  # ≥6 h/day = good
# No cap applied: a location can have multiple receivers recording in parallel,
# so the daily total may legitimately exceed 24 hours.


def _coverage_level(hours_recorded):
    """Return calendar cell category based on total recording hours for the day."""
    if not hours_recorded or hours_recorded <= 0:
        return 'missing'
    if hours_recorded >= COVERAGE_GOOD_HOURS:
        return 'good'
    return 'partial'


def _apply_coverage_intensity(months, value_of, include):
    """Set cell['intensity'] ∈ [0,1] by linear scaling from min to max value
    (#43, gradient shading). include(cell) determines whether the cell is in
    the scale; otherwise intensity=None (neutral/grey). All equal → intensity=1.0.
    """
    vals = [value_of(c) for mo in months for wk in mo['weeks']
            for c in wk if c and include(c)]
    if vals:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
    else:
        lo, hi, span = 0, 0, 1.0
    for mo in months:
        for wk in mo['weeks']:
            for c in wk:
                if not c:
                    continue
                c['intensity'] = ((value_of(c) - lo) / span) if include(c) else None


def build_coverage_calendar(day_data, mode='all'):
    """Convert {date: {'count': int, 'minutes': float}} to a month-by-month calendar.

    `minutes` — sum of recording durations per day (recordings.duration_minutes);
    `count`   — number of recordings. Actual hours = minutes/60. Pure function.

    mode='all' (default): all years, month by month, for the full date range.
    mode='aggregated': one synthetic year (12 months); each (month, day) sums
        values across ALL years and counts years with data (cell['years']).

    cell = {'day', 'date', 'count', 'hours', 'level', ['years' in aggregated mode]}.
    """
    # Filter out records with no date (DATE(NULL)=None key breaks date sorting).
    day_data = {d: v for d, v in (day_data or {}).items() if d is not None}

    if not day_data:
        return {'months': [], 'total_recordings': 0, 'total_hours': 0.0,
                'active_days': 0, 'day_range': None, 'mode': mode}

    total_recordings = sum(v.get('count', 0) for v in day_data.values())
    cal = calendar.Calendar(firstweekday=0)  # 0 = Monday

    if mode == 'aggregated':
        # Roll up by (month, day) across all years.
        agg = {}  # (m, d) -> {'minutes', 'count', 'years': set}
        for dt, v in day_data.items():
            a = agg.setdefault((dt.month, dt.day),
                               {'minutes': 0.0, 'count': 0, 'years': set()})
            a['minutes'] += float(v.get('minutes', 0) or 0)
            a['count'] += v.get('count', 0)
            a['years'].add(dt.year)
        months = []
        total_hours = 0.0
        # Use synthetic year 2000 (leap year — so February 29 exists).
        for m in range(1, 13):
            weeks = []
            for week in cal.monthdatescalendar(2000, m):
                row = []
                for d in week:
                    if d.month != m:
                        row.append(None)
                        continue
                    a = agg.get((m, d.day))
                    minutes = a['minutes'] if a else 0.0
                    hours = round(minutes / 60.0, 1)
                    total_hours += hours
                    row.append({'day': d.day, 'date': d,
                                'count': a['count'] if a else 0,
                                'hours': hours,
                                'years': len(a['years']) if a else 0,
                                'level': _coverage_level(hours)})
                weeks.append(row)
            months.append({'year': 2000, 'month': m, 'label': f'{m:02d}',
                           'weeks': weeks})
        _apply_coverage_intensity(months, lambda c: c['hours'], lambda c: c['hours'] > 0)
        years_all = sorted({dt.year for dt in day_data})
        return {
            'months': months, 'total_recordings': total_recordings,
            'total_hours': round(total_hours, 1), 'active_days': len(day_data),
            'day_range': (min(day_data), max(day_data)), 'mode': 'aggregated',
            'years': years_all,
        }

    # mode == 'all'
    days = sorted(day_data)
    first, last = days[0], days[-1]
    months = []
    total_hours = 0.0
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for d in week:
                if d.month != m:
                    row.append(None)  # Day from adjacent month — leave empty.
                    continue
                info = day_data.get(d)
                cnt = info.get('count', 0) if info else 0
                minutes = float(info.get('minutes', 0) or 0) if info else 0.0
                # Total recording hours per day (NO 24-hour cap — multiple receivers
                # at a location can legally sum to >24 h).
                hours = round(minutes / 60.0, 1)
                total_hours += hours
                row.append({'day': d.day, 'date': d, 'count': cnt,
                            'hours': hours, 'level': _coverage_level(hours)})
            weeks.append(row)
        months.append({'year': y, 'month': m, 'label': f'{y}-{m:02d}',
                       'weeks': weeks})
        m += 1
        if m > 12:
            m, y = 1, y + 1

    _apply_coverage_intensity(months, lambda c: c['hours'], lambda c: c['hours'] > 0)
    return {
        'months': months,
        'total_recordings': total_recordings,
        'total_hours': round(total_hours, 1),
        'active_days': len(day_data),
        'day_range': (first, last),
        'mode': 'all',
    }