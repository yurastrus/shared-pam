# myproject/app/pam/utils.py

import calendar
import csv
import io
import math
import os
import threading
from datetime import date, datetime, timedelta, timezone

# Сторонні бібліотеки (Data Science & Audio)
import librosa
import librosa.display
import numpy as np
import pandas as pd
import requests
from suncalc import get_times

# Matplotlib (налаштування бекенду має йти перед імпортом pyplot)
import matplotlib
matplotlib.use('Agg')  # Запобігає спробам відкрити GUI на сервері
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# Flask та розширення
from flask import current_app
from flask_login import current_user

# SQLAlchemy
from sqlalchemy import create_engine, table, text, column
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy.pool import QueuePool, StaticPool

# Локальні модулі
from app.models import User

# Глобальна змінна для зберігання engine
_pam_engine = None
_engine_lock = threading.Lock()

_geodata_engine = None
_geodata_lock = threading.Lock()

def get_pam_engine():
    """
    Створює та повертає SQLAlchemy engine з правильно налаштованим пулом з'єднань.
    Використовує singleton pattern для уникнення створення множинних engine.
    """
    global _pam_engine
    
    if _pam_engine is None:
        with _engine_lock:
            if _pam_engine is None:
                _pam_engine = create_engine(
                    current_app.config['PAM_DATABASE_URI'],
                    poolclass=QueuePool,
                    pool_size=5,          # Максимум 5 з'єднань у пулі
                    max_overflow=10,      # Максимум 10 додаткових з'єднань
                    pool_timeout=30,      # Таймаут очікування з'єднання
                    pool_recycle=300,    # Оновлення з'єднань кожні 30 хвилин
                    pool_pre_ping=True,   # Перевірка з'єднань перед використанням
                    echo=False
                )
    
    return _pam_engine

def get_pam_db_connection():
    """
    Створює та повертає з'єднання до бази даних PAM.
    Тепер використовує пул з'єднань.
    """
    engine = get_pam_engine()
    return engine.connect()

def get_geodata_engine():
    """Створює Singleton engine для бази геоданих."""
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
    """Повертає з'єднання до бази GEODATA."""
    engine = get_geodata_engine()
    return engine.connect()

def get_institution_filter(user_inst_ids=None, is_admin=False, selected_inst_id=None):
    """
    Генерує SQL-умову для фільтрації за правами доступу ТА вибраними установами.
    selected_inst_id — може бути рядком '1,2,3', числом 2, або списком [1, 2]
    """
    # 1. Базова умова прав доступу
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

    # 2. Додаткова умова вибраних установ (якщо вибрані у фільтрі)
    if selected_inst_id:
        # Нормалізуємо до списку int
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
    Спрощений розрахунок часу сходу та заходу сонця.
    ВИПРАВЛЕНО: Додано конвертацію в часовий пояс UTC+2.
    """
    try:
        # Константи
        CIVIL_ZENITH = 90.833
        
        # День року
        day_of_year = date_obj.timetuple().tm_yday
        
        # Розрахунок сонячного схилення
        P = math.asin(.39795 * math.cos(.98563 * (day_of_year - 173) * math.pi / 180))
        
        # Розрахунок для даної широти
        lat_rad = latitude * math.pi / 180
        
        # Розрахунок кута годин
        argument = -math.tan(lat_rad) * math.tan(P)
        
        if argument < -1:
            argument = -1
        elif argument > 1:
            argument = 1
            
        hour_angle = 24 * math.acos(argument) / (2 * math.pi)
        
        # Розрахунок часів в годинах (відносно UTC)
        sunrise_hour = 12 - hour_angle - longitude / 15
        sunset_hour = 12 + hour_angle - longitude / 15

        # 1. Створення "наївних" datetime об'єктів в UTC
        utc_sunrise = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=sunrise_hour)
        utc_sunset = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=sunset_hour)

        # 2. Визначення цільового часового поясу (UTC+2)
        kyiv_tz = timezone(timedelta(hours=2))

        # 3. Створення "свідомих" (aware) datetime об'єктів шляхом встановлення UTC як початкового поясу
        aware_utc_sunrise = utc_sunrise.replace(tzinfo=timezone.utc)
        aware_utc_sunset = utc_sunset.replace(tzinfo=timezone.utc)

        # 4. Конвертація в цільовий часовий пояс
        local_sunrise = aware_utc_sunrise.astimezone(kyiv_tz)
        local_sunset = aware_utc_sunset.astimezone(kyiv_tz)
        
        return {
            'sunrise': local_sunrise,
            'sunset': local_sunset
        }
        
    except Exception as e:
        current_app.logger.error(f"Error calculating sun times: {e}")
        # Fallback значення (також тепер з часовим поясом)
        kyiv_tz = timezone(timedelta(hours=2))
        sunrise_time = (datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=6)).replace(tzinfo=kyiv_tz)
        sunset_time = (datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=18)).replace(tzinfo=kyiv_tz)
        return {
            'sunrise': sunrise_time,
            'sunset': sunset_time
        }

def get_available_species(lang_code):
    """
    Повертає список видів з pam_db, доступних поточному користувачеві.
    ВИПРАВЛЕНО: тепер з правильним керуванням з'єднаннями.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        base_query_select = "SELECT scientific_name, common_name_en, common_name_uk, required_role FROM species"
        query_filter = "WHERE required_role IS NULL"
        params = {}
        
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            # Отримуємо список назв усіх ролей користувача
            user_roles = [role.name for role in current_user.roles]
            
            # 3. Адміністратор бачить абсолютно все, це винятковий випадок
            if 'admin' in user_roles:
                query_filter = ""  # Знімаємо будь-які обмеження
            
            # 4. Для інших користувачів з ролями будуємо динамічний фільтр
            elif user_roles: # Перевіряємо, чи список ролей не порожній
                query_filter = "WHERE required_role IS NULL OR required_role IN :roles"
                # SQLAlchemy/Psycopg2 коректно обробить список для оператора IN
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

def get_filtered_detections(species_name, start_date=None, end_date=None, confidence=0.0, location_ids=None, biotope_ids=None, institution_id=None):
    """
    ОНОВЛЕНО: Додано статус верифікації для кожної детекції.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        params = {'species_name': species_name, 'confidence': confidence}

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)
        
        joins = """
            JOIN recordings r ON d.recording_id = r.recording_id 
            JOIN locations l ON r.location_id = l.location_id
            LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
        """
        conditions = [
                    "s.scientific_name = :species_name",
                    "d.confidence >= :confidence",
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
                d.confidence,
                dvm.verification_result
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
                'verification_result': r['verification_result']
            } 
            for r in db_result if r['datetime_start']
        ]
        
    except Exception as e:
        current_app.logger.error(f"PAM DB Error (get_filtered_detections): {e}")
        return []
    finally:
        if conn is not None:
            conn.close()

def get_daily_detection_counts(species_name, start_date, end_date, confidence, location_ids=None, biotope_ids=None, excel_exp=False, institution_id=None):
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
        
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        joins = "JOIN recordings r ON d.recording_id = r.recording_id JOIN locations l ON r.location_id = l.location_id"
        conditions = [
            "s.scientific_name = :species_name",
            "d.confidence >= :confidence",
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

def get_time_scatter_data(species_name, start_date, end_date, confidence, location_ids=None, biotope_ids=None, excel_exp=False, institution_id=None):
    """
    ОНОВЛЕНО: Додано статус верифікації для кожної точки на графіку добової активності.
    """
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
        
        joins = """
            JOIN locations l ON r.location_id = l.location_id
            LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id
        """
        conditions = [
            "s.scientific_name = :species_name",
            "d.confidence >= :confidence",
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

        # 1. Визначаємо базові поля, які потрібні завжди
        select_fields = [
            "r.datetime_start",
            "l.lat",
            "l.lon",
            "dvm.verification_result"
        ]

        # 2. Додаємо додаткові поля, якщо треба
        if excel_exp:
            select_fields.append("d.confidence")

        # 3. Формуємо рядок (об'єднуємо через кому)
        columns_str = ", ".join(select_fields)

        # 4. Підставляємо в єдиний запит
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
            'verification_result': r['verification_result']
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

def get_species_summary(species_name, start_date=None, end_date=None, confidence=0.0, location_id=None, location_ids=None, biotope_ids=None, min_detections=1, institution_id=None):
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

        base_joins = """
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN species s ON d.species_id = s.species_id
            JOIN locations l ON r.location_id = l.location_id
        """
        base_conditions = f"""
            WHERE s.scientific_name = :species_name
              AND d.confidence >= :confidence
              AND DATE(r.datetime_start) >= :start_date
              AND DATE(r.datetime_start) <= :end_date
              AND {inst_condition}
        """

        # CTE для відбору локацій, що задовольняють умову min_detections
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

        # Загальна кількість детекцій по виду (вже на відфільтрованих локаціях)
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

        # Кількість унікальних локалітетів
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

        # Кількість днів з виявленням
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

        # Загальна кількість детекцій по всіх видах (для %)
        sql_all_species = f"""
            SELECT COUNT(*) as cnt
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            {joins}
            WHERE d.confidence >= :confidence
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

def get_unique_detection_points(lang_code, species_name, start_date=None, end_date=None, confidence=0.0, location_ids=None, biotope_ids=None, min_detections=1, institution_id=None):
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
            # Збираємо ID всіх установ, до яких належить користувач
            user_inst_ids = [inst.id for inst in current_user.institutions]
            is_admin = current_user.has_role('admin')

        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        params.update(inst_params)

        joins = "LEFT JOIN detection_verification_map dvm ON d.detection_id = dvm.detection_id"
        conditions = [
            "s.scientific_name = :species_name",
            "d.confidence >= :confidence",
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

def get_species_ranking(lang_code, start_date=None, end_date=None, confidence=0.0, min_detections=1, location_ids=None, biotope_ids=None, tax_filters=None, institution_id=None):
    """
    Повертає рейтингову таблицю видів з кількістю детекцій.
    ВИПРАВЛЕНО: Коректна фільтрація з використанням INNER JOIN.
    """
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
        
        # Базові JOIN'и, які потрібні завжди
        from_clause = """
            FROM species s
            JOIN detections d ON s.species_id = d.species_id
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
        """
        
        # Додаткові JOIN'и для фільтрів
        if biotope_ids:
            from_clause += " JOIN location_biotopes lb ON l.location_id = lb.location_id"

        # Формуємо умови WHERE
        conditions = [
            "d.confidence >= :confidence",
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

        # Фільтр доступу
        if current_user.is_authenticated and hasattr(current_user, 'roles'):
            user_roles = [role.name for role in current_user.roles]
            
            # Адміністратор бачить абсолютно все, тому для нього не додаємо жодних обмежень по ролях
            if 'admin' not in user_roles:
                # Для інших користувачів дозволяємо види без ролі АБО з роллю, яка є у користувача
                # SQLAlchemy коректно обробить :user_roles як список для оператора IN
                conditions.append("(s.required_role IS NULL OR s.required_role IN :user_roles)")
                params['user_roles'] = tuple(user_roles)
        else:
            # Для неавтентифікованих користувачів - тільки види без ролі
            conditions.append("s.required_role IS NULL")
        

        if tax_filters:
            for key, value in tax_filters.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value

        where_clause = "WHERE " + " AND ".join(conditions)
        
        # --- КІНЕЦЬ ЗМІНЕНОГО БЛОКУ ---
        
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

def get_overview_statistics(lang_code, start_date=None, end_date=None, confidence=0.75, min_detections=1, location_ids=None, biotope_ids=None, tax_filters=None, institution_id=None):
    """
    Повертає загальну статистику для overview сторінки.
    ОНОВЛЕНО: враховує фільтри по локаціях та біотопах.
    """
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
        
        # Формуємо динамічні частини запиту
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

        # Формуємо рядок access_filter, який буде додано до SQL-запитів
        # Якщо access_conditions порожній (для адміна), то і фільтр буде порожнім
        access_filter = ("WHERE " + " AND ".join(access_conditions)) if access_conditions else ""
        
        # Загальна кількість детекцій
        total_detections_sql = f"""
            SELECT COUNT(r.recording_id)
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN species s ON d.species_id = s.species_id
            JOIN locations l ON r.location_id = l.location_id
            {joins}
            {access_filter}
            AND d.confidence >= :confidence
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
            AND {inst_condition}
            {condition_sql}
        """
        total_detections = conn.execute(text(total_detections_sql), params).scalar() or 0
        
        # Кількість унікальних видів
        unique_species_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT s.species_id
                FROM species s
                JOIN detections d ON s.species_id = d.species_id
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN locations l ON r.location_id = l.location_id
                {joins}
                {access_filter}
                AND d.confidence >= :confidence
                AND DATE(r.datetime_start) >= :start_date
                AND DATE(r.datetime_start) <= :end_date
                AND {inst_condition}
                {condition_sql}
                GROUP BY s.species_id
                HAVING COUNT(r.recording_id) >= :min_detections
            ) as filtered_species
        """
        unique_species = conn.execute(text(unique_species_sql), params).scalar() or 0
        
        # Кількість локацій
        locations_count_sql = f"""
            SELECT COUNT(DISTINCT l.location_id)
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            JOIN species s ON d.species_id = s.species_id
            {joins}
            {access_filter}
            AND d.confidence >= :confidence
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

def get_locations_for_map(lang_code, start_date=None, end_date=None, confidence=0.75, location_ids=None, biotope_ids=None, min_detections=1, tax_filters=None, institution_id=None):
    """
    Повертає дані локацій для відображення на карті.
    ОНОВЛЕНО: враховує фільтри по локаціях, біотопах та мінімальній кількості детекцій.
    """
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

        # Формуємо рядок access_filter, який буде додано до SQL-запитів
        access_filter = ("WHERE " + " AND ".join(access_conditions)) if access_conditions else ""
        
        sql = f"""
            SELECT 
                l.location_id,
                l.location_name,
                l.location_name_en,
                l.lat,
                l.lon,
                COUNT(d.detection_id) as detection_count, -- Змінено з r.recording_id для точності
                COUNT(DISTINCT s.species_id) as species_count
            FROM detections d
            JOIN recordings r ON d.recording_id = r.recording_id
            JOIN locations l ON r.location_id = l.location_id
            JOIN species s ON d.species_id = s.species_id
            {joins}
            {access_filter}
            AND d.confidence >= :confidence
            AND DATE(r.datetime_start) >= :start_date
            AND DATE(r.datetime_start) <= :end_date
            AND {inst_condition}
            {condition_sql}
            GROUP BY l.location_id, l.location_name, l.location_name_en, l.lat, l.lon
            HAVING COUNT(d.detection_id) >= :min_detections -- <-- ЗМІНЕНО
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
    Генерує спектрограму (Mel або Linear) з фіксованими відступами для JS.
    
    Args:
        audio_path (str): Шлях до аудіофайлу.
        spectrogram_type (str): 'mel' (за замовчуванням) або 'linear'.
        force_regenerate (bool): Чи перезаписувати існуючий файл.
    """
    try:
        base_path, _ = os.path.splitext(audio_path)
        spectrogram_path = f"{base_path}.png"

        if not force_regenerate and os.path.exists(spectrogram_path):
            return True

        if not os.path.exists(audio_path):
            print(f"Не знайдено аудіофайл: {audio_path}")
            return False

        # 1. Завантаження аудіо
        y, sr = librosa.load(audio_path, sr=None)
        duration = librosa.get_duration(y=y, sr=sr)
        
        # 2. Налаштування полотна (HD DPI)
        fig_width = max(6, duration * 1.2)
        fig = plt.figure(figsize=(fig_width, 3), dpi=100)
        
        # Жорсткі відступи для синхронізації з JS плеєром
        ax = fig.add_axes([0.12, 0.20, 0.864, 0.75])
        
        # Спільні налаштування для HD якості
        hop_length = 256  # Малий крок для гладкості по часу
        fmax = 8000       # Обрізка частот зверху
        vmin = -70        # Поріг тиші (прибирає фоновий шум)

        S_DB = None
        y_axis_mode = 'mel'

        # 3. Логіка вибору типу спектрограми
        if spectrogram_type == 'linear':
            # --- ЛІНІЙНА (STFT) ---
            # Високий n_fft для деталізації тонких ліній частот
            n_fft = 4096 
            D = librosa.stft(y, n_fft=n_fft, hop_length=hop_length)
            S_DB = librosa.amplitude_to_db(np.abs(D), ref=np.max)
            y_axis_mode = 'linear'
            
        else:
            # --- MEL (За замовчуванням) ---
            # n_mels=256 дає високу вертикальну деталізацію
            n_fft = 2048
            n_mels = 256
            S = librosa.feature.melspectrogram(
                y=y, sr=sr, n_fft=n_fft, hop_length=hop_length, 
                n_mels=n_mels, fmax=fmax
            )
            S_DB = librosa.power_to_db(S, ref=np.max)
            y_axis_mode = 'mel'

        # 4. Відображення
        librosa.display.specshow(
            S_DB, 
            sr=sr, 
            hop_length=hop_length, 
            x_axis='time', 
            y_axis=y_axis_mode,  # Автоматично підбирає правильну шкалу
            ax=ax, 
            fmax=fmax, 
            cmap='magma',        # Контрастна палітра
            vmin=vmin
        )
        
        # Оформлення осей
        ax.set_xlabel("sec")
        ax.set_ylabel("kHz")
        
        # Форматувальник для переведення Гц у кГц (працює і для linear, і для mel)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: '{:.1f}'.format(x/1000)))

        # Збереження
        plt.savefig(spectrogram_path, transparent=False)
        plt.close(fig)

        return True

    except Exception as e:
        print(f"Error generating spectrogram: {e}")
        return False

def get_occurrence_data(filters, limit=None):
    """
    Отримує дані Occurrence.
    ОПТИМІЗОВАНО: Використовує UNION ALL для Smart Filter та пряме порівняння Timestamp для індексів.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # 1. Отримуємо параметри фільтрації
        export_mode = filters.get('export_mode', 'standard')
        agg_type = filters.get('aggregation', 'none')
        try:
            agg_minutes = int(filters.get('aggregation_minutes', 60))
        except (ValueError, TypeError):
            agg_minutes = 60

        # --- ОПТИМІЗАЦІЯ ДАТИ ---
        # Формуємо timestamp межі, щоб SQL використовував індекс по datetime_start
        start_date_str = filters.get('start_date')
        end_date_str = filters.get('end_date')
        
        params = {
            'start_ts': f"{start_date_str} 00:00:00",
            'end_ts': f"{end_date_str} 23:59:59",
            # Якщо Smart Filter - ігноруємо слайдер (ставимо 0), інакше беремо значення
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

        # --- ТАКСОНОМІЧНІ ФІЛЬТРИ ---
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

        # --- CTE: ВЕРИФІКАТОРИ ---
        # Використовується для отримання імен користувачів
        cte_verifiers = """
            WITH Verifiers AS (
                SELECT segment_id, STRING_AGG(user_id::text, '|') as verifier_user_ids
                FROM segment_verifications GROUP BY segment_id
            )
        """

        # Список колонок, однаковий для всіх частин UNION
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
        # ЛОГІКА ПОБУДОВИ SQL ЗАПИТУ
        # =========================================================================
        
        if export_mode == 'smart_filter':
            # --- РЕЖИМ SMART FILTER (OPTIMIZED) ---
            # Розбиваємо на два запити:
            # 1. Чітко верифіковані (швидкий доступ через dvm)
            # 2. Розумна автоматика (швидкий доступ через evaluation + detections)
            
            # Частина 1: Тільки Верифіковані
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
                    AND (dvm.verification_result = 1 OR dvm.positive_votes >= 1) -- Позитивна верифікація
                    AND (dvm.verification_result IS NULL OR dvm.verification_result != 0) -- Не відхилені
                    AND {inst_condition}
                    {taxo_where}
            """

            # Частина 2: Тільки High Confidence Auto
            # Явно виключаємо те, що вже є в dvm (щоб не було дублікатів з першою частиною)
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
                    -- Умови Розумного фільтра:
                    AND eval.total_samples >= 200
                    AND d.confidence >= eval.p0_95_threshold
                    -- Виключаємо записи, які мають хоч якусь історію верифікації (вони або в Частині 1, або відхилені)
                    AND (dvm.detection_id IS NULL OR (
                        (dvm.verification_result IS NULL OR dvm.verification_result = 0)
                        AND (dvm.positive_votes IS NULL OR dvm.positive_votes = 0)
                    ))
                    -- Страховка: не показувати явно відхилені
                    AND (dvm.verification_result IS NULL OR dvm.verification_result != 0)
            """

            # Об'єднуємо
            base_sql = f"{sql_part_verified} UNION ALL {sql_part_smart}"

        else:
            # --- СТАНДАРТНІ РЕЖИМИ ---
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
        # АГРЕГАЦІЯ ТА ЛІМІТИ
        # =========================================================================
        
        final_sql = ""
        count_sql = ""

        # Якщо потрібна агрегація (1 запис на день/годину)
        if agg_type in ['location_day', 'location_time']:
            if agg_type == 'location_day':
                partition_expr = "DATE(datetime_start)"
            else:
                params['agg_seconds'] = agg_minutes * 60
                partition_expr = "FLOOR(EXTRACT(EPOCH FROM datetime_start) / :agg_seconds)"

            # Обгортаємо base_sql у CTE для ранжування
            # UNION результат стає джерелом даних
            final_sql = f"""
                {cte_verifiers},
                RawData AS ({base_sql}),
                RankedData AS (
                    SELECT *,
                        ROW_NUMBER() OVER(
                            PARTITION BY scientific_name, location_id, {partition_expr}
                            ORDER BY 
                                CASE WHEN verifier_user_ids IS NOT NULL THEN 0 ELSE 1 END ASC, -- Спочатку верифіковані
                                confidence DESC -- Потім найвищий confidence
                        ) as rn
                    FROM RawData
                )
                SELECT * FROM RankedData WHERE rn = 1 ORDER BY datetime_start
            """
            
            # Для підрахунку кількості при агрегації
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
            # Без агрегації - просто повертаємо дані
            final_sql = f"{cte_verifiers} {base_sql} ORDER BY datetime_start"
            count_sql = f"{cte_verifiers} SELECT COUNT(*) FROM ({base_sql}) as total_rows"

        # =========================================================================
        # ВИКОНАННЯ ЗАПИТІВ
        # =========================================================================
        
        # 1. Count (Загальна кількість)
        total_count = conn.execute(text(count_sql), params).scalar() or 0
        
        # 2. Data (Дані з лімітом, якщо потрібно)
        if limit:
            final_sql += " LIMIT :limit"
            params['limit'] = limit
            
        db_result = conn.execute(text(final_sql), params).mappings().fetchall()
        
        # =========================================================================
        # ПОСТОБРОБКА (Mapping users & Formatting CSV dicts)
        # =========================================================================
        
        # Збираємо ID користувачів для мапінгу імен
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
                
                # Логіка Remarks для Smart Filter
                if export_mode == 'smart_filter' and p95 and row['confidence'] >= p95:
                    identificationVerificationStatus = 'unverified' # Технічно це все ще MachineObservation
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

# Список параметрів, які ми хочемо тягнути (можна легко розширити)
OPEN_METEO_PARAMS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",     # Середня температура
    "precipitation_sum",       # Опади
    "wind_speed_10m_max",      # Вітер пориви
    "wind_speed_10m_mean",     # Вітер середній
    "relative_humidity_2m_mean", # Вологість
    "surface_pressure_mean"
]

def get_weather_data(start_date, end_date, lat, lon):
    """
    Отримує погодні дані з бази geodata або Open-Meteo.
    Логіка кешування:
    1. Дані старші за 7 днів вважаються "історичними" і не оновлюються.
    2. Дані за останні 7 днів оновлюються, ЯКЩО останнє оновлення було > 12 годин тому.
    3. Використовує PostGIS для фолбеку.
    """
    # --- НАЛАШТУВАННЯ ---
    COORD_PRECISION = 2           # Точність координат (знаків після коми)
    STABILITY_THRESHOLD_DAYS = 7  # Період "нестабільності" (дні)
    CACHE_FRESHNESS_HOURS = 24    # Як часто оновлювати нестабільні дані (години)
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
        
        # Дата, до якої дані вважаються "архівними" і незмінними
        cutoff_date = (datetime.now() - timedelta(days=STABILITY_THRESHOLD_DAYS)).date()
        
        # Час, після якого свіжі дані вважаються "застарілими" і потребують повторного запиту
        refresh_cutoff_time = datetime.now() - timedelta(hours=CACHE_FRESHNESS_HOURS)

        # 2. Розумна перевірка наявності даних
        # Ми беремо дату з бази, ЯКЩО:
        # (А) Вона стара і стабільна (date <= cutoff_date)
        # АБО
        # (Б) Вона свіжа, АЛЕ ми її оновили зовсім недавно (updated_at >= refresh_cutoff_time)
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
        
        # 3. Спроба оновлення через API (тільки якщо чогось не вистачає)
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
                        # Вставляємо тільки ті дати, яких немає у списку "валідних"
                        # Тобто: або зовсім нові, або старі, які протухли (updated_at старий)
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
                    
                    # 4. Зберігаємо в базу (Upsert)
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
                                'updated_at': datetime.now() # Оновлюємо час, продовжуючи "життя" кешу
                            }
                        )
                        conn.execute(do_update_stmt)
                        conn.commit()
                else:
                    current_app.logger.error(f"Open-Meteo API Error {response.status_code}")
            
            except Exception as api_err:
                current_app.logger.error(f"Weather API Connection Failed: {api_err}")

        # 5. Фінальна вибірка
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
        
        # Fallback з використанням PostGIS (якщо API впав і даних немає)
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

        # Форматування
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


# ── Календар покриття локації записами (Idea 10 / #37) ───────────────────────
#
# Метрика покриття дня = СУМА ТРИВАЛОСТІ записів за добу (recordings.duration_minutes
# / 60 = години реального запису). Усі наявні записи 5-хвилинні (default 5);
# при імпорті тривалість задається у формі pam/import.

# Поріг «добре» у годинах реального запису на добу (узгоджено з користувачем).
COVERAGE_GOOD_HOURS = 6  # ≥6 год запису/день — добре
# Cap НЕ застосовуємо: на локації може стояти кілька ресиверів (паралельні
# записи), тож сумарна тривалість за добу законно може перевищувати 24 год.


def _coverage_level(hours_recorded):
    """Категорія клітинки календаря за сумарними годинами запису за добу."""
    if not hours_recorded or hours_recorded <= 0:
        return 'missing'
    if hours_recorded >= COVERAGE_GOOD_HOURS:
        return 'good'
    return 'partial'


def _apply_coverage_intensity(months, value_of, include):
    """Проставляє cell['intensity'] ∈ [0,1] лінійно від min до max значення
    (#43, градієнтна заливка). include(cell) → чи входить у шкалу; інакше
    intensity=None (нейтральний/сірий). Якщо всі рівні — intensity=1.0.
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
    """Перетворює {date: {'count': int, 'minutes': float}} на помісячний календар.

    `minutes` — сума тривалості записів за добу (recordings.duration_minutes);
    `count` — к-сть записів. Години реального запису = minutes/60. Чиста функція.

    mode='all' (дефолт): усі роки помісячно за весь діапазон.
    mode='aggregated': один умовний рік (12 міс); кожен (місяць,день) сумує
        значення за ВСІ роки + рахує к-сть років із даними (cell['years']).

    cell = {'day', 'date', 'count', 'hours', 'level', ['years' у aggregated]}.
    """
    # Відсіюємо записи без дати (DATE(NULL)=None ключ ламає сортування дат).
    day_data = {d: v for d, v in (day_data or {}).items() if d is not None}

    if not day_data:
        return {'months': [], 'total_recordings': 0, 'total_hours': 0.0,
                'active_days': 0, 'day_range': None, 'mode': mode}

    total_recordings = sum(v.get('count', 0) for v in day_data.values())
    cal = calendar.Calendar(firstweekday=0)  # 0 = Monday

    if mode == 'aggregated':
        # Згортка по (month, day) за всі роки.
        agg = {}  # (m, d) -> {'minutes', 'count', 'years': set}
        for dt, v in day_data.items():
            a = agg.setdefault((dt.month, dt.day),
                               {'minutes': 0.0, 'count': 0, 'years': set()})
            a['minutes'] += float(v.get('minutes', 0) or 0)
            a['count'] += v.get('count', 0)
            a['years'].add(dt.year)
        months = []
        total_hours = 0.0
        # Умовний рік 2000 (високосний — щоб 29 лютого існувало).
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
                    row.append(None)  # день сусіднього місяця — порожньо
                    continue
                info = day_data.get(d)
                cnt = info.get('count', 0) if info else 0
                minutes = float(info.get('minutes', 0) or 0) if info else 0.0
                # Сума годин запису за добу (БЕЗ обмеження 24 — кілька ресиверів
                # на локації можуть давати законно >24 год сумарно).
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