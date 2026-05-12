# myproject/app/pam/routes.py

from flask import render_template, request, jsonify, current_app, g, flash, redirect, url_for, send_from_directory, abort, Response, send_file
from flask_login import login_required, current_user
import traceback
from sqlalchemy import text
import os
import math
import json
import threading
import pandas as pd
from .pam_upload_utils import process_zip_archive, get_upload_statistics
from .pam_import_utils import IMPORTERS, PAMImportProcessor
from app.utils.decorators import role_required
from . import pam_bp
from .utils import get_pam_db_connection, get_pam_engine, generate_spectrogram_image, get_occurrence_data, get_institution_filter
import io
import csv
from app.models import User, Institution
from datetime import datetime, timedelta, date
from .pam_evaluation_utils import get_species_logistic_data


# --- СТАТИЧНІ ФАЙЛИ МОДУЛЯ PAM ---
@pam_bp.route('/<lang_code>/pam-static/<path:filename>')
def serve_pam_static(lang_code, filename):
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    return send_from_directory(static_dir, filename)


@pam_bp.route('/<lang_code>/pam')
def pam_home(lang_code):
    """
    Цільова сторінка (ХАБ) для розділу PAM.
    Картковий лендінг з 3 секціями: Аналітика / Верифікація / Управління.
    """
    g.lang_code = lang_code
    auth = current_user.is_authenticated
    return render_template(
        'pam_home.html',
        can_verifier=auth and current_user.has_role(
            'manager', 'pam_verifier', 'roztochya_user', 'fzs_user', 'volunteer_user'
        ),
        is_manager=auth and current_user.has_role('manager'),
        can_export=auth and current_user.has_role('manager', 'roztochya_user'),
    )

@pam_bp.route('/<lang_code>/pam/pam_detailed')
def pam_detailed(lang_code):
    """
    Сторінка дашборду акустичного моніторингу.
    """
    from .utils import get_available_species
    
    g.lang_code = lang_code
    
    try:
        species_list = get_available_species(lang_code)
        current_app.logger.info(f"Loaded {len(species_list)} species for dashboard")
    except Exception as e:
        current_app.logger.error(f"Error loading species list: {e}")
        species_list = []
    
    return render_template('pam_species_detailed.html', available_species=species_list)

@pam_bp.route('/<lang_code>/pam/pam_overview')
def pam_overview(lang_code):
    """
    Сторінка загального огляду з рейтинговою таблицею всіх видів.
    """
    g.lang_code = lang_code
    return render_template('pam_overview.html')

@pam_bp.route('/<lang_code>/pam/verification/upload')
@login_required 
@role_required('manager')
def verification_upload(lang_code):
    """Сторінка завантаження ZIP архівів з аудіосегментами."""
    g.lang_code = lang_code
    
    current_app.logger.info("Loading verification upload page...")
    
    try:
        current_app.logger.info("Attempting to get upload statistics...")
        stats = get_upload_statistics()
        current_app.logger.info(f"Statistics loaded successfully: {stats}")
        
        max_upload_size = current_app.config.get('PAM_MAX_UPLOAD_SIZE', 700 * 1024 * 1024)
        max_upload_size_mb = max_upload_size // (1024 * 1024)

        # Додайте ці рядки для діагностики:
        current_app.logger.info(f"PAM_MAX_UPLOAD_SIZE from config: {current_app.config.get('PAM_MAX_UPLOAD_SIZE')}")
        current_app.logger.info(f"max_upload_size calculated: {max_upload_size}")
        current_app.logger.info(f"max_upload_size_mb calculated: {max_upload_size_mb}")

        return render_template('pam_verification_upload.html', 
                            stats=stats,
                            max_upload_size=max_upload_size,
                            max_upload_size_mb=max_upload_size_mb)
        
    except Exception as e:
        current_app.logger.error(f"Error loading verification upload page: {e}")
        current_app.logger.error(f"Exception type: {type(e)}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        
        # Спробуємо з порожньою статистикою
        current_app.logger.info("Trying with empty stats...")
        try:
            empty_stats = {
                'total_segments': 0,
                'status_counts': {},
                'species_counts': {}
            }
            return render_template('pam_verification_upload.html', stats=empty_stats)
        except Exception as template_error:
            current_app.logger.error(f"Template error: {template_error}")
            flash('Помилка завантаження сторінки.', 'danger')
            return redirect(url_for('pam.pam_home', lang_code=lang_code))

@pam_bp.route('/<lang_code>/pam/verification/upload/process', methods=['POST'])
@login_required
@role_required('manager')
def process_verification_upload(lang_code):
    """API для обробки завантаження ZIP архівів з сегментами."""
    g.lang_code = lang_code
    
    try:
        current_app.logger.info(f"Upload process started by user {current_user.username}")
        
        # Детальна перевірка запиту
        current_app.logger.info(f"Files in request: {request.files.keys()}")
        current_app.logger.info(f"Form data: {request.form.keys()}")
        
        # Перевіряємо чи є файл у запиті
        if 'zip_file' not in request.files:
            current_app.logger.error("No zip_file in request.files")
            return jsonify({'success': False, 'error': 'Файл не надано'}), 400
            
        zip_file = request.files['zip_file']
        current_app.logger.info(f"Received file: {zip_file.filename}")
        
        if zip_file.filename == '':
            current_app.logger.error("Empty filename")
            return jsonify({'success': False, 'error': 'Файл не вибрано'}), 400
            
        if not zip_file.filename.lower().endswith('.zip'):
            current_app.logger.error(f"Invalid file extension: {zip_file.filename}")
            return jsonify({'success': False, 'error': 'Тільки ZIP файли дозволені'}), 400
        
        # Перевірка розміру з конфігурації
        content_length = request.content_length
        max_size = current_app.config.get('PAM_MAX_UPLOAD_SIZE', 100 * 1024 * 1024)
        max_size_mb = max_size // (1024 * 1024)
        
        if content_length and content_length > max_size:
            current_app.logger.error(f"File too large: {content_length} bytes, max allowed: {max_size}")
            return jsonify({
                'success': False, 
                'error': f'Файл занадто великий (максимум {max_size_mb}MB)'
            }), 400
        
        current_app.logger.info(f"Content length: {content_length}, max allowed: {max_size}")
        
        # ВИПРАВЛЕННЯ: Використовуємо PAM_UPLOAD_PATH з конфігурації
        upload_dir = current_app.config.get('PAM_UPLOAD_PATH')
        
        if not upload_dir:
            # Fallback шлях якщо не налаштовано в конфігурації
            upload_dir = os.path.join(current_app.instance_path, 'uploads', 'pam_segments')
            current_app.logger.warning(f"PAM_UPLOAD_PATH not configured, using fallback: {upload_dir}")
        
        # Створюємо директорію якщо не існує
        try:
            os.makedirs(upload_dir, exist_ok=True)
            current_app.logger.info(f"Using upload directory: {upload_dir}")
        except OSError as e:
            current_app.logger.error(f"Failed to create upload directory {upload_dir}: {e}")
            return jsonify({
                'success': False, 
                'error': 'Помилка створення директорії для завантаження'
            }), 500
                
        # Обробляємо архів
        current_app.logger.info("Starting ZIP archive processing")
        stats = process_zip_archive(zip_file, upload_dir)
        
        current_app.logger.info(f"ZIP archive processed successfully. Stats: {stats}")
        
        return jsonify({
            'success': True,
            'message': 'Архів успішно оброблено',
            'stats': stats
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error processing ZIP archive: {e}")
        current_app.logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False, 
            'error': f'Помилка обробки архіву: {str(e)}'
        }), 500

@pam_bp.route('/<lang_code>/pam/verification/segments')
@login_required
@role_required('pam_verifier')
def verification_segments(lang_code):
    """Сторінка перегляду завантажених сегментів з фільтрацією."""
    g.lang_code = lang_code
    
    current_app.logger.info("Loading verification segments page...")
    
    conn = None
    try:
        current_app.logger.info("Attempting to connect to PAM database...")
        conn = get_pam_db_connection()
        current_app.logger.info("PAM database connected successfully")
        
        current_app.logger.info("Fetching species for filter...")
        species_for_filter = conn.execute(text("""
            SELECT DISTINCT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            FROM species s
            JOIN segments seg ON s.species_id = seg.species_id
            ORDER BY s.scientific_name
        """)).fetchall()
        
        current_app.logger.info(f"Found {len(species_for_filter)} species for filter")
        
        species_list = []
        for species in species_for_filter:
            display_name = species[1]  # scientific_name
            if lang_code == 'uk' and species[2]:  # common_name_uk
                display_name = f"{species[2]} ({species[1]})"
            elif lang_code == 'en' and species[3]:  # common_name_en
                display_name = f"{species[3]} ({species[1]})"
                
            species_list.append({
                'id': species[0],
                'name': display_name
            })
        
        current_app.logger.info(f"Prepared {len(species_list)} species for template")
        return render_template('pam_verification_segments.html', 
                             available_species=species_list)
        
    except Exception as e:
        current_app.logger.error(f"Error loading segments page: {e}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        flash('Помилка завантаження сторінки сегментів.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/pam/verification/verify')
@login_required
@role_required('pam_verifier')
def verification_interface(lang_code):
    """Інтерфейс для верифікації аудіосегментів."""
    g.lang_code = lang_code
    
    current_app.logger.info("Loading verification interface page...")
    
    conn = None
    try:
        current_app.logger.info("Attempting to connect to PAM database...")
        conn = get_pam_db_connection()
        current_app.logger.info("PAM database connected successfully")
        
        current_app.logger.info("Fetching species with pending segments...")
        species_query = """
            SELECT DISTINCT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            FROM species s
            JOIN segments seg ON s.species_id = seg.species_id
            WHERE seg.status = 'pending'
            ORDER BY s.scientific_name
        """
        
        species_data = conn.execute(text(species_query)).fetchall()
        current_app.logger.info(f"Found {len(species_data)} species with pending segments")
        
        species_list = []
        for species in species_data:
            display_name = species[1]  # scientific_name
            if lang_code == 'uk' and species[2]:  # common_name_uk
                display_name = f"{species[2]} ({species[1]})"
            elif lang_code == 'en' and species[3]:  # common_name_en
                display_name = f"{species[3]} ({species[1]})"
                
            species_list.append({
                'id': species[0],
                'name': display_name
            })
        
        current_app.logger.info(f"Prepared {len(species_list)} species for verification interface")
        return render_template('pam_verification_interface.html', 
                             available_species=species_list)
        
    except Exception as e:
        current_app.logger.error(f"Error loading verification interface: {e}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        flash('Помилка завантаження інтерфейсу верифікації.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/pam/evaluation/results')
def evaluation_results(lang_code):
    """Сторінка перегляду результатів оцінки точності BirdNET."""
    g.lang_code = lang_code
    from .pam_evaluation_utils import get_evaluation_summary, get_species_for_dropdown
    
    try:
        current_app.logger.info("=== STARTING EVALUATION RESULTS PAGE ===")
        current_app.logger.info(f"Language code: {lang_code}")
        
        current_app.logger.info("Calling get_evaluation_summary()...")
        summary = get_evaluation_summary()
        species_list = get_species_for_dropdown()

        current_app.logger.info(f"Summary received. Type: {type(summary)}")
        
        if summary:
            current_app.logger.info(f"Summary keys: {summary.keys()}")
            
            # Детальна перевірка summary секції
            if 'summary' in summary:
                current_app.logger.info(f"Summary.summary keys: {summary['summary'].keys()}")
                current_app.logger.info(f"Last calculation value: {summary['summary'].get('last_calculation')}")
                current_app.logger.info(f"Last calculation type: {type(summary['summary'].get('last_calculation'))}")
            
            # Детальна перевірка top_species
            if 'top_species' in summary:
                current_app.logger.info(f"Top species count: {len(summary['top_species'])}")
                for i, species in enumerate(summary['top_species']):
                    current_app.logger.info(f"Species {i}: {species}")
            
            # Детальна перевірка top_logistic_species
            if 'top_logistic_species' in summary:
                current_app.logger.info(f"Top logistic species count: {len(summary['top_logistic_species'])}")
                for i, species in enumerate(summary['top_logistic_species']):
                    current_app.logger.info(f"Logistic species {i}: {species}")
        
        current_app.logger.info("Rendering template...")
        result = render_template('pam_evaluation_results.html', summary=summary, species_list=species_list)
        current_app.logger.info("Template rendered successfully")
        return result
        
    except Exception as e:
        current_app.logger.error(f"Error loading evaluation results: {e}")
        current_app.logger.error(f"Error type: {type(e).__name__}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        
        # Додаткова діагностика - перевіримо чи можемо підключитись до PAM DB
        try:
            from .utils import get_pam_db_connection
            current_app.logger.info("Testing PAM DB connection...")
            conn = get_pam_db_connection()
            current_app.logger.info("PAM DB connection successful")
            
            # Перевіримо чи є взагалі таблиця evaluation
            result = conn.execute(text("SELECT COUNT(*) FROM evaluation")).fetchone()
            current_app.logger.info(f"Evaluation table record count: {result[0] if result else 'ERROR'}")
            
            conn.close()
            
        except Exception as db_error:
            current_app.logger.error(f"PAM DB connection error: {db_error}")
        
        flash('Помилка завантаження результатів оцінки.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))

@pam_bp.route('/<lang_code>/api/pam/get-plot-data')
def api_get_plot_data(lang_code):
    """
    API-ендпоінт для отримання даних для графіка.
    ОНОВЛЕНО: Повертає повний об'єкт детекцій.
    """
    from .utils import get_available_species, get_filtered_detections
    
    try:
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        institution_id = request.args.get('institution_id', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None
        
        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75

        if not species_name:
            return jsonify({'error': 'Species name is required.'}), 400

        allowed_species_rows = get_available_species(lang_code)
        allowed_species_values = [row['value'] for row in allowed_species_rows]
        if species_name not in allowed_species_values:
            return jsonify({'error': 'Access denied to this species.'}), 403
        
        all_detections = get_filtered_detections(
            species_name=species_name,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            institution_id=institution_id
        )
        
        # Замість розділення на окремі списки, повертаємо один масив об'єктів
        return jsonify({
            'species_name': species_name,
            'detections': all_detections
        })
    
    except Exception as e:
        current_app.logger.error(f"Error in get_plot_data: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-barchart-data')
def api_get_barchart_data(lang_code):
    """API-ендпоінт для отримання даних для стовпчикового графіка."""
    from .utils import get_available_species, get_daily_detection_counts
    
    try:
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None

        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75

        if not species_name:
            return jsonify({'error': 'Species name is required.'}), 400

        allowed_species_rows = get_available_species(lang_code)
        allowed_species_values = [row['value'] for row in allowed_species_rows]
        if species_name not in allowed_species_values:
            return jsonify({'error': 'Access denied to this species.'}), 403
        
        barchart_data = get_daily_detection_counts(
            species_name=species_name,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            institution_id=institution_id
        )
        
        return jsonify(barchart_data)
    
    except Exception as e:
        current_app.logger.error(f"Error in get_barchart_data: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-time-scatter-data')
def api_get_time_scatter_data(lang_code):
    """API-ендпоінт для графіка добової активності."""
    from .utils import get_available_species, get_time_scatter_data
    
    try:
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None

        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75

        current_app.logger.info(f"API call for time scatter data: species={species_name}, confidence={confidence}, start_date={start_date}, end_date={end_date}")

        if not species_name:
            return jsonify({'error': 'Species name is required.'}), 400

        allowed_species_rows = get_available_species(lang_code)
        allowed_species_values = [row['value'] for row in allowed_species_rows]
        if species_name not in allowed_species_values:
            return jsonify({'error': 'Access denied to this species.'}), 403
        
        plot_data = get_time_scatter_data(
            species_name=species_name,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            institution_id=institution_id
        )
        
        current_app.logger.info(f"Returning time scatter data: {len(plot_data.get('detections', []))} detections, {len(plot_data.get('sun_times', []))} sun times")
        
        return jsonify(plot_data)
    
    except Exception as e:
        current_app.logger.error(f"Error in get_time_scatter_data: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-species-summary')
def api_get_species_summary(lang_code):
    """API для отримання загальної статистики по виду."""
    from .utils import get_available_species, get_species_summary

    try:
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_id = request.args.get('location_id')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None
        
        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75
        
        try:
            min_detections = int(request.args.get('min_detections', 1))
        except (ValueError, TypeError):
            min_detections = 1

        if not species_name:
            return jsonify({'error': 'Species name is required.'}), 400

        allowed_species_rows = get_available_species(lang_code)
        allowed_species_values = [row['value'] for row in allowed_species_rows]
        if species_name not in allowed_species_values:
            return jsonify({'error': 'Access denied to this species.'}), 403

        summary = get_species_summary(
            species_name=species_name,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_id=location_id,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            min_detections=min_detections,
            institution_id=institution_id
        )

        return jsonify(summary)

    except Exception as e:
        current_app.logger.error(f"Error in get_species_summary: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-unique-points')
def api_get_unique_points(lang_code):
    """API для отримання унікальних точок виявлення виду."""
    from .utils import get_available_species, get_unique_detection_points

    try:
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None
        
        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75
            
        try:
            min_detections = int(request.args.get('min_detections', 1))
        except (ValueError, TypeError):
            min_detections = 1

        if not species_name:
            return jsonify({'error': 'Species name is required.'}), 400

        allowed_species_rows = get_available_species(lang_code)
        allowed_species_values = [row['value'] for row in allowed_species_rows]
        if species_name not in allowed_species_values:
            return jsonify({'error': 'Access denied to this species.'}), 403

        points = get_unique_detection_points(
            lang_code=lang_code, # <-- ДОДАНО
            species_name=species_name,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            min_detections=min_detections,
            institution_id=institution_id
        )

        return jsonify(points)

    except Exception as e:
        current_app.logger.error(f"Error in get_unique_detection_points: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-filters-data')
def api_get_pam_filters_data(lang_code):
    """API для отримання даних фільтрів: установи, біотопи, локації."""
    conn = None
    try:
        conn = get_pam_db_connection()
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        selected_inst_id = request.args.get('institution_id', '') # Новий параметр

        params = {}
        date_filter = ""
        if start_date and end_date:
            date_filter = " AND r.datetime_start BETWEEN :start_date AND :end_date"
            params['start_date'] = start_date
            params['end_date'] = end_date

        # Базові права доступу
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        base_inst_condition, base_inst_params = get_institution_filter(user_inst_ids, is_admin)
        params.update(base_inst_params)

        # Додаткова умова, якщо користувач вибрав конкретну установу у фільтрі
        selected_inst_filter = ""
        inst_ids = []
        if selected_inst_id:
            inst_ids = [int(i) for i in selected_inst_id.split(',') if i.strip().isdigit()]
            if inst_ids:
                selected_inst_filter = """
                    AND EXISTS (
                        SELECT 1 FROM location_institutions li_sel 
                        WHERE li_sel.location_id = l.location_id 
                        AND li_sel.institution_id = ANY(:selected_inst_ids)
                    )
                """
        params['selected_inst_ids'] = inst_ids

        # 1. Отримуємо список Установ (каскадність: залежить тільки від дат і прав)
        # Показуємо тільки ті установи, де є записи у вибраний період
        inst_query = f"""
            SELECT DISTINCT i.id, i.name_uk, i.name_en, i.code
            FROM institutions i
            JOIN location_institutions li ON i.id = li.institution_id
            JOIN locations l ON li.location_id = l.location_id
            JOIN recordings r ON l.location_id = r.location_id
            WHERE {base_inst_condition}
            {date_filter}
            ORDER BY i.name_uk
        """
        inst_result = conn.execute(text(inst_query), params).mappings().fetchall()
        institutions = []
        for row in inst_result:
            display_name = row['name_uk']
            if lang_code == 'en' and row['name_en']:
                display_name = row['name_en']
            
            institutions.append({'id': row['id'], 'text': display_name})

        # 2. Отримуємо Біотопи (залежать від дати, прав + вибраної установи)
        biotopes_query = f"""
            SELECT DISTINCT b.id, b.name_ua, b.name_en
            FROM biotopes b
            JOIN location_biotopes lb ON b.id = lb.biotope_id
            JOIN locations l ON lb.location_id = l.location_id
            JOIN recordings r ON l.location_id = r.location_id
            WHERE {base_inst_condition} 
            {date_filter}
            {selected_inst_filter}
            ORDER BY b.name_ua
        """
        biotopes_result = conn.execute(text(biotopes_query), params).mappings().fetchall()
        biotopes = [{'id': row['id'], 'text': row['name_ua'] if lang_code == 'uk' else row['name_en']} for row in biotopes_result]

        # 3. Отримуємо Локації (залежать від дати, прав + вибраної установи)
        locations_query = f"""
            SELECT DISTINCT l.location_id, l.location_name, l.location_name_en
            FROM locations l
            JOIN recordings r ON l.location_id = r.location_id
            WHERE EXISTS (SELECT 1 FROM detections WHERE recording_id = r.recording_id)
            AND {base_inst_condition}
            {date_filter}
            {selected_inst_filter}
            ORDER BY l.location_name
        """
        locations_result = conn.execute(text(locations_query), params).mappings().fetchall()
        locations = []
        for row in locations_result:
            display_name = row['location_name']
            if lang_code == 'en' and row['location_name_en']:
                display_name = row['location_name_en']
            locations.append({'id': row['location_id'], 'text': display_name})

        return jsonify({
            'institutions': institutions,
            'biotopes': biotopes,
            'locations': locations
        })

    except Exception as e:
        current_app.logger.error(f"Error getting filters data: {e}")
        return jsonify({'error': 'Failed to load filter data'}), 500
    finally:
        if conn:
            conn.close()
            
@pam_bp.route('/<lang_code>/api/pam/get-species-ranking')
def api_get_species_ranking(lang_code):
    """API-ендпоінт для отримання рейтингової таблиці всіх видів."""
    from .utils import get_species_ranking
    
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        tax_filters = {
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus')
        }
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None


        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75
        try:
            min_detections = int(request.args.get('min_detections', 5))
        except (ValueError, TypeError):
            min_detections = 5

        ranking = get_species_ranking(
            lang_code=lang_code,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            min_detections=min_detections,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            tax_filters=tax_filters,
            institution_id=institution_id
        )
        
        return jsonify(ranking)
    
    except Exception as e:
        current_app.logger.error(f"Error in get_species_ranking: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-overview-stats')
def api_get_overview_stats(lang_code):
    """API для загальної статистики PAM."""
    from .utils import get_overview_statistics
    
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None

        tax_filters = {
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus')
        }

        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75
        
        try:
            min_detections = int(request.args.get('min_detections', 1))
        except (ValueError, TypeError):
            min_detections = 1
            
        stats = get_overview_statistics(
            lang_code=lang_code,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            min_detections=min_detections,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            tax_filters=tax_filters,
            institution_id=institution_id
        )
        
        return jsonify(stats)
        
    except Exception as e:
        current_app.logger.error(f"Error in get_overview_stats: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/pam/get-locations-map')
def api_get_locations_map(lang_code):
    """API для даних карти локацій."""
    from .utils import get_locations_for_map
    
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None
        
        tax_filters = {
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus')
        }


        try:
            confidence = float(request.args.get('confidence', 0.75))
        except (ValueError, TypeError):
            confidence = 0.75

        try:
            min_detections = int(request.args.get('min_detections', 1))
        except (ValueError, TypeError):
            min_detections = 1
            
        locations = get_locations_for_map(
            lang_code=lang_code,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            location_ids=location_ids,
            biotope_ids=biotope_ids,
            min_detections=min_detections,
            tax_filters=tax_filters,
            institution_id=institution_id
        )
        
        return jsonify(locations)
        
    except Exception as e:
        current_app.logger.error(f"Error in get_locations_map: {e}")
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Internal server error'}), 500

@pam_bp.route('/<lang_code>/api/verification/segments')
@login_required
def api_verification_segments(lang_code):
    """API для отримання списку сегментів з пагінацією та фільтрацією."""
    from .utils import get_pam_db_connection
    
    conn = None
    try:
        conn = get_pam_db_connection()
        
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        species_id = request.args.get('species_id', type=int)
        status = request.args.get('status', 'all')
        
        # Базовий запит для вибірки сегментів
        base_query = """
            SELECT seg.id, seg.filename, seg.confidence_level, seg.location_name,
                   seg.recorded_date, seg.recorded_time, seg.status,
                   seg.verification_count, seg.positive_verifications,
                   s.scientific_name, s.common_name_uk, s.common_name_en
            FROM segments seg
            JOIN species s ON seg.species_id = s.species_id
        """
        
        # Підготовка умов для основного запиту (залежать від виду та статусу)
        main_conditions = []
        main_params = {}
        
        if species_id:
            main_conditions.append("seg.species_id = :species_id")
            main_params['species_id'] = species_id
            
        if status != 'all':
            main_conditions.append("seg.status = :status")
            main_params['status'] = status
        
        main_where_clause = ""
        if main_conditions:
            main_where_clause = "WHERE " + " AND ".join(main_conditions)
        
        # Запит для підрахунку загальної кількості (для пагінації)
        count_query = text(f"""
            SELECT COUNT(*)
            FROM segments seg
            {main_where_clause}
        """)
        
        total_count = conn.execute(count_query, main_params).scalar()
        
        # Розрахунок статистики
        species_only_conditions = []
        species_only_params = {}
        if species_id:
            species_only_conditions.append("seg.species_id = :species_id")
            species_only_params['species_id'] = species_id

        species_only_where = "WHERE " + " AND ".join(species_only_conditions) if species_only_conditions else ""
        
        # Отримуємо кількість сегментів по потрібних статусах
        counts_query_sql = text(f"""
            SELECT
                COUNT(CASE WHEN seg.status = 'pending' THEN 1 END) as pending_count,
                COUNT(CASE WHEN seg.status = 'completed' THEN 1 END) as completed_count,
                COUNT(CASE WHEN seg.status = 'archived' THEN 1 END) as archived_count
            FROM segments seg
            {species_only_where}
        """)
        counts_result = conn.execute(counts_query_sql, species_only_params).fetchone()

        # Розраховуємо середню точність
        avg_confidence_query_sql = text(f"""
            SELECT AVG(seg.confidence_level)
            FROM segments seg
            {main_where_clause}
        """)
        avg_confidence = conn.execute(avg_confidence_query_sql, main_params).scalar() or 0.0
        
        # Основний запит з пагінацією
        offset = (page - 1) * per_page
        pagination_params = main_params.copy()
        pagination_params['limit'] = per_page
        pagination_params['offset'] = offset
        
        main_query = text(f"""
            {base_query}
            {main_where_clause}
            ORDER BY seg.upload_date DESC
            LIMIT :limit OFFSET :offset
        """)
        
        segments = conn.execute(main_query, pagination_params).fetchall()
        
        # Форматування результатів
        segments_data = []
        for seg in segments:
            species_name = seg[9]
            if lang_code == 'uk' and seg[10]:
                species_name = seg[10]
            elif lang_code == 'en' and seg[11]:
                species_name = seg[11]
            
            segments_data.append({
                'id': seg[0],
                'filename': seg[1],
                'confidence_level': round(seg[2], 3) if seg[2] else 0,
                'location_name': seg[3] or '',
                'recorded_date': seg[4].strftime('%d.%m.%Y') if seg[4] else '',
                'recorded_time': seg[5].strftime('%H:%M:%S') if seg[5] else '',
                'status': seg[6] or 'unknown',
                'verification_count': seg[7] or 0,
                'positive_verifications': seg[8] or 0,
                'species_name': species_name
            })
        
        return jsonify({
            'segments': segments_data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_count,
                'pages': (total_count + per_page - 1) // per_page if total_count > 0 else 0
            },
            'summary': {
                'pending_count': counts_result.pending_count if counts_result else 0,
                'completed_count': counts_result.completed_count if counts_result else 0,
                'archived_count': counts_result.archived_count if counts_result else 0,
                'avg_confidence': round(float(avg_confidence), 3)
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Error fetching segments: {e}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Помилка отримання даних'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/api/verification/next-segment')
@login_required
@role_required('pam_verifier')
def api_next_verification_segment(lang_code):
    """API для отримання наступного сегменту для верифікації."""
    conn = None
    try:
        # --- ВИПРАВЛЕННЯ: Ініціалізуємо g.lang_code ---
        g.lang_code = lang_code
        # ----------------------------------------------
        
        conn = get_pam_db_connection()
        
        species_id = request.args.get('species_id', type=int)
        
        # ... (решта коду залишається без змін) ...
        
        query = text("""
            SELECT seg.id, seg.filename, seg.confidence_level, seg.location_name,
                   seg.recorded_date, seg.recorded_time, seg.file_path,
                   s.scientific_name, s.common_name_uk, s.common_name_en
            FROM segments seg
            JOIN species s ON seg.species_id = s.species_id
            WHERE seg.status = 'pending'
            AND seg.id NOT IN (
                SELECT sv.segment_id 
                FROM segment_verifications sv 
                WHERE sv.user_id = :user_id
            )
        """)
        
        params = {"user_id": current_user.id}
        
        if species_id:
            query = text("""
                SELECT seg.id, seg.filename, seg.confidence_level, seg.location_name,
                       seg.recorded_date, seg.recorded_time, seg.file_path,
                       s.scientific_name, s.common_name_uk, s.common_name_en
                FROM segments seg
                JOIN species s ON seg.species_id = s.species_id
                WHERE seg.status = 'pending'
                AND seg.species_id = :species_id
                AND seg.id NOT IN (
                    SELECT sv.segment_id 
                    FROM segment_verifications sv 
                    WHERE sv.user_id = :user_id
                )
                ORDER BY RANDOM() LIMIT 1
            """)
            params["species_id"] = species_id
        else:
            query = text(str(query) + " ORDER BY RANDOM() LIMIT 1")
        
        result = conn.execute(query, params).fetchone()
        
        if not result:
            return jsonify({
                'message': 'Немає сегментів для верифікації за вибраними критеріями'
            }), 404
        
        scientific_name = result[7]
        common_name_uk = result[8]
        common_name_en = result[9]
        
        display_name = scientific_name
        if g.lang_code == 'uk' and common_name_uk:
            display_name = f"{common_name_uk} ({scientific_name})"
        elif g.lang_code == 'en' and common_name_en:
            display_name = f"{common_name_en} ({scientific_name})"

        audio_url = url_for('pam.serve_verification_audio', 
                           lang_code=g.lang_code,
                           segment_id=result[0],
                           _external=True)
        
        return jsonify({
            'segment_id': result[0],
            'filename': result[1],
            'confidence_level': round(result[2], 3),
            'location_name': result[3],
            'recorded_date': result[4].strftime('%d.%m.%Y'),
            'recorded_time': result[5].strftime('%H:%M:%S'),
            'species_display_name': display_name,
            'audio_url': audio_url
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting next verification segment: {e}")
        # Додамо трасування помилки в лог для кращої діагностики
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': 'Помилка отримання сегменту'}), 500
    finally:
        if conn:
            conn.close()
            
@pam_bp.route('/<lang_code>/api/verification/submit', methods=['POST'])
@login_required  
@role_required('pam_verifier')
def api_submit_verification(lang_code):
    """API для збереження результатів верифікації."""
    conn = None
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'Немає даних для обробки'}), 400
            
        segment_id = data.get('segment_id')
        verification_result = data.get('verification_result')  # 1, 0, або None
        
        current_app.logger.info(f"Received verification: segment_id={segment_id}, result={verification_result}")
        
        if not segment_id:
            return jsonify({'success': False, 'error': 'ID сегменту обов\'язковий'}), 400
            
        if verification_result not in [0, 1, None]:
            return jsonify({
                'success': False, 
                'error': 'Результат верифікації має бути 0, 1 або null'
            }), 400
        
        conn = get_pam_db_connection()
        
        # Перевіряємо чи існує сегмент
        segment_check = conn.execute(
            text("SELECT id, status FROM segments WHERE id = :segment_id"),
            {"segment_id": segment_id}
        ).fetchone()
        
        if not segment_check:
            return jsonify({'success': False, 'error': 'Сегмент не знайдено'}), 404
            
        if segment_check[1] != 'pending':
            return jsonify({
                'success': False, 
                'error': 'Сегмент вже не потребує верифікації'
            }), 400
        
        # Перевіряємо чи користувач вже верифікував цей сегмент
        existing = conn.execute(text("""
            SELECT id FROM segment_verifications 
            WHERE segment_id = :segment_id AND user_id = :user_id
        """), {
            "segment_id": segment_id, 
            "user_id": current_user.id
        }).fetchone()
        
        if existing:
            # Оновлюємо існуючу верифікацію
            conn.execute(text("""
                UPDATE segment_verifications 
                SET verification_result = :verification_result, verified_at = CURRENT_TIMESTAMP
                WHERE segment_id = :segment_id AND user_id = :user_id
            """), {
                "verification_result": verification_result,
                "segment_id": segment_id, 
                "user_id": current_user.id
            })
            action = 'updated'
        else:
            # Створюємо нову верифікацію
            conn.execute(text("""
                INSERT INTO segment_verifications (segment_id, user_id, verification_result)
                VALUES (:segment_id, :user_id, :verification_result)
            """), {
                "segment_id": segment_id,
                "user_id": current_user.id,
                "verification_result": verification_result
            })
            action = 'created'
        
        conn.commit()
        
        current_app.logger.info(
            f"Verification {action} by user {current_user.username} for segment {segment_id}: {verification_result}"
        )
        
        return jsonify({
            'success': True,
            'message': 'Верифікацію збережено успішно',
            'action': action
        })
        
    except Exception as e:
        if conn:
            conn.rollback()
        current_app.logger.error(f"Error submitting verification: {e}")
        current_app.logger.error(f"Request data: {request.get_data()}")
        return jsonify({'success': False, 'error': 'Помилка збереження верифікації'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/api/verification/stats')
@login_required
def api_verification_stats(lang_code):
    """
    API для отримання статистики верифікацій користувача.
    Підтримує фільтрацію за видом (species_id).
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        species_id = request.args.get('species_id', type=int)

        # --- Підготовка умов та параметрів для запитів ---
        params = {'user_id': current_user.id}
        
        # Умови для статистики користувача (верифіковані сегменти)
        user_stats_conditions = ["sv.user_id = :user_id"]
        # Умови для підрахунку сегментів, що залишились (не верифіковані)
        remaining_conditions = ["seg.status = 'pending'"]
        
        if species_id:
            params['species_id'] = species_id
            user_stats_conditions.append("seg.species_id = :species_id")
            remaining_conditions.append("seg.species_id = :species_id")

        user_stats_where = " AND ".join(user_stats_conditions)
        remaining_where = " AND ".join(remaining_conditions)
        
        # --- Запит 1: Отримати статистику верифікованих сегментів ---
        user_stats_query = text(f"""
            SELECT 
                COUNT(*) as total_verifications,
                COUNT(CASE WHEN sv.verification_result = 1 THEN 1 END) as positive_verifications,
                COUNT(CASE WHEN sv.verification_result = 0 THEN 1 END) as negative_verifications,
                COUNT(CASE WHEN sv.verification_result IS NULL THEN 1 END) as skipped_verifications
            FROM segment_verifications sv
            JOIN segments seg ON sv.segment_id = seg.id
            WHERE {user_stats_where}
        """)
        user_stats = conn.execute(user_stats_query, params).fetchone()
        
        # --- Запит 2: Отримати кількість сегментів, що залишились для верифікації ---
        remaining_query = text(f"""
            SELECT COUNT(DISTINCT seg.id)
            FROM segments seg
            WHERE {remaining_where}
              AND seg.id NOT IN (
                SELECT sv.segment_id 
                FROM segment_verifications sv 
                WHERE sv.user_id = :user_id
            )
        """)
        remaining_count = conn.execute(remaining_query, params).scalar()

        # --- Запит 3: Статистика по видах (для іншого функціоналу, але теж оновлена) ---
        species_stats_query = text(f"""
            SELECT s.scientific_name, s.common_name_uk, s.common_name_en,
                   COUNT(*) as verifications_count,
                   COUNT(CASE WHEN sv.verification_result = 1 THEN 1 END) as positive_count
            FROM segment_verifications sv
            JOIN segments seg ON sv.segment_id = seg.id
            JOIN species s ON seg.species_id = s.species_id
            WHERE {user_stats_where}
            GROUP BY s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            ORDER BY verifications_count DESC
        """)
        species_stats_results = conn.execute(species_stats_query, params).fetchall()
        
        species_data = []
        for row in species_stats_results:
            species_name = row[0]
            if g.lang_code == 'uk' and row[1]:
                species_name = row[1]
            elif g.lang_code == 'en' and row[2]:
                species_name = row[2]
                
            species_data.append({
                'species_name': species_name,
                'total_verifications': row[3],
                'positive_verifications': row[4],
                'accuracy_rate': round(row[4] / row[3] * 100, 1) if row[3] > 0 else 0
            })
        
        return jsonify({
            'user_stats': {
                'total_verifications': user_stats.total_verifications if user_stats else 0,
                'positive_verifications': user_stats.positive_verifications if user_stats else 0,
                'negative_verifications': user_stats.negative_verifications if user_stats else 0,
                'skipped_verifications': user_stats.skipped_verifications if user_stats else 0,
                'remaining_verifications': remaining_count if remaining_count is not None else 0
            },
            'species_stats': species_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting verification stats: {e}")
        return jsonify({'error': 'Помилка отримання статистики'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/audio/segments/<int:segment_id>')
@login_required
@role_required('pam_verifier')
def serve_verification_audio(lang_code, segment_id):
    """Сервіс для віддачі аудіофайлів верифікації."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        result = conn.execute(
            text("SELECT file_path, filename FROM segments WHERE id = :segment_id"),
            {"segment_id": segment_id}
        ).fetchone()
        
        if not result:
            current_app.logger.error(f"Segment {segment_id} not found in database")
            abort(404)
        
        file_path, filename = result

        # --- ПОЧАТОК ТИМЧАСОВОЇ ЗАГЛУШКИ ДЛЯ ЛОКАЛЬНОЇ РОЗРОБКИ ---
        # local_upload_path = "C:/Users/IuriiStrus/GitHub_cloned_repos/myproject/pam_data_import/segments/"
        # server_base_path = '/home/yura/pam_data_import/segments/'
        # file_path = file_path.replace(server_base_path, local_upload_path)
        # print(f"Local Base Path : {local_upload_path}")
        # print(f"Final Path      : {file_path}")
        # --- КІНЕЦЬ ТИМЧАСОВОЇ ЗАГЛУШКИ ---

        current_app.logger.info(f"Serving audio file: {file_path}")
        
        if not os.path.exists(file_path):
            current_app.logger.error(f"Audio file not found: {file_path}")
            abort(404)
        
        directory = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)
        
        return send_from_directory(directory, file_name, as_attachment=False)
        
    except Exception as e:
        current_app.logger.error(f"Error serving audio file for segment {segment_id}: {e}")
        abort(500)
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/api/evaluation/detailed-results')
def api_detailed_evaluation_results(lang_code):
    """API для отримання детальних результатів оцінки з пагінацією."""
    from .utils import get_pam_db_connection
    
    conn = None
    try:
        # Отримуємо параметри
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        sort_by = request.args.get('sort_by', 'precision_score')
        order = request.args.get('order', 'desc')
        
        # Валідація сортування
        sort_field_mapping = {
            'precision_score': 'e.precision_score',
            'total_samples': 'e.total_samples',
            'logistic_r_squared': 'e.logistic_r_squared',
            'p0_9_threshold': 'e.p0_9_threshold',
            'p0_95_threshold': 'e.p0_95_threshold',
            'p0_99_threshold': 'e.p0_99_threshold'
        }
        
        if sort_by not in sort_field_mapping:
            sort_by = 'precision_score'
        if order not in ['asc', 'desc']:
            order = 'desc'
        
        sort_field = sort_field_mapping[sort_by]
        
        conn = get_pam_db_connection()
        
        offset = (page - 1) * per_page
        
        # === ОНОВЛЕНИЙ SQL ЗАПИТ ===
        # Ми явно перераховуємо всі колонки, включаючи нові інтервали для порогів
        # Індекси:
        # 0-3: інфо про вид
        # 4-7: основна статистика
        # 8-10: пороги (значення)
        # 11-12: precision CI
        # 13-18: threshold CIs (НОВІ)
        query = f"""
            SELECT e.species_id, s.scientific_name, s.common_name_uk, s.common_name_en,
                e.precision_score, e.total_samples, e.calculated_at,
                e.logistic_r_squared,
                e.p0_9_threshold, e.p0_95_threshold, e.p0_99_threshold,
                e.precision_lower_ci, e.precision_upper_ci,
                e.p0_9_lower_ci, e.p0_9_upper_ci,
                e.p0_95_lower_ci, e.p0_95_upper_ci,
                e.p0_99_lower_ci, e.p0_99_upper_ci
            FROM evaluation e
            JOIN species s ON e.species_id = s.species_id
            WHERE e.is_current = TRUE
            ORDER BY {sort_field} {order.upper()} NULLS LAST
            LIMIT :per_page OFFSET :offset
        """
        
        results = conn.execute(text(query), {
            'per_page': per_page,
            'offset': offset
        }).fetchall()
        
        # Отримуємо загальну кількість
        total_count = conn.execute(text("""
            SELECT COUNT(*) FROM evaluation WHERE is_current = TRUE
        """)).fetchone()[0]
        
        # Формуємо результат
        detailed_results = []
        for i, row in enumerate(results):
            
            species_name = row[1]  # scientific_name
            if lang_code == 'uk' and row[2]:  # common_name_uk
                species_name = row[2]
            elif lang_code == 'en' and row[3]:  # common_name_en
                species_name = row[3]
                
            detailed_results.append({
                'species_id': row[0],
                'species_name': species_name,
                'scientific_name': row[1],
                
                # Основні метрики
                'precision': round(row[4], 3) if row[4] is not None else 0,
                'precision_lower': round(row[11], 3) if row[11] is not None else None,
                'precision_upper': round(row[12], 3) if row[12] is not None else None,
                
                'total_samples': row[5] if row[5] is not None else 0,
                'calculated_at': row[6].strftime('%d.%m.%Y %H:%M') if row[6] else None,
                'logistic_r_squared': round(row[7], 3) if row[7] is not None else None,
                
                # Поріг 90%
                'p0_9_threshold': round(row[8], 3) if row[8] is not None else None,
                'p0_9_lower': round(row[13], 3) if row[13] is not None else None,
                'p0_9_upper': round(row[14], 3) if row[14] is not None else None,
                
                # Поріг 95%
                'p0_95_threshold': round(row[9], 3) if row[9] is not None else None,
                'p0_95_lower': round(row[15], 3) if row[15] is not None else None,
                'p0_95_upper': round(row[16], 3) if row[16] is not None else None,
                
                # Поріг 99%
                'p0_99_threshold': round(row[10], 3) if row[10] is not None else None,
                'p0_99_lower': round(row[17], 3) if row[17] is not None else None,
                'p0_99_upper': round(row[18], 3) if row[18] is not None else None
            })
        
        response_data = {
            'results': detailed_results,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_count,
                'pages': (total_count + per_page - 1) // per_page
            },
            'sort_by': sort_by,
            'order': order
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        current_app.logger.error("=== API DETAILED EVALUATION RESULTS ERROR ===")
        current_app.logger.error(f"Error message: {str(e)}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Помилка отримання детальних результатів'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/admin/evaluation/recalculate', methods=['POST'])
@login_required
@role_required('manager')
def admin_recalculate_metrics(lang_code):
    """Адміністраторський роут для перерахунку всіх метрик."""
    from .pam_evaluation_utils import recalculate_all_metrics
    
    try:
        min_verifications = request.form.get('min_verifications', 2, type=int)

        species_choice = request.form.get('species_choice')
        target_species_id = None

        if species_choice and species_choice != 'all':
            try:
                target_species_id = int(species_choice)
            except ValueError:
                pass # Якщо прийшло сміття, буде None (тобто 'all')
        
        if min_verifications < 1 or min_verifications > 10:
            flash('Мінімальна кількість верифікацій має бути між 1 та 10.', 'warning')
            return redirect(url_for('pam.evaluation_results', lang_code=lang_code))
        
        current_app.logger.info(f"Metrics recalculation started by admin {current_user.username}")
        
        result = recalculate_all_metrics(current_user.id, min_verifications, target_species_id=target_species_id)

        if result['success']:
            calc_count = result['calculated_count']
            fail_count = result['failed_count']
            mode = result.get('mode')

            # 1. Підсумок верхнього рівня
            if calc_count == 0:
                # Нічого не пораховано — це не помилка, але й не success
                if mode == 'single':
                    flash('Для цього виду метрики не пораховано (недостатньо даних).', 'warning')
                else:
                    flash('Жоден вид не пройшов перерахунок — недостатньо даних.', 'warning')
            elif mode == 'single':
                flash('Перерахунок завершено для 1 виду.', 'success')
            else:
                flash(
                    f'Метрики перераховано: {calc_count} вид(ів), '
                    f'пропущено: {fail_count}.',
                    'success' if fail_count == 0 else 'info'
                )

            # 2. Деталі по пропущених — конкретні причини
            for detail in result.get('failed_species_detail', [])[:5]:
                flash(detail['message'], 'info')
            extra = len(result.get('failed_species_detail', [])) - 5
            if extra > 0:
                flash(f'... та ще {extra} вид(ів) з недостатніми даними.', 'info')

        else:
            # Структуровані помилки — різний рівень залежно від причини
            reason = result.get('reason')
            msg = result.get('error', 'Невідома помилка')
            if reason in ('insufficient_data', 'no_eligible_species'):
                flash(msg, 'warning')
            else:
                flash(f'Помилка перерахунку метрик: {msg}', 'danger')

    except Exception as e:
        current_app.logger.error(f"Error in admin recalculate metrics: {e}")
        flash('Неочікувана помилка під час перерахунку метрик.', 'danger')
    
    return redirect(url_for('pam.evaluation_results', lang_code=lang_code))

@pam_bp.route('/<lang_code>/admin/verification/cleanup', methods=['POST'])
@login_required
@role_required('manager')
def admin_cleanup_verifications(lang_code):
    """Адміністраторський роут для видалення файлів завершених верифікацій."""
    from .pam_evaluation_utils import cleanup_completed_verifications
    
    try:
        current_app.logger.info(f"Verification cleanup started by admin {current_user.username}")
        
        result = cleanup_completed_verifications()
        
        if result['success']:
            flash(
                f'Очищення завершено! '
                f'Видалено файлів: {result["deleted_files"]}, '
                f'звільнено місця: {result["deleted_size_mb"]} MB',
                'success'
            )
            
            if result['errors']:
                error_msg = f'Помилки: {len(result["errors"])}'
                flash(error_msg, 'warning')
        else:
            flash(f'Помилка очищення: {result.get("error", "Невідома помилка")}', 'danger')
        
    except Exception as e:
        current_app.logger.error(f"Error in admin cleanup: {e}")
        flash('Неспідівана помилка під час очищення файлів.', 'danger')
    
    return redirect(url_for('pam.evaluation_results', lang_code=lang_code))

@pam_bp.route('/<lang_code>/api/verification/consensus-status')
@login_required
@role_required('manager')
def api_consensus_status(lang_code):
    """API для отримання статистики консенсусу верифікацій."""
    from .utils import get_pam_db_connection
    
    conn = None
    try:
        conn = get_pam_db_connection()
        
        status_stats = conn.execute("""
            SELECT status, COUNT(*) 
            FROM segments 
            GROUP BY status
        """).fetchall()
        
        consensus_stats = conn.execute("""
            SELECT 
                COUNT(CASE WHEN verification_count >= 2 THEN 1 END) as reached_consensus,
                COUNT(CASE WHEN verification_count = 1 THEN 1 END) as need_more_verifications,
                COUNT(CASE WHEN verification_count = 0 THEN 1 END) as no_verifications
            FROM segments
        """).fetchone()
        
        top_verifiers = conn.execute("""
            SELECT 
                sv.user_id,
                COUNT(*) as total_verifications,
                COUNT(CASE WHEN sv.verification_result = 1 THEN 1 END) as positive_verifications,
                ROUND(COUNT(CASE WHEN sv.verification_result = 1 THEN 1 END) * 100.0 / COUNT(*), 1) as positive_rate
            FROM segment_verifications sv
            GROUP BY sv.user_id
            ORDER BY COUNT(*) DESC
            LIMIT 5
        """).fetchall()
        
        if top_verifiers:
            user_ids = [v[0] for v in top_verifiers]
            from app.models import User
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username for u in users}
            
            top_verifiers_with_names = []
            for user_id, total, positive, rate in top_verifiers:
                top_verifiers_with_names.append({
                    'user_id': user_id,
                    'username': user_map.get(user_id, f'User {user_id}'),
                    'total_verifications': total,
                    'positive_verifications': positive,
                    'positive_rate': rate
                })
        else:
            top_verifiers_with_names = []
        
        return jsonify({
            'status_stats': dict(status_stats),
            'consensus_stats': {
                'reached_consensus': consensus_stats[0],
                'need_more_verifications': consensus_stats[1],
                'no_verifications': consensus_stats[2]
            },
            'top_verifiers': top_verifiers_with_names
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting consensus status: {e}")
        return jsonify({'error': 'Помилка отримання статистики консенсусу'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/audio/spectrograms/<int:segment_id>')
@login_required
@role_required('pam_verifier')
def serve_spectrogram_image(lang_code, segment_id):
    """Сервіс для віддачі зображень спектрограм."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        result = conn.execute(
            text("SELECT file_path FROM segments WHERE id = :segment_id"),
            {"segment_id": segment_id}
        ).fetchone()
        
        if not result:
            current_app.logger.error(f"Spectrogram request: Segment {segment_id} not found in DB")
            abort(404)
        
        audio_path = result[0]

        # --- ПОЧАТОК ТИМЧАСОВОЇ ЗАГЛУШКИ ДЛЯ ЛОКАЛЬНОЇ РОЗРОБКИ ---
        # local_upload_path = "C:/Users/IuriiStrus/GitHub_cloned_repos/myproject/pam_data_import/segments/"
        # server_base_path = '/home/yura/pam_data_import/segments/'
        # audio_path = audio_path.replace(server_base_path, local_upload_path)
        # print(f"Local Base Path : {local_upload_path}")
        # print(f"Final Path      : {audio_path}")
        # --- КІНЕЦЬ ТИМЧАСОВОЇ ЗАГЛУШКИ ---


        base_path, _ = os.path.splitext(audio_path)
        spectrogram_path = f"{base_path}.png"

        # Якщо зображення не існує, спробуємо його згенерувати "на льоту"
        if not os.path.exists(spectrogram_path):
            current_app.logger.info(f"Generating on-the-fly spectrogram for segment {segment_id}")
            generate_spectrogram_image(audio_path)

        # Перевіряємо ще раз, чи існує файл після генерації
        if not os.path.exists(spectrogram_path):
            current_app.logger.error(f"Failed to generate or find spectrogram for: {spectrogram_path}")
            abort(404)
        
        directory = os.path.dirname(spectrogram_path)
        filename = os.path.basename(spectrogram_path)
        
        return send_from_directory(directory, filename, as_attachment=False)
        
    except Exception as e:
        current_app.logger.error(f"Error serving spectrogram for segment {segment_id}: {e}")
        abort(500)
    finally:
        if conn:
            conn.close()

def process_spectrograms_background(app, segments, force_regenerate):
    # Потрібен контекст додатку для доступу до logger та конфігів, якщо треба
    with app.app_context():
        processed_count = 0
        failed_count = 0
        
        print(f"--- Початок фонової генерації: {len(segments)} файлів ---")
        
        for segment_id, audio_path in segments:
            # Викликаємо функцію генерації
            # Вона сама перевіряє наявність файлу та потребу перезапису
            if generate_spectrogram_image(audio_path, force_regenerate=force_regenerate):
                processed_count += 1
            else:
                failed_count += 1
                
        print(f"--- Фонова генерація завершена. Оброблено: {processed_count}, Помилок: {failed_count} ---")
        # Тут можна додати запис у лог або відправку email адміну

@pam_bp.route('/<lang_code>/admin/evaluation/build-spectrograms', methods=['POST'])
@login_required
@role_required('admin')
def admin_build_spectrograms(lang_code):
    """
    Асинхронний запуск генерації спектрограм.
    """
    conn = None
    try:
        mode = request.form.get('mode', 'missing')
        force_regenerate = (mode == 'all')
        
        conn = get_pam_db_connection()
        # Беремо всі шляхи
        segments = conn.execute(text(
            "SELECT id, file_path FROM segments WHERE status != 'archived'"
        )).fetchall()
        
        # Перетворюємо RowProxy на список кортежів або словників, 
        # щоб безпечно передати в потік (закриття conn не вплинуло на дані)
        segments_data = [(row[0], row[1]) for row in segments]
        
        # --- ЗАПУСК У ФОНІ ---
        # Використовуємо threading, щоб віддати відповідь браузеру миттєво
        # Передаємо current_app._get_current_object(), щоб потік мав доступ до реального app
        thread = threading.Thread(
            target=process_spectrograms_background,
            args=(current_app._get_current_object(), segments_data, force_regenerate)
        )
        thread.start()
        
        count_msg = f"{len(segments_data)} файлів"
        flash(
            f'Процес генерації спектрограм ({mode}) запущено у фоні для {count_msg}. '
            f'Перевірте консоль сервера або зачекайте кілька хвилин перед оновленням сторінки.',
            'info'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error starting build spectrograms: {e}")
        flash('Помилка запуску процесу.', 'danger')
    finally:
        if conn:
            conn.close()

    return redirect(request.referrer or url_for('pam.evaluation_results', lang_code=lang_code))

@pam_bp.route('/<lang_code>/api/pam/evaluation/thresholds')
def api_get_evaluation_thresholds(lang_code):
    """
    API для отримання словника з пороговими значеннями p0.95 для видів.
    Враховує тільки розрахунки з total_samples > 200.
    Повертає об'єкт: { "scientific_name": threshold_value, ... }
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # ОНОВЛЕНИЙ ЗАПИТ:
        # 1. Не використовує is_current.
        # 2. Фільтрує за total_samples > 200.
        # 3. Гарантує, що для кожного виду береться НАЙНОВІШИЙ розрахунок,
        #    який відповідає умовам, щоб уникнути дублікатів.
        query = text("""
            WITH RankedEvaluations AS (
                SELECT
                    e.species_id,
                    e.p0_95_threshold,
                    ROW_NUMBER() OVER(PARTITION BY e.species_id ORDER BY e.calculated_at DESC) as rn
                FROM evaluation e
                WHERE e.p0_95_threshold IS NOT NULL 
                  AND e.p0_95_threshold > 0 
                  AND e.p0_95_threshold < 1
                  AND e.total_samples > 200
            )
            SELECT
                s.scientific_name,
                re.p0_95_threshold
            FROM RankedEvaluations re
            JOIN species s ON re.species_id = s.species_id
            WHERE re.rn = 1
        """)
        
        db_result = conn.execute(query).mappings().fetchall()
        
        # Створюємо словник {назва_виду: поріг}
        thresholds = {
            row['scientific_name']: round(row['p0_95_threshold'], 2) 
            for row in db_result
        }
        
        return jsonify(thresholds)
        
    except Exception as e:
        current_app.logger.error(f"Error getting evaluation thresholds: {e}")
        return jsonify({'error': 'Failed to load evaluation thresholds'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/pam/manage-locations')
@login_required
@role_required('pam_verifier')
def manage_pam_locations(lang_code):
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        is_admin = current_user.has_role('admin')

        # 1. Беремо УСІ установи з Основної БД 
        if is_admin:
            all_inst_objects = Institution.query.order_by(Institution.name_uk).all()
        else:
            all_inst_objects = current_user.institutions
        
        # ДВОМОВНІСТЬ УСТАНОВ: Формуємо словник залежно від поточної мови
        if lang_code == 'uk':
            inst_names_map = {i.id: i.name_uk for i in all_inst_objects}
        else:
            inst_names_map = {i.id: (i.name_en or i.name_uk) for i in all_inst_objects}
            
        all_assignable_list =[{'id': i.id, 'name': inst_names_map[i.id]} for i in all_inst_objects]

        # 2. Отримуємо локації (з обома назвами) з ПАМ БД
        loc_query = text("""
            SELECT l.location_id, l.location_name, l.location_name_en, l.lat, l.lon, li.institution_id
            FROM locations l
            LEFT JOIN location_institutions li ON l.location_id = li.location_id
            ORDER BY l.location_name
        """)
        raw_rows = conn.execute(loc_query).fetchall()

        locations_dict = {}
        used_inst_ids = set()

        for row in raw_rows:
            lid = row.location_id
            if lid not in locations_dict:
                # ДВОМОВНІСТЬ ЛОКАЦІЙ
                loc_name = row.location_name
                if lang_code == 'en' and row.location_name_en:
                    loc_name = row.location_name_en
                    
                locations_dict[lid] = {
                    'location_id': lid,
                    'name': loc_name,
                    'latitude': float(row.lat),
                    'longitude': float(row.lon),
                    'inst_ids': [],
                    'has_name_en': bool(row.location_name_en and row.location_name_en.strip())
                }

            if row.institution_id:
                locations_dict[lid]['inst_ids'].append(row.institution_id)
                used_inst_ids.add(row.institution_id)

        # 3. Підтягуємо зв'язки локацій з біотопами
        biotope_links = conn.execute(text("SELECT location_id, biotope_id FROM location_biotopes")).fetchall()
        loc_biotope_map = {}
        for bl in biotope_links:
            loc_biotope_map.setdefault(bl.location_id, []).append(bl.biotope_id)
        for lid, loc in locations_dict.items():
            loc['biotope_ids'] = loc_biotope_map.get(lid, [])

        # 4. Фільтруємо те, що бачить менеджер
        user_inst_ids =[i.id for i in current_user.institutions]
        if is_admin:
            final_locations = list(locations_dict.values())
        else:
            final_locations =[loc for loc in locations_dict.values() if any(i in user_inst_ids for i in loc['inst_ids'])]

        # 5. Список для верхнього фільтра
        filter_institutions = [{'id': i_id, 'name': inst_names_map[i_id]}
                               for i_id in used_inst_ids if i_id in inst_names_map]

        # 6. Список біотопів для форми
        biotopes_result = conn.execute(text("SELECT id, name_ua, name_en FROM biotopes ORDER BY name_ua")).fetchall()
        biotopes = [dict(row._mapping) for row in biotopes_result]

        # 7. Довідники для журналу обслуговування
        battery_types = conn.execute(text("SELECT id, name_ua, name_en FROM battery_types ORDER BY name_ua")).fetchall()
        sd_card_statuses = conn.execute(text("SELECT id, name_ua, name_en FROM sd_card_status ORDER BY id")).fetchall()
        visit_purposes = conn.execute(text("SELECT id, name_ua, name_en FROM visit_purposes ORDER BY id")).fetchall()

        can_edit = current_user.has_role('manager')

        return render_template('manage_pam_locations.html',
                               locations=final_locations,
                               biotopes=biotopes,
                               available_institutions=all_assignable_list,
                               filter_institutions=filter_institutions,
                               locations_json_string=json.dumps(final_locations),
                               battery_types=battery_types,
                               sd_card_statuses=sd_card_statuses,
                               visit_purposes=visit_purposes,
                               can_edit=can_edit,
                               geoserver_url=current_app.config.get('GEOSERVER_URL', ''))
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/pam/api/location/<int:location_id>')
@login_required
@role_required('manager')
def get_pam_location_details(lang_code, location_id):
    """API для отримання деталей конкретної локації ПАМ."""
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        
        location = conn.execute(text("""
            SELECT location_id, location_name, location_name_en, lat, lon, state_province
            FROM locations WHERE location_id = :id
        """), {'id': location_id}).fetchone()
        
        if not location:
            return jsonify({'error': 'Локацію не знайдено.'}), 404
        
        # Біотопи
        b_ids = conn.execute(text("SELECT biotope_id FROM location_biotopes WHERE location_id = :id"), {'id': location_id}).fetchall()
        
        # Установи
        i_ids = conn.execute(text("SELECT institution_id FROM location_institutions WHERE location_id = :id"), {'id': location_id}).fetchall()
        
        return jsonify({
            'id': location.location_id,
            'name': location.location_name,
            'name_en': location.location_name_en or '',
            'state_province': location.state_province or '',
            'latitude': float(location.lat),
            'longitude': float(location.lon),
            'biotope_ids': [row[0] for row in b_ids],
            'institution_ids': [row[0] for row in i_ids],
        })
    except Exception as e:
        return jsonify({'error': 'Помилка отримання даних.'}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/pam/api/location/create', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def api_create_pam_location(lang_code):
    """API для ручного створення нової локації ПАМ."""
    g.lang_code = lang_code
    conn = None
    try:
        data = request.json
        name = (data.get('name') or '').strip()
        name_en = (data.get('name_en') or '').strip() or None
        lat = data.get('lat')
        lon = data.get('lon')
        institution_ids = data.get('institution_ids', [])
        biotope_ids = data.get('biotope_ids', [])

        if not name or lat is None or lon is None:
            return jsonify({'success': False, 'error': 'Назва та координати обов\'язкові.'}), 400

        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Некоректні координати.'}), 400

        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        if not is_admin and institution_ids and not all(i_id in user_inst_ids for i_id in institution_ids):
            return jsonify({'success': False, 'error': 'Доступ заборонено: можна призначати лише свої установи.'}), 403

        state_province = (data.get('state_province') or '').strip() or None

        conn = get_pam_db_connection()
        with conn.begin():
            result = conn.execute(text("""
                INSERT INTO locations
                    (location_name, location_name_en, lat, lon, geom, visibility_level, state_province, created_at)
                VALUES
                    (:name, :name_en, :lat, :lon,
                     ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                     0, :state_province, NOW())
                RETURNING location_id
            """), {
                'name': name, 'name_en': name_en,
                'lat': lat, 'lon': lon,
                'state_province': state_province
            })
            new_location_id = result.fetchone()[0]

            if institution_ids:
                conn.execute(
                    text("INSERT INTO location_institutions (location_id, institution_id) VALUES (:l_id, :i_id)"),
                    [{'l_id': new_location_id, 'i_id': i_id} for i_id in institution_ids]
                )

            if biotope_ids:
                conn.execute(
                    text("INSERT INTO location_biotopes (location_id, biotope_id) VALUES (:l_id, :b_id)"),
                    [{'l_id': new_location_id, 'b_id': b_id} for b_id in biotope_ids]
                )

        current_app.logger.info(
            f"User {current_user.username} created new PAM location '{name}' (id={new_location_id})"
        )
        return jsonify({'success': True, 'location_id': new_location_id, 'message': 'Локацію успішно створено!'})
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.error(f"Error creating PAM location: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Помилка сервера при створенні локації.'}), 500
    finally:
        if conn:
            conn.close()


@pam_bp.route('/<lang_code>/pam/api/update-location/<int:location_id>', methods=['POST'])
@login_required
@role_required('manager')
def update_pam_location(lang_code, location_id):
    conn = None
    try:
        data = request.json
        conn = get_pam_db_connection()
        new_inst_ids = data.get('institution_ids', [])
        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        if not is_admin and not all(i_id in user_inst_ids for i_id in new_inst_ids):
            return jsonify({'success': False, 'error': 'Доступ заборонено'}), 403

        with conn.begin(): 
            # Оновлюємо назви
            conn.execute(text("UPDATE locations SET location_name = :n, location_name_en = :ne WHERE location_id = :id"),
                         {'n': data.get('name'), 'ne': data.get('name_en'), 'id': location_id})
            
            # Оновлюємо зв'язки в ПАМ БД (просто записуємо ID)
            if is_admin:
                conn.execute(text("DELETE FROM location_institutions WHERE location_id = :id"), {'id': location_id})
            else:
                conn.execute(text("DELETE FROM location_institutions WHERE location_id = :id AND institution_id = ANY(:u_ids)"),
                             {'id': location_id, 'u_ids': user_inst_ids})
            
            if new_inst_ids:
                conn.execute(text("INSERT INTO location_institutions (location_id, institution_id) VALUES (:l_id, :i_id)"),
                             [{'l_id': location_id, 'i_id': i_id} for i_id in new_inst_ids])

        return jsonify({'success': True, 'message': 'Оновлено'})
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/admin/evaluation/convert-to-flac', methods=['POST'])
@login_required
@role_required('admin')
def admin_convert_to_flac(lang_code):
    """Адміністраторський роут для конвертації WAV сегментів у FLAC."""
    from .pam_evaluation_utils import convert_wav_to_flac
    
    try:
        current_app.logger.info(f"WAV to FLAC conversion started by admin {current_user.username}")
        
        result = convert_wav_to_flac()
        
        if result['success']:
            flash(
                f"Конвертацію завершено! Успішно: {result['converted_count']}, помилок: {result['failed_count']}. "
                f"{result.get('message', '')}",
                'success'
            )
            
            if result['errors']:
                error_count = len(result['errors'])
                flash(f'Під час конвертації виникло {error_count} помилок. Деталі див. у логах.', 'warning')
        else:
            flash(f"Помилка конвертації: {result.get('error', 'Невідома помилка')}", 'danger')
        
    except Exception as e:
        current_app.logger.error(f"Error in admin convert to flac: {e}")
        flash('Неспідівана помилка під час конвертації файлів.', 'danger')
    
    return redirect(url_for('pam.evaluation_results', lang_code=lang_code))

@pam_bp.route('/<lang_code>/api/pam/verification/top-verifiers')
@login_required
def api_top_verifiers(lang_code):
    """
    API для отримання рейтингу верифікаторів на основі фільтрів.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Отримуємо ті ж самі фільтри, що й для сегментів
        species_id = request.args.get('species_id', type=int)
        status = request.args.get('status', 'all')
        
        # Базовий запит
        base_query = """
            SELECT 
                sv.user_id,
                COUNT(sv.id) as verification_count
            FROM segment_verifications sv
            JOIN segments seg ON sv.segment_id = seg.id
        """
        
        conditions = []
        params = {}
        
        if species_id:
            conditions.append("seg.species_id = :species_id")
            params['species_id'] = species_id
            
        if status != 'all':
            conditions.append("seg.status = :status")
            params['status'] = status
            
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
            
        # Формуємо кінцевий запит
        query = text(f"""
            {base_query}
            {where_clause}
            GROUP BY sv.user_id
            ORDER BY verification_count DESC
            LIMIT 20
        """)
        
        # Виконуємо запит до PAM_DB
        db_results = conn.execute(query, params).fetchall()
        
        if not db_results:
            return jsonify({'verifiers': []})

        # Двоетапний процес: 
        # 1. Отримали ID користувачів та їх статистику з PAM_DB.
        # 2. Тепер отримаємо їхні імена з основної бази даних.
        user_ids = [row[0] for row in db_results]
        
        # Запит до основної бази даних через модель User
        users = User.query.filter(User.id.in_(user_ids)).all()
        user_map = {u.id: u.username for u in users}

        # Формуємо фінальний результат
        verifiers_data = []
        for user_id, count in db_results:
            verifiers_data.append({
                'username': user_map.get(user_id, f'User #{user_id}'),
                'verifications_count': count
            })

        return jsonify({'verifiers': verifiers_data})

    except Exception as e:
        current_app.logger.error(f"Error getting top verifiers: {e}")
        return jsonify({'error': 'Помилка отримання рейтингу'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/pam/trends')
def pam_trends(lang_code):
    """
    Сторінка для візуалізації багаторічних трендів видів.
    """
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Отримуємо список видів, для яких є розраховані тренди
        species_query = text("""
            SELECT DISTINCT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            FROM species s
            JOIN species_yearly_trends t ON s.species_id = t.species_id
            ORDER BY s.scientific_name
        """)
        species_result = conn.execute(species_query).fetchall()

        available_species = []
        for row in species_result:
            display_name = row.scientific_name
            if lang_code == 'uk' and row.common_name_uk:
                display_name = f"{row.common_name_uk} ({row.scientific_name})"
            elif lang_code == 'en' and row.common_name_en:
                display_name = f"{row.common_name_en} ({row.scientific_name})"
            available_species.append({'id': row.species_id, 'text': display_name})

        # Отримуємо список доступних років
        years_query = text("SELECT DISTINCT year FROM species_yearly_trends ORDER BY year DESC")
        available_years = [row[0] for row in conn.execute(years_query).fetchall()]
        
        start_year = available_years[-1] if available_years else datetime.now().year
        end_year = available_years[0] if available_years else datetime.now().year

        return render_template('pam_yearly_trends.html',
                               available_species=available_species,
                               available_years=available_years,
                               start_year=start_year,
                               end_year=end_year)

    except Exception as e:
        current_app.logger.error(f"Error loading PAM trends page: {e}", exc_info=True)
        flash('Помилка завантаження сторінки аналізу трендів.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/api/pam/yearly-trends')
def api_pam_yearly_trends(lang_code):
    """
    API для отримання даних для графіка багаторічної динаміки.
    """
    g.lang_code = lang_code
    conn = None
    try:
        species_id = request.args.get('species_id', type=int)
        start_year = request.args.get('start_year', type=int)
        end_year = request.args.get('end_year', type=int)

        if not all([species_id, start_year, end_year]):
            return jsonify({'error': 'Missing required parameters'}), 400

        conn = get_pam_db_connection()
        
        query = text("""
            SELECT year, mean_rai, lower_ci, upper_ci
            FROM species_yearly_trends
            WHERE species_id = :species_id 
              AND year BETWEEN :start_year AND :end_year
            ORDER BY year ASC
        """)
        
        result = conn.execute(query, {
            'species_id': species_id,
            'start_year': start_year,
            'end_year': end_year
        }).mappings().fetchall()
        
        # Конвертуємо результат в список словників, готовий для JSON
        trend_data = [dict(row) for row in result]
        
        return jsonify(trend_data)

    except Exception as e:
        current_app.logger.error(f"Error in yearly trends API: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/pam/species-dashboard')
def pam_species_dashboard(lang_code):
    """
    Сторінка детального аналізу по видах для даних ПАМ (сезонна + річна динаміка).
    """
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Отримуємо види, для яких є розраховані дані в обох таблицях
        species_query = text("""
            SELECT DISTINCT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            FROM species s
            JOIN species_yearly_trends t ON s.species_id = t.species_id
            WHERE EXISTS (SELECT 1 FROM analysis_intermediate ai WHERE ai.species_id = s.species_id)
            ORDER BY s.scientific_name
        """)
        species_result = conn.execute(species_query).fetchall()

        available_species = []
        for row in species_result:
            display_name = row.scientific_name
            if lang_code == 'uk' and row.common_name_uk:
                display_name = f"{row.common_name_uk} ({row.scientific_name})"
            elif lang_code == 'en' and row.common_name_en:
                display_name = f"{row.common_name_en} ({row.scientific_name})"
            available_species.append({'id': row.species_id, 'text': display_name})

        # Отримуємо список доступних років з трендів
        years_query = text("SELECT DISTINCT year FROM species_yearly_trends ORDER BY year ASC")
        available_years = [row[0] for row in conn.execute(years_query).fetchall()]
        
        start_year = available_years[0] if available_years else datetime.now().year - 5
        end_year = available_years[-1] if available_years else datetime.now().year

        # ВАЖЛИВО: рендеримо новий шаблон pam_species_dashboard.html
        return render_template('pam_species_dashboard.html',
                               available_species=available_species,
                               available_years=available_years,
                               start_year=start_year,
                               end_year=end_year)

    except Exception as e:
        current_app.logger.error(f"Error loading PAM species dashboard page: {e}", exc_info=True)
        flash('Помилка завантаження сторінки детального аналізу.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn:
            conn.close()

@pam_bp.route('/<lang_code>/api/pam/species-dynamics')
def api_pam_species_dynamics(lang_code):
    """
    API для отримання даних трендів з урахуванням інституції.
    """
    g.lang_code = lang_code
    conn = None
    try:
        species_id = request.args.get('species_id', type=int)
        start_year = request.args.get('start_year', type=int)
        end_year = request.args.get('end_year', type=int)
        
        # Отримуємо ID інституції (якщо передано)
        inst_param = request.args.get('institution_id')
        institution_id = int(inst_param) if inst_param and inst_param.isdigit() else None

        if not all([species_id, start_year, end_year]):
            return jsonify({'error': 'Missing required parameters'}), 400

        conn = get_pam_db_connection()
        params = {
            'species_id': species_id,
            'start_year': start_year,
            'end_year': end_year
        }
        
        # --- 1. Сезонна активність (Динамічний розрахунок) ---
        # Таблиця analysis_intermediate прив'язана до location_id. 
        # Якщо обрана установа - фільтруємо через JOIN.
        seasonal_join = ""
        seasonal_where = ""
        
        if institution_id is not None:
            seasonal_join = "JOIN location_institutions li ON ai.location_id = li.location_id"
            seasonal_where = "AND li.institution_id = :inst_id"
            params['inst_id'] = institution_id

        seasonal_query = text(f"""
            SELECT ai.year, ai.month, SUM(ai.detection_count) as count
            FROM analysis_intermediate ai
            {seasonal_join}
            WHERE ai.species_id = :species_id 
              AND ai.year BETWEEN :start_year AND :end_year
              {seasonal_where}
            GROUP BY ai.year, ai.month 
            ORDER BY ai.year, ai.month
        """)
        seasonal_result = conn.execute(seasonal_query, params).mappings().fetchall()
        seasonal_data = [dict(row) for row in seasonal_result]
        
        # --- 2. Річна динаміка (Пре-калькульовані дані) ---
        # Вибираємо або глобальний тренд (IS NULL), або конкретної установи (= id)
        trend_condition = "institution_id IS NULL" if institution_id is None else "institution_id = :inst_id"
        
        yearly_query = text(f"""
            SELECT year, mean_rai, lower_ci, upper_ci
            FROM species_yearly_trends
            WHERE species_id = :species_id 
              AND year BETWEEN :start_year AND :end_year
              AND {trend_condition}
            ORDER BY year ASC
        """)
        yearly_result = conn.execute(yearly_query, params).mappings().fetchall()
        yearly_data = [{'year': r['year'], 'mean_dr_index': r['mean_rai'], 'lower_ci': r['lower_ci'], 'upper_ci': r['upper_ci']} for r in yearly_result]
        
        # --- 3. Активність по біотопах (Пре-калькульовані дані) ---
        biotope_condition = "sba.institution_id IS NULL" if institution_id is None else "sba.institution_id = :inst_id"

        biotope_query = text(f"""
            SELECT
                sba.year,
                sba.detection_count,
                sba.effort_hours,
                CASE WHEN '{lang_code}' = 'uk' THEN b.name_ua ELSE b.name_en END as biotope_name
            FROM species_biotope_yearly_activity sba
            JOIN biotopes b ON sba.biotope_id = b.id
            WHERE sba.species_id = :species_id 
              AND sba.year BETWEEN :start_year AND :end_year
              AND {biotope_condition}
            ORDER BY sba.year, biotope_name
        """)
        biotope_result = conn.execute(biotope_query, params).mappings().fetchall()
        
        biotope_data = []
        for row in biotope_result:
            data_point = dict(row)
            count = data_point.get('detection_count', 0)
            effort = data_point.get('effort_hours', 0)
            relative_activity = (count / effort) if effort > 0 else 0
            data_point['relative_activity'] = relative_activity
            biotope_data.append(data_point)

        return jsonify({
            'seasonal_activity': seasonal_data, 
            'yearly_trend': yearly_data,
            'biotope_activity': biotope_data
        })

    except Exception as e:
        current_app.logger.error(f"Error in PAM species dynamics API: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        if conn:
            conn.close()

            
@pam_bp.route('/<lang_code>/pam/data-export')
@login_required
@role_required('manager')
def pam_data_export(lang_code):
    """
    Сторінка для підготовки та експорту даних.
    Фільтри завантажуються динамічно через API.
    """
    g.lang_code = lang_code
    try:
        # Тепер нам не потрібно завантажувати тут список видів
        return render_template('pam_data_export.html')
    except Exception as e:
        current_app.logger.error(f"Error loading Data export page: {e}", exc_info=True)
        flash('Помилка завантаження сторінки експорту.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    
@pam_bp.route('/<lang_code>/api/pam/get-taxonomic-filters')
@login_required
def api_get_taxonomic_filters(lang_code):
    # Цей код з попередньої відповіді є правильним і залишається без змін
    # ... (код функції api_get_taxonomic_filters) ...
    conn = None
    try:
        conn = get_pam_db_connection()
        p = {
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus'),
        }

        def fetch_distinct(column, filter_by):
            conditions = [f"s.{column} IS NOT NULL"]
            params = {}
            for key, value in filter_by.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value
            
            where_clause = "WHERE " + " AND ".join(conditions)
            query = text(f"SELECT DISTINCT s.{column} FROM species s {where_clause} ORDER BY s.{column}")
            return [row[0] for row in conn.execute(query, params).fetchall()]

        response_data = {
            'classes': fetch_distinct('class', {}),
            'orders': fetch_distinct('order_rank', {'class': p['class']}),
            'families': fetch_distinct('family', {'class': p['class'], 'order': p['order']}),
            'genera': fetch_distinct('genus', {'class': p['class'], 'order': p['order'], 'family': p['family']}),
        }
        
        species_filters = {k: v for k, v in p.items() if v}
        response_data['species'] = fetch_distinct_species(conn, lang_code, species_filters)
        
        return jsonify(response_data)
    except Exception as e:
        current_app.logger.error(f"Error fetching taxonomic filters: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load filter data'}), 500
    finally:
        if conn:
            conn.close()

def fetch_distinct_species(conn, lang_code, filters):
    # ... (код допоміжної функції) ...
    conditions = []
    params = {}
    for key, value in filters.items():
        if value:
            db_column = 'order_rank' if key == 'order' else key
            conditions.append(f"s.{db_column} = :{key}")
            params[key] = value

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    query = text(f"""
        SELECT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
        FROM species s {where_clause} ORDER BY s.scientific_name
    """)
    result = conn.execute(query, params).fetchall()
    
    species_list = []
    for row in result:
        display_name = row.scientific_name
        if lang_code == 'uk' and row.common_name_uk:
            display_name = f"{row.common_name_uk} ({row.scientific_name})"
        elif lang_code == 'en' and row.common_name_en:
            display_name = f"{row.common_name_en} ({row.scientific_name})"
        species_list.append({'id': row.species_id, 'text': display_name})
    return species_list

@pam_bp.route('/<lang_code>/api/pam/data-preview')
@login_required
@role_required('manager')
def api_data_preview(lang_code):
    """API для попереднього перегляду даних."""
    try:
        filters = {
            'species_ids': [int(sid) for sid in request.args.get('species_ids', '').split(',') if sid],
            'genus': request.args.get('genus'),
            'family': request.args.get('family'),
            'order': request.args.get('order'),
            'class': request.args.get('class'),
            'start_date': request.args.get('start_date'),
            'end_date': request.args.get('end_date'),
            'confidence': float(request.args.get('confidence', 0.75)),
            
            # --- НОВІ ПАРАМЕТРИ ---
            'export_mode': request.args.get('export_mode', 'standard'),
            'aggregation': request.args.get('aggregation', 'none'),
            'aggregation_minutes': request.args.get('aggregation_minutes', 60), # Дефолт 60 хв
            'institution_code': request.args.get('institution_code', 'RSNR'),
            'detector_name': request.args.get('detector_name', 'BirdNET 2.4')
        }
        
        result = get_occurrence_data(filters, limit=20)
        
        return jsonify({
            'preview_data': result['data'],
            'total_count': result['total_count']
        })
    except Exception as e:
        current_app.logger.error(f"Preview error: {e}")
        return jsonify({'error': 'Помилка на сервері при підготовці даних.'}), 500

@pam_bp.route('/<lang_code>/api/pam/data-download')
def api_data_download(lang_code):
    try:
        filters = {
            'species_ids': [int(sid) for sid in request.args.get('species_ids', '').split(',') if sid],
            'genus': request.args.get('genus'),
            'family': request.args.get('family'),
            'order': request.args.get('order'),
            'class': request.args.get('class'),
            'start_date': request.args.get('start_date'),
            'end_date': request.args.get('end_date'),
            'confidence': float(request.args.get('confidence', 0.75)),
            
            # --- НОВІ ПАРАМЕТРИ ---
            'export_mode': request.args.get('export_mode', 'standard'),
            'aggregation': request.args.get('aggregation', 'none'),
            'aggregation_minutes': request.args.get('aggregation_minutes', 60),
            'institution_code': request.args.get('institution_code', 'RSNR'),
            'detector_name': request.args.get('detector_name', 'BirdNET 2.4')
        }
        
        result = get_occurrence_data(filters, limit=None)
        data = result['data']

        if not data:
            return "Дані за вибраними критеріями не знайдено.", 404
            
        output = io.StringIO()
        if data:
            writer = csv.DictWriter(output, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
            
        output.seek(0)
        return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=occurrence_data_export.csv"})
    except Exception as e:
        current_app.logger.error(f"Download error: {e}")
        return "Помилка на сервері при генерації файлу.", 500

@pam_bp.route('/<lang_code>/pam/yearly-table')
def pam_yearly_table(lang_code):
    """
    Сторінка для візуалізації багаторічних трендів видів.
    ОНОВЛЕНО: Тепер завантажує лише "каркас" сторінки, фільтри завантажуються динамічно.
    """
    g.lang_code = lang_code
    try:
        # Нам більше не потрібно тут завантажувати роки, це зробить API
        return render_template('pam_yearly_table.html')
    except Exception as e:
        current_app.logger.error(f"Error loading PAM trends page: {e}", exc_info=True)
        flash('Помилка завантаження сторінки аналізу трендів.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))

@pam_bp.route('/<lang_code>/api/pam/get-trends-filters')
def api_get_trends_filters(lang_code):
    """
    API для отримання даних фільтрів на сторінці Огляду (Overview).
    ОНОВЛЕНО: Додано підтримку institution_id.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # 1. Збір параметрів
        institution_id = request.args.get('institution_id', '')
        
        p = {
            'start_year': request.args.get('start_year', type=int),
            'end_year': request.args.get('end_year', type=int),
            'locations': [int(id) for id in request.args.get('locations', '').split(',') if id],
            'biotopes': [int(id) for id in request.args.get('biotopes', '').split(',') if id],
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus')
        }

        # 2. Базові права доступу + Вибрана установа
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Використовуємо нашу універсальну функцію
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
        
        # 3. Отримуємо список доступних установ (для дропдауна)
        # Враховуємо права доступу (але не selected_institution, щоб можна було перемикати)
        base_access_cond, base_access_params = get_institution_filter(user_inst_ids, is_admin)
        
        inst_query = f"""
            SELECT DISTINCT i.id, i.name_uk, i.name_en, i.code
            FROM institutions i
            JOIN location_institutions li ON i.id = li.institution_id
            JOIN locations l ON li.location_id = l.location_id
            JOIN recordings r ON l.location_id = r.location_id
            WHERE {base_access_cond}
            ORDER BY i.name_uk
        """
        inst_result = conn.execute(text(inst_query), base_access_params).mappings().fetchall()
        institutions = []
        for row in inst_result:
            name = row['name_uk'] if lang_code == 'uk' and row['name_uk'] else row['name_en']
            institutions.append({'id': row['id'], 'text': name})

        # 4. Роки (Фільтруємо по установі)
        # Додаємо inst_condition до запиту років
        years_query = text(f"""
            SELECT DISTINCT EXTRACT(YEAR FROM r.datetime_start)::integer as year 
            FROM recordings r
            JOIN locations l ON r.location_id = l.location_id
            WHERE r.datetime_start IS NOT NULL 
            AND {inst_condition}
            ORDER BY year DESC
        """)
        all_years = [row.year for row in conn.execute(years_query, inst_params).fetchall()]
        
        response_data = {
            'years': all_years,
            'institutions': institutions # <-- Повертаємо список установ
        }

        # --- Підготовка умов для каскадних фільтрів ---
        base_joins = " FROM recordings r JOIN locations l ON r.location_id = l.location_id "
        base_conditions = [inst_condition] # <-- Додаємо умову установи сюди
        params = inst_params.copy()

        if p['start_year'] and p['end_year']:
            base_conditions.append("EXTRACT(YEAR FROM r.datetime_start) BETWEEN :start_year AND :end_year")
            params['start_year'] = p['start_year']
            params['end_year'] = p['end_year']

        # 5. Біотопи
        biotope_params = params.copy()
        biotope_joins = base_joins + " JOIN location_biotopes lb ON l.location_id = lb.location_id "
        biotope_conditions = base_conditions.copy()
        if p['locations']:
            biotope_conditions.append("l.location_id = ANY(:locations)")
            biotope_params['locations'] = p['locations']
        
        where_clause = " WHERE " + " AND ".join(biotope_conditions)
        biotopes_query = text(f"""
            SELECT DISTINCT b.id, b.name_ua, b.name_en
            {biotope_joins}
            JOIN biotopes b ON lb.biotope_id = b.id
            {where_clause} ORDER BY b.name_ua
        """)
        biotopes_result = conn.execute(biotopes_query, biotope_params).mappings().fetchall()
        response_data['biotopes'] = [{'id': r['id'], 'text': r['name_ua'] if lang_code == 'uk' else r['name_en']} for r in biotopes_result]

        # 6. Локації
        location_params = params.copy()
        location_joins = base_joins
        location_conditions = base_conditions.copy()
        if p['biotopes']:
            location_joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id "
            location_conditions.append("lb.biotope_id = ANY(:biotopes)")
            location_params['biotopes'] = p['biotopes']
            
        where_clause = " WHERE " + " AND ".join(location_conditions)
        locations_query = text(f"""
            SELECT DISTINCT l.location_id, l.location_name, l.location_name_en
            {location_joins} {where_clause} ORDER BY l.location_name
        """)
        locations_result = conn.execute(locations_query, location_params).mappings().fetchall()
        response_data['locations'] = [{'id': r['location_id'], 'text': r['location_name'] if lang_code != 'en' or not r['location_name_en'] else r['location_name_en']} for r in locations_result]

        # 7. Таксономія (Тут складніше, бо таблиця species не зв'язана прямо з recordings в цьому контексті, 
        # але ми повертаємо доступні таксони глобально або фільтруємо через recordings->detections->species, 
        # що важко. Зазвичай таксономічні фільтри лишають глобальними або фільтрують окремо. 
        # Залишимо поки логіку "наявних видів", але якщо треба строго по установі - треба джойнити detections)
        
        def fetch_distinct_taxa(column, filter_by):
            # Перетворюємо рядок на f-рядок, додавши літеру 'f'
            taxa_conditions = [f"s.{column} IS NOT NULL"]
            taxa_params = {}
            for key, value in filter_by.items():
                if value:
                    db_column = 'order_rank' if key == 'order' else key
                    taxa_conditions.append(f"s.{db_column} = :{key}")
                    taxa_params[key] = value
            where_clause = "WHERE " + " AND ".join(taxa_conditions)
            query = text(f"SELECT DISTINCT s.{column} FROM species s {where_clause} ORDER BY s.{column}")
            return [row[0] for row in conn.execute(query, taxa_params).fetchall()]
        
        response_data['classes'] = fetch_distinct_taxa('class', {})
        response_data['orders'] = fetch_distinct_taxa('order_rank', {'class': p['class']})
        response_data['families'] = fetch_distinct_taxa('family', {'class': p['class'], 'order': p['order']})
        response_data['genera'] = fetch_distinct_taxa('genus', {'class': p['class'], 'order': p['order'], 'family': p['family']})

        return jsonify(response_data)
        
    except Exception as e:
        current_app.logger.error(f"Error fetching trends filters: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load filter data'}), 500
    finally:
        if conn: conn.close()


@pam_bp.route('/<lang_code>/api/pam/yearly-trends-table')
def api_yearly_trends_table(lang_code):
    conn = None
    try:
        # 1. Параметри
        start_year = request.args.get('start_year', type=int)
        end_year = request.args.get('end_year', type=int)
        confidence = request.args.get('confidence', 0.95, type=float)
        min_detections = request.args.get('min_detections', 5, type=int)
        
        # Обробка множинного вибору установ
        inst_ids_param = request.args.get('institution_id', '')
        inst_ids = [int(i) for i in inst_ids_param.split(',') if i.strip().isdigit()]
        
        location_ids = [int(id) for id in request.args.get('locations', '').split(',') if id] or None
        biotope_ids = [int(id) for id in request.args.get('biotopes', '').split(',') if id] or None
        
        if not all([start_year, end_year]): return jsonify({'error': 'Years required.'}), 400
        params = {'start_year': start_year, 'end_year': end_year, 'confidence': confidence}

        conn = get_pam_db_connection()

        # --- Динамічні фрагменти SQL для ІНСТИТУЦІЙ ---
        inst_join = ""
        inst_where = ""
        if inst_ids:
            inst_join = " JOIN location_institutions li ON r.location_id = li.location_id "
            inst_where = " AND li.institution_id = ANY(:inst_ids) "
            params['inst_ids'] = inst_ids

        # --- 2. РОЗРАХУНОК ЗУСИЛЛЯ (Denomitor / Effort) ---
        # Тут ми рахуємо, скільки всього годин записали ОБРАНІ установи
        effort_joins = " JOIN locations l ON r.location_id = l.location_id " + inst_join
        if biotope_ids:
            effort_joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id "

        effort_conditions = "WHERE EXTRACT(YEAR FROM r.datetime_start) BETWEEN :start_year AND :end_year " + inst_where
        if location_ids: effort_conditions += " AND l.location_id = ANY(:location_ids) "
        if biotope_ids:   effort_conditions += " AND lb.biotope_id = ANY(:biotope_ids) "

        effort_query = text(f"""
            SELECT 
                EXTRACT(YEAR FROM r.datetime_start)::integer as year, 
                COUNT(r.recording_id) * (5.0 / 60.0) as effort_hours
            FROM recordings r 
            {effort_joins}
            {effort_conditions}
            GROUP BY year
        """)
        
        effort_result = conn.execute(effort_query, params).mappings().fetchall()
        effort_by_year = {row['year']: float(row['effort_hours']) for row in effort_result}
        total_effort = sum(effort_by_year.values())

        # --- 3. РОЗРАХУНОК ДЕТЕКЦІЙ (Numerator / Detections) ---
        detections_joins = " JOIN locations l ON r.location_id = l.location_id " + inst_join
        if biotope_ids:
            detections_joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id "

        # Базові умови
        det_conditions = [
            "d.confidence >= :confidence",
            "EXTRACT(YEAR FROM r.datetime_start) BETWEEN :start_year AND :end_year",
            inst_where.replace("AND", "") # прибираємо перший AND для списку
        ]
        if location_ids: det_conditions.append("l.location_id = ANY(:location_ids)")
        if biotope_ids:   det_conditions.append("lb.biotope_id = ANY(:biotope_ids)")
        
        # Таксономічні фільтри
        taxo_filters = {'class': request.args.get('class'), 'order': request.args.get('order'), 
                        'family': request.args.get('family'), 'genus': request.args.get('genus')}
        for key, value in taxo_filters.items():
            if value:
                db_col = 'order_rank' if key == 'order' else key
                det_conditions.append(f"s.{db_col} = :{key}")
                params[key] = value

        where_clause = " WHERE " + " AND ".join([c for c in det_conditions if c.strip()])

        detections_query = text(f"""
            SELECT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en,
                   EXTRACT(YEAR FROM r.datetime_start)::integer as year, 
                   COUNT(d.detection_id) as detection_count
            FROM detections d 
            JOIN species s ON d.species_id = s.species_id 
            JOIN recordings r ON d.recording_id = r.recording_id 
            {detections_joins}
            {where_clause}
            GROUP BY s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en, year 
            ORDER BY s.scientific_name, year
        """)
        
        detections_result = conn.execute(detections_query, params).mappings().fetchall()
        
        # --- 4. ОБРОБКА ТА НОРМАЛІЗАЦІЯ ---
        data_by_species = {}
        total_counts = {}

        for row in detections_result:
            if row['detection_count'] < min_detections: continue
            
            sid = row['species_id']
            year = row['year']
            
            if sid not in data_by_species:
                name = row['scientific_name']
                if lang_code == 'uk' and row['common_name_uk']: name = f"{row['common_name_uk']} ({row['scientific_name']})"
                data_by_species[sid] = {'display_name': name, 'values': {}}
                total_counts[sid] = 0
            
            # Нормалізація: (кількість / зусилля установи) * 24 години
            effort = effort_by_year.get(year, 0)
            if effort > 0:
                normalized = (row['detection_count'] / effort) * 24
                data_by_species[sid]['values'][year] = round(normalized, 2)
                total_counts[sid] += row['detection_count']

        # Формування фінального списку
        all_years = list(range(start_year, end_year + 1))
        result_list = []
        for sid, data in data_by_species.items():
            # Загальний показник за весь період
            data['total_normalized'] = round((total_counts[sid] / total_effort * 24), 2) if total_effort > 0 else 0
            result_list.append(data)

        result_list.sort(key=lambda x: x['display_name'])
        return jsonify({'years': all_years, 'species_data': result_list})

    except Exception as e:
        current_app.logger.error(f"Error in yearly trends table: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/api/pam/locations-with-status')
@login_required
def api_get_pam_locations_with_status(lang_code):
    """
    API, що повертає список локацій ПАМ з їхнім прогнозованим статусом.
    Підтримує фільтрацію за установами: ?institution_id=X
    """
    g.lang_code = lang_code

    is_admin = current_user.has_role('admin')
    user_inst_ids = [inst.id for inst in current_user.institutions]
    selected_inst_id = request.args.get('institution_id', '')

    # Будуємо SQL-умову фільтрації за установами
    if is_admin:
        inst_condition = "1=1"
        inst_params = {}
    elif user_inst_ids:
        inst_condition = """EXISTS (
            SELECT 1 FROM location_institutions li_perm
            WHERE li_perm.location_id = l.location_id
            AND li_perm.institution_id = ANY(:user_inst_ids)
        )"""
        inst_params = {"user_inst_ids": user_inst_ids}
    else:
        return jsonify([])

    conn = None
    try:
        conn = get_pam_db_connection()

        INACTIVE_MARKER_DAYS = 200
        DEVICE_REMOVED_PURPOSE_ID = 3
        TIME_WARNING_DAYS = 14
        TIME_CRITICAL_DAYS = 3
        DATA_RATE_MB_PER_HOUR = 290
        status_severity = {'ok': 0, 'warning': 1, 'critical': 2}

        # Додаткова фільтрація за вибраною установою з dropdown
        if selected_inst_id and selected_inst_id.isdigit():
            inst_condition += """ AND EXISTS (
                SELECT 1 FROM location_institutions li_sel
                WHERE li_sel.location_id = l.location_id
                AND li_sel.institution_id = :selected_inst_id
            )"""
            inst_params['selected_inst_id'] = int(selected_inst_id)

        last_data_query = text("SELECT location_id, MAX(datetime_start) as last_data_date FROM recordings GROUP BY location_id")
        last_data_results = conn.execute(last_data_query).fetchall()
        last_data_map = {row.location_id: row.last_data_date for row in last_data_results}

        locations = conn.execute(
            text(f"SELECT l.location_id, l.location_name, l.lat, l.lon FROM locations l WHERE {inst_condition}"),
            inst_params
        ).fetchall()
        response_data = []
        
        for loc in locations:
            last_visit_query = text("""
                SELECT 
                    sv.visit_datetime, sv.recording_hours_per_day, sv.visit_purpose_id,
                    bt.estimated_recording_hours, scs.capacity_gb
                FROM service_visits sv
                LEFT JOIN battery_types bt ON sv.battery_type_id = bt.id
                LEFT JOIN sd_card_status scs ON sv.sd_card_status_id = scs.id
                WHERE sv.location_id = :location_id
                ORDER BY sv.visit_datetime DESC LIMIT 1
            """)
            last_visit = conn.execute(last_visit_query, {'location_id': loc.location_id}).fetchone()

            status, status_reason = 'unknown', "Немає даних про обслуговування"
            days_since_visit, battery_days_left, sd_card_days_left = None, None, None

            if last_visit and last_visit.visit_purpose_id == DEVICE_REMOVED_PURPOSE_ID:
                status, status_reason = 'inactive', "Прилад демонтовано"
            else:
                last_activity_date = last_data_map.get(loc.location_id)
                if not last_activity_date and last_visit:
                    last_activity_date = last_visit.visit_datetime

                if not last_activity_date:
                    status, status_reason = 'inactive', "Немає ані даних, ані записів про обслуговування"
                else:
                    days_since_activity = (datetime.now(last_activity_date.tzinfo).date() - last_activity_date.date()).days
                    
                    if days_since_activity > INACTIVE_MARKER_DAYS:
                        status = 'inactive'
                        status_reason = f"Останні дані понад {INACTIVE_MARKER_DAYS} днів тому"
                    elif last_visit:
                        days_since_visit = (datetime.now(last_visit.visit_datetime.tzinfo).date() - last_visit.visit_datetime.date()).days
                        
                        if last_visit.recording_hours_per_day and last_visit.estimated_recording_hours:
                            predicted_battery_days = last_visit.estimated_recording_hours / last_visit.recording_hours_per_day
                            battery_days_left = predicted_battery_days - days_since_visit

                        card_capacity_gb = last_visit.capacity_gb
                        if not card_capacity_gb:
                            prev_card_query = text("""
                                SELECT scs.capacity_gb FROM service_visits sv
                                JOIN sd_card_status scs ON sv.sd_card_status_id = scs.id
                                WHERE sv.location_id = :location_id AND scs.capacity_gb IS NOT NULL
                                ORDER BY sv.visit_datetime DESC LIMIT 1
                            """)
                            prev_card = conn.execute(prev_card_query, {'location_id': loc.location_id}).fetchone()
                            if prev_card: 
                                # --- ВИПРАВЛЕННЯ 1 ТУТ ---
                                card_capacity_gb = float(prev_card.capacity_gb) if prev_card.capacity_gb else None

                        if card_capacity_gb and last_visit.recording_hours_per_day:
                            daily_usage_gb = (last_visit.recording_hours_per_day * DATA_RATE_MB_PER_HOUR) / 1024
                            # --- ВИПРАВЛЕННЯ 2 ТУТ ---
                            predicted_sd_card_days = float(card_capacity_gb) / daily_usage_gb if daily_usage_gb > 0 else float('inf')
                            sd_card_days_left = predicted_sd_card_days - days_since_visit
                        
                        battery_status = 'ok'
                        if battery_days_left is not None and battery_days_left <= TIME_CRITICAL_DAYS: battery_status = 'critical'
                        elif battery_days_left is not None and battery_days_left <= TIME_WARNING_DAYS: battery_status = 'warning'
                        
                        sd_card_status = 'ok'
                        if sd_card_days_left is not None and sd_card_days_left <= TIME_CRITICAL_DAYS: sd_card_status = 'critical'
                        elif sd_card_days_left is not None and sd_card_days_left <= TIME_WARNING_DAYS: sd_card_status = 'warning'
                        
                        if status_severity[sd_card_status] > status_severity[battery_status]:
                            status, status_reason = sd_card_status, "Лімітуючий фактор: SD-карта"
                        else:
                            status, status_reason = battery_status, "Лімітуючий фактор: Батарея"
                    else:
                        status, status_reason = 'unknown', 'Є свіжі дані, але немає записів обслуговування для прогнозу'

            response_data.append({
                'id': loc.location_id, 'name': loc.location_name, 'latitude': float(loc.lat), 'longitude': float(loc.lon),
                'status': status, 'last_visit_date': last_visit.visit_datetime.strftime('%d.%m.%Y') if last_visit else '---',
                'days_since_visit': days_since_visit, 'status_reason': status_reason,
                'battery_days_left': round(battery_days_left) if battery_days_left is not None else None,
                'sd_card_days_left': round(sd_card_days_left) if sd_card_days_left is not None else None
            })
            
        return jsonify(response_data)
    except Exception as e:
        current_app.logger.error(f"Error fetching PAM locations with status: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load location status data'}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/api/pam/location/<int:location_id>/service-history')
@login_required
def api_get_pam_service_history(lang_code, location_id):
    """API для отримання історії обслуговування для конкретної локації ПАМ."""
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        # Перевірка доступу: лише адмін або користувач, чия установа пов'язана з локацією
        if not current_user.has_role('admin'):
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'error': 'Доступ заборонено'}), 403
            has_access = conn.execute(text("""
                SELECT 1 FROM location_institutions
                WHERE location_id = :loc_id AND institution_id = ANY(:inst_ids)
            """), {'loc_id': location_id, 'inst_ids': user_inst_ids}).fetchone()
            if not has_access:
                return jsonify({'error': 'Доступ заборонено'}), 403
        # --- ОНОВЛЕНО: Додано JOIN з visit_purposes ---
        history_query = text(f"""
            SELECT
                sv.id, sv.visit_datetime, sv.is_operational, sv.comments, sv.user_id,
                sv.visit_purpose_id, sv.battery_type_id, sv.sd_card_status_id,
                sv.recording_hours_per_day,
                CASE WHEN '{lang_code}' = 'uk' THEN vp.name_ua ELSE vp.name_en END as purpose,
                CASE WHEN '{lang_code}' = 'uk' THEN bt.name_ua ELSE bt.name_en END as battery_info,
                CASE WHEN '{lang_code}' = 'uk' THEN scs.name_ua ELSE scs.name_en END as sd_card_info
            FROM service_visits sv
            JOIN visit_purposes vp ON sv.visit_purpose_id = vp.id
            LEFT JOIN battery_types bt ON sv.battery_type_id = bt.id
            JOIN sd_card_status scs ON sv.sd_card_status_id = scs.id
            WHERE sv.location_id = :location_id
            ORDER BY sv.visit_datetime DESC
            LIMIT 20
        """)
        
        visits = conn.execute(history_query, {'location_id': location_id}).fetchall()
        if not visits: return jsonify([])

        user_ids = [v.user_id for v in visits]
        users = User.query.filter(User.id.in_(user_ids)).all()
        user_map = {user.id: user.username for user in users}

        history_data = []
        for v in visits:
            history_data.append({
                'id': v.id,
                'visit_datetime': v.visit_datetime.strftime('%d.%m.%Y %H:%M'),
                'visit_datetime_raw': v.visit_datetime.strftime('%Y-%m-%dT%H:%M'),
                'user': user_map.get(v.user_id, f"User ID: {v.user_id}"),
                'is_own': v.user_id == current_user.id,
                'purpose': v.purpose,
                'visit_purpose_id': v.visit_purpose_id,
                'battery_type_id': v.battery_type_id,
                'sd_card_status_id': v.sd_card_status_id,
                'recording_hours_per_day': v.recording_hours_per_day,
                'is_operational': v.is_operational,
                'battery_info': v.battery_info or 'Не замінювались',
                'sd_card_info': v.sd_card_info,
                'comments': v.comments
            })
        
        return jsonify(history_data)
    except Exception as e:
        current_app.logger.error(f"Error fetching PAM service history for location {location_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load history'}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/api/pam/service-log/create', methods=['POST'])
@login_required
@role_required('manager')
def api_create_pam_service_visit(lang_code):
    """API для створення нового запису в журналі обслуговування ПАМ."""
    g.lang_code = lang_code
    conn = None
    try:
        data = request.json

        location_id = data.get('location_id')
        sd_card_status_id = data.get('sd_card_status_id')
        visit_datetime_str = data.get('visit_datetime')
        recording_hours_per_day = data.get('recording_hours_per_day')
        visit_purpose_id = data.get('visit_purpose_id')

        if not all([location_id, sd_card_status_id, visit_datetime_str, recording_hours_per_day, visit_purpose_id]):
            return jsonify({'success': False, 'error': 'Не всі обов\'язкові поля заповнені.'}), 400

        is_operational_str = data.get('is_camera_operational')
        is_camera_operational = True if is_operational_str == 'true' else False if is_operational_str == 'false' else None

        params = {
            "location_id": int(location_id), "user_id": current_user.id,
            "visit_datetime": datetime.fromisoformat(visit_datetime_str),
            "is_operational": is_camera_operational,
            "battery_type_id": int(data['battery_type_id']) if data.get('battery_type_id') else None,
            "sd_card_status_id": int(sd_card_status_id),
            "recording_hours_per_day": int(recording_hours_per_day),
            "visit_purpose_id": int(visit_purpose_id),
            "comments": data.get('comments', '').strip() or None
        }

        conn = get_pam_db_connection()
        # Перевірка доступу: лише адмін або користувач, чия установа пов'язана з локацією
        if not current_user.has_role('admin'):
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'success': False, 'error': 'Доступ заборонено'}), 403
            has_access = conn.execute(text("""
                SELECT 1 FROM location_institutions
                WHERE location_id = :loc_id AND institution_id = ANY(:inst_ids)
            """), {'loc_id': int(location_id), 'inst_ids': user_inst_ids}).fetchone()
            if not has_access:
                return jsonify({'success': False, 'error': 'Доступ заборонено'}), 403
        insert_query = text("""
            INSERT INTO service_visits (location_id, user_id, visit_datetime, is_operational, 
                                        battery_type_id, sd_card_status_id, recording_hours_per_day, 
                                        visit_purpose_id, comments)
            VALUES (:location_id, :user_id, :visit_datetime, :is_operational, 
                    :battery_type_id, :sd_card_status_id, :recording_hours_per_day, 
                    :visit_purpose_id, :comments)
        """)
        conn.execute(insert_query, params)
        conn.commit()
        
        current_app.logger.info(f"User {current_user.username} created new PAM service visit for location {location_id}")
        return jsonify({'success': True, 'message': 'Запис успішно додано до журналу!'}), 201
    except (ValueError, TypeError) as e:
        if conn: conn.rollback()
        current_app.logger.warning(f"Invalid data for PAM service visit creation: {e}")
        return jsonify({'success': False, 'error': 'Передано некоректні дані.'}), 400
    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"Error creating PAM service visit: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Помилка сервера при збереженні запису.'}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/api/pam/service-visit/<int:visit_id>/update', methods=['POST'])
@login_required
@role_required('manager')
def api_update_pam_service_visit(lang_code, visit_id):
    """API для редагування існуючого запису в журналі обслуговування ПАМ."""
    g.lang_code = lang_code
    conn = None
    try:
        data = request.json
        visit_datetime_str = data.get('visit_datetime')
        sd_card_status_id  = data.get('sd_card_status_id')
        recording_hours    = data.get('recording_hours_per_day')
        visit_purpose_id   = data.get('visit_purpose_id')

        if not all([visit_datetime_str, sd_card_status_id, recording_hours, visit_purpose_id]):
            return jsonify({'success': False, 'error': 'Не всі обов\'язкові поля заповнені.'}), 400

        is_operational_str  = data.get('is_camera_operational')
        is_camera_operational = True if is_operational_str == 'true' else False if is_operational_str == 'false' else None

        conn = get_pam_db_connection()

        # Перевірка: запис існує і користувач має доступ до локації
        visit_row = conn.execute(
            text("SELECT location_id FROM service_visits WHERE id = :id"),
            {'id': visit_id}
        ).fetchone()
        if not visit_row:
            return jsonify({'success': False, 'error': 'Запис не знайдено.'}), 404

        if not current_user.has_role('admin'):
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'success': False, 'error': 'Доступ заборонено'}), 403
            has_access = conn.execute(text("""
                SELECT 1 FROM location_institutions
                WHERE location_id = :loc_id AND institution_id = ANY(:inst_ids)
            """), {'loc_id': visit_row.location_id, 'inst_ids': user_inst_ids}).fetchone()
            if not has_access:
                return jsonify({'success': False, 'error': 'Доступ заборонено'}), 403

        params = {
            'id': visit_id,
            'visit_datetime':        datetime.fromisoformat(visit_datetime_str),
            'visit_purpose_id':      int(visit_purpose_id),
            'battery_type_id':       int(data['battery_type_id']) if data.get('battery_type_id') else None,
            'sd_card_status_id':     int(sd_card_status_id),
            'recording_hours_per_day': int(recording_hours),
            'is_operational':        is_camera_operational,
            'comments':              data.get('comments', '').strip() or None,
        }
        conn.execute(text("""
            UPDATE service_visits
            SET visit_datetime          = :visit_datetime,
                visit_purpose_id        = :visit_purpose_id,
                battery_type_id         = :battery_type_id,
                sd_card_status_id       = :sd_card_status_id,
                recording_hours_per_day = :recording_hours_per_day,
                is_operational          = :is_operational,
                comments                = :comments
            WHERE id = :id
        """), params)
        conn.commit()

        current_app.logger.info(f"User {current_user.username} updated PAM service visit {visit_id}")
        return jsonify({'success': True, 'message': 'Запис оновлено.'})
    except (ValueError, TypeError) as e:
        if conn: conn.rollback()
        current_app.logger.warning(f"Invalid data for PAM service visit update: {e}")
        return jsonify({'success': False, 'error': 'Некоректні дані.'}), 400
    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"Error updating PAM service visit {visit_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Помилка сервера.'}), 500
    finally:
        if conn: conn.close()

@pam_bp.route('/<lang_code>/api/pam/get-weather-overlay')
def api_get_weather_overlay(lang_code):
    """
    API для отримання погодних даних.
    Координати розраховуються динамічно:
    1. Центр фактичних детекцій (якщо є результати фільтрації).
    2. Центр вибраних локацій (якщо детекцій немає).
    3. Дефолтний центр (за замовчуванням).
    """
    from .utils import get_weather_data, get_pam_db_connection
    
    try:
        # 1. Збір параметрів
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        species_name = request.args.get('species')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        
        try:
            confidence = float(request.args.get('confidence', 0.95))
        except (ValueError, TypeError):
            confidence = 0.95

        if not start_date or not end_date:
            return jsonify({'error': 'Dates required'}), 400
            
        # 2. Дефолтні координати (оновлені)
        target_lat = 49.94
        target_lon = 23.70
        
        conn = None
        try:
            conn = get_pam_db_connection()

            user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
            is_admin = current_user.is_authenticated and current_user.has_role('admin')
            inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, selected_inst_id=institution_id)
            params.update(inst_params)

            # 3. Спроба 1: Знайти центр фактичних детекцій
            conditions = [
                "s.scientific_name = :species",
                "d.confidence >= :confidence",
                "DATE(r.datetime_start) BETWEEN :start_date AND :end_date",
                inst_condition
            ]
            
            params = {
                'species': species_name,
                'confidence': confidence,
                'start_date': start_date,
                'end_date': end_date
            }
            
            joins = """
                JOIN recordings r ON d.recording_id = r.recording_id
                JOIN locations l ON r.location_id = l.location_id
                JOIN species s ON d.species_id = s.species_id
            """
            
            if location_ids_str:
                loc_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()]
                if loc_ids:
                    conditions.append("l.location_id = ANY(:loc_ids)")
                    params['loc_ids'] = loc_ids
            
            if biotope_ids_str:
                bio_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()]
                if bio_ids:
                    joins += " JOIN location_biotopes lb ON l.location_id = lb.location_id"
                    conditions.append("lb.biotope_id = ANY(:bio_ids)")
                    params['bio_ids'] = bio_ids
            
            where_clause = " AND ".join(conditions)
            
            centroid_sql = text(f"""
                SELECT AVG(l.lat), AVG(l.lon)
                FROM detections d
                {joins}
                WHERE {where_clause}
            """)
            
            res = conn.execute(centroid_sql, params).fetchone()
            
            if res and res[0] is not None and res[1] is not None:
                target_lat = float(res[0])
                target_lon = float(res[1])
                current_app.logger.info(f"Weather: Using centroid of DETECTIONS: {target_lat}, {target_lon}")
            
            # 4. Спроба 2: Якщо детекцій немає, беремо центр вибраних локацій
            elif location_ids_str:
                # Повторно парсимо ID, бо вони могли бути використані вище
                loc_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()]
                if loc_ids:
                    res_loc = conn.execute(text(
                        "SELECT AVG(lat), AVG(lon) FROM locations WHERE location_id = ANY(:ids)"
                    ), {'ids': loc_ids}).fetchone()
                    
                    if res_loc and res_loc[0]:
                        target_lat = float(res_loc[0])
                        target_lon = float(res_loc[1])
                        current_app.logger.info(f"Weather: Using centroid of SELECTED LOCATIONS: {target_lat}, {target_lon}")
            else:
                current_app.logger.info(f"Weather: Using DEFAULT coordinates: {target_lat}, {target_lon}")

        except Exception as db_err:
            current_app.logger.error(f"Error calculating weather centroid: {db_err}")
        finally:
            if conn: conn.close()
        
        # 5. Отримання даних (з бази або API)
        weather_data = get_weather_data(start_date, end_date, target_lat, target_lon)
        
        # 6. Форматування результату
        result = {
            'dates': [],
            'temp_mean': [],
            'temp_min': [],
            'temp_max': [],
            'precipitation': [],
            'wind_speed_max': [],
            'wind_speed_mean': [], # Додано нове поле
            'humidity': [],
            'pressure': [],
            'debug_coords': {'lat': target_lat, 'lon': target_lon}
        }
        
        for row in weather_data:
            result['dates'].append(row['date'])
            
            # Температура
            result['temp_mean'].append(row.get('temperature_2m_mean'))
            result['temp_min'].append(row.get('temperature_2m_min'))
            result['temp_max'].append(row.get('temperature_2m_max'))
            
            # Опади
            result['precipitation'].append(row.get('precipitation_sum', 0))
            
            # Вітер (розділяємо макс і середній)
            result['wind_speed_max'].append(row.get('wind_speed_10m_max'))
            result['wind_speed_mean'].append(row.get('wind_speed_10m_mean')) # <-- Виправлено: беремо з правильного ключа
            
            # Інше
            result['humidity'].append(row.get('relative_humidity_2m_mean'))
            result['pressure'].append(row.get('surface_pressure_mean'))
            
        return jsonify(result)
        
    except Exception as e:
        current_app.logger.error(f"API Weather Error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    
@pam_bp.route('/<lang_code>/api/pam/export-detailed-data')
@login_required
@role_required('pam_verifier')
def export_detailed_data(lang_code):
    """
    Експорт даних дашборду в один Excel-файл з 4 листами.
    Доступно тільки для адмінів та модераторів.
    """
    try:
        # 1. Збір параметрів (ідентично до графіків)
        species_name = request.args.get('species')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        institution_id = request.args.get('institution_id', '')
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()] or None
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()] or None
        
        try:
            confidence = float(request.args.get('confidence', 0.75))
            min_detections = int(request.args.get('min_detections', 1))
        except (ValueError, TypeError):
            confidence = 0.75
            min_detections = 1

        if not species_name:
            return jsonify({'error': 'Species name is required'}), 400

        # --- ОТРИМАННЯ ДАНИХ ---
        from .utils import (
            get_unique_detection_points, 
            get_daily_detection_counts, 
            get_weather_data,
            get_time_scatter_data,
            get_filtered_detections,
            get_pam_db_connection
        )

        # Лист 1: Карта (Map Locations)
        map_points = get_unique_detection_points(
            lang_code=lang_code, species_name=species_name, start_date=start_date, end_date=end_date,
            confidence=confidence, location_ids=location_ids, biotope_ids=biotope_ids, min_detections=min_detections,
            institution_id=institution_id
        )
        
        # Лист 2: Динаміка + Погода (Daily Dynamics & Weather)
        daily_data = get_daily_detection_counts(
            species_name=species_name, start_date=start_date, end_date=end_date,
            confidence=confidence, location_ids=location_ids, biotope_ids=biotope_ids, excel_exp=True,
            institution_id=institution_id
        )
        
        # Для погоди нам потрібні координати. Використовуємо логіку з api_get_weather_overlay
        # Спрощено беремо центр або дефолт
        target_lat, target_lon = 49.94, 23.70
        if map_points:
            lats = [p['lat'] for p in map_points]
            lons = [p['lon'] for p in map_points]
            target_lat = sum(lats) / len(lats)
            target_lon = sum(lons) / len(lons)
            
        weather_data = get_weather_data(start_date, end_date, target_lat, target_lon)

        # Лист 3: Добова активність + Сонце (Time Activity)
        time_scatter = get_time_scatter_data(
            species_name=species_name, start_date=start_date, end_date=end_date,
            confidence=confidence, location_ids=location_ids, biotope_ids=biotope_ids, excel_exp=True,
            institution_id=institution_id
        )

        # Лист 4: Сирі дані (All Raw Detections)
        # Використовуємо get_filtered_detections, але нам треба більше деталей (локація)
        # Тому зробимо кастомний запит або розширимо існуючий, 
        # але для швидкості використаємо get_occurrence_data (якщо він підходить) або сформуємо тут.
        # Найкраще використати те, що є в utils, але get_filtered_detections повертає тільки дату і conf.
        # Тому використаємо get_time_scatter_data, бо там вже є verification_result і час.
        # Але для "повного" списку краще використати api_data_preview логіку або сформувати з time_scatter.
        # Давайте візьмемо time_scatter, оскільки він містить всі точки, які показані на графіку.
        
        # --- ФОРМУВАННЯ DATAFRAMES ---

        # 1. MAP DATAFRAME
        df_map = pd.DataFrame(map_points)
        if not df_map.empty:
            df_map['verification_status'] = df_map['is_verified'].apply(lambda x: 'Verified' if x else 'Unverified')
            # Перейменування та відбір колонок
            df_map = df_map[['location_id', 'location_name', 'lat', 'lon', 'detection_count', 'verification_status']]
        
        # 2. DAILY + WEATHER DATAFRAME
        df_daily = pd.DataFrame({'date': daily_data['dates'], 'detections_count': daily_data['counts']})
        df_weather = pd.DataFrame(weather_data)
        
        if not df_daily.empty and not df_weather.empty:
            # Мерджимо по даті
            df_combined = pd.merge(df_daily, df_weather, on='date', how='left')
        else:
            df_combined = df_daily

        # 3. TIME ACTIVITY + SUN
        detections_list = time_scatter.get('detections', [])
        sun_times_list = time_scatter.get('sun_times', [])
        
        df_time = pd.DataFrame(detections_list)
        df_sun = pd.DataFrame(sun_times_list)
        
        if not df_time.empty:
            # Маппінг статусів верифікації
            def get_status_label(val):
                if val == 1: return 'Confirmed'
                if val == 0: return 'Rejected'
                return 'Automatic'
            
            df_time['verification_status'] = df_time['verification_result'].apply(get_status_label)
            
            # Додаємо дані про сонце до кожного запису детекції (join по даті)
            if not df_sun.empty:
                df_time = pd.merge(df_time, df_sun[['date', 'sunrise', 'sunset']], on='date', how='left')
            
            # Вибір колонок
            df_time = df_time[['date', 'time', 'confidence', 'verification_status', 'sunrise', 'sunset']]

        # --- ЗАПИС В EXCEL ---
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Sheet 1
            df_map.to_excel(writer, sheet_name='Map_Locations', index=False)
            
            # Sheet 2
            df_combined.to_excel(writer, sheet_name='Daily_Dynamics_Weather', index=False)
            
            # Sheet 3
            df_time.to_excel(writer, sheet_name='Raw_Data', index=False)
            
            
        output.seek(0)
        
        filename = f"PAM_Export_{species_name}_{start_date}_{end_date}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        current_app.logger.error(f"Export Error: {e}", exc_info=True)
        return jsonify({'error': 'Server error during export'}), 500

@pam_bp.route('/<lang_code>/pam/evaluation/species/<int:species_id>')
@login_required
def species_evaluation_detail(lang_code, species_id):
    """Сторінка детальної оцінки та графіка регресії для виду."""
    g.lang_code = lang_code
    
    data = get_species_logistic_data(species_id)
    
    if not data:
        flash('Дані для цього виду відсутні або ще не розраховані.', 'warning')
        return redirect(url_for('pam.evaluation_results', lang_code=lang_code))
    
    # Формуємо красиву назву для заголовка
    info = data['info']
    display_name = info['scientific_name']
    if lang_code == 'uk' and info['common_name_uk']:
        display_name = f"{info['common_name_uk']} ({info['scientific_name']})"
    elif lang_code == 'en' and info['common_name_en']:
        display_name = f"{info['common_name_en']} ({info['scientific_name']})"
        
    return render_template(
        'pam_evaluation_species_detail.html', 
        data=data,
        display_name=display_name,
        species_id=species_id
    )

@pam_bp.route('/<lang_code>/pam/evaluation/export-species/<int:species_id>')
@login_required
@role_required('pam_verifier')
def download_species_evaluation_excel(lang_code, species_id):
    """Експорт даних регресії в Excel."""
    try:
        data = get_species_logistic_data(species_id)
        if not data:
            return "No data", 404

        # 1. Лист з точками
        df_points = pd.DataFrame(data['points'])
        df_points = df_points.rename(columns={
            'confidence': 'Confidence Score (X)',
            'outcome': 'Binary Outcome (Y)',
            'avg_verification': 'Raw Consensus Score'
        })

        # 2. Лист з метриками
        # Фільтруємо словник info, щоб залишити тільки параметри
        info = data['info']
        metrics = {k: v for k, v in info.items() if k not in ['scientific_name', 'common_name_uk', 'common_name_en']}
        df_params = pd.DataFrame([metrics])
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df_points.to_excel(writer, sheet_name='Data Points', index=False)
            df_params.to_excel(writer, sheet_name='Model Parameters', index=False)
            
            # Додаємо формулу
            ws = writer.sheets['Model Parameters']
            ws.cell(row=5, column=1, value="Formula: P(x) = 1 / (1 + exp(-(beta0 + beta1 * x)))")

        output.seek(0)
        filename = f"Regression_{info['scientific_name']}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        current_app.logger.error(f"Export error: {e}")
        return "Export failed", 500


# ---------------------------------------------------------------------------
# PAM IMPORT
# ---------------------------------------------------------------------------

@pam_bp.route('/<lang_code>/pam/import')
@login_required
@role_required('manager')
def pam_import(lang_code):
    """Import page: select location, classifier, upload BirdNET CSV files."""
    g.lang_code = lang_code
    conn = None
    try:
        conn = get_pam_db_connection()
        is_admin = current_user.has_role('admin')

        if is_admin:
            all_inst_objects = Institution.query.order_by(Institution.name_uk).all()
        else:
            all_inst_objects = current_user.institutions

        if lang_code == 'uk':
            inst_names_map = {i.id: i.name_uk for i in all_inst_objects}
        else:
            inst_names_map = {i.id: (i.name_en or i.name_uk) for i in all_inst_objects}

        institutions = [{'id': i.id, 'name': inst_names_map[i.id]} for i in all_inst_objects]

        raw_rows = conn.execute(text("""
            SELECT l.location_id, l.location_name, l.location_name_en, l.lat, l.lon, li.institution_id
            FROM locations l
            LEFT JOIN location_institutions li ON l.location_id = li.location_id
            ORDER BY l.location_name
        """)).fetchall()

        locations_dict = {}
        for row in raw_rows:
            lid = row.location_id
            if lid not in locations_dict:
                loc_name = row.location_name
                if lang_code == 'en' and row.location_name_en:
                    loc_name = row.location_name_en
                locations_dict[lid] = {
                    'location_id': lid,
                    'name': loc_name,
                    'latitude': float(row.lat),
                    'longitude': float(row.lon),
                    'inst_ids': [],
                }
            if row.institution_id:
                locations_dict[lid]['inst_ids'].append(row.institution_id)

        user_inst_ids = [i.id for i in current_user.institutions]
        if is_admin:
            final_locations = list(locations_dict.values())
        else:
            final_locations = [
                loc for loc in locations_dict.values()
                if any(i in user_inst_ids for i in loc['inst_ids'])
            ]

        importers_list = [{'key': imp.key, 'name': imp.name} for imp in IMPORTERS.values()]

        biotopes_result = conn.execute(text(
            "SELECT id, name_ua, name_en FROM biotopes ORDER BY name_ua"
        )).fetchall()
        biotopes = [dict(row._mapping) for row in biotopes_result]

        return render_template(
            'pam_import.html',
            institutions=institutions,
            locations_json_string=json.dumps(final_locations),
            importers=importers_list,
            biotopes=biotopes,
            geoserver_url=current_app.config.get('GEOSERVER_URL', ''),
        )
    except Exception as e:
        current_app.logger.error(f"PAM import page error: {e}", exc_info=True)
        flash('Помилка завантаження сторінки імпорту.', 'danger')
        return redirect(url_for('pam.pam_home', lang_code=lang_code))
    finally:
        if conn:
            conn.close()


@pam_bp.route('/<lang_code>/api/pam/import', methods=['POST'])
@login_required
@role_required('manager')
def api_pam_import(lang_code):
    """
    Process a batch of uploaded BirdNET CSV files for a given location.

    Form fields:
        location_id  – integer
        classifier   – importer key (default: 'birdnet')
        files        – one or more CSV file uploads
    """
    g.lang_code = lang_code
    try:
        location_id = request.form.get('location_id', type=int)
        if not location_id:
            return jsonify({'success': False, 'error': 'location_id is required'}), 400

        classifier = request.form.get('classifier', 'birdnet')
        importer = IMPORTERS.get(classifier)
        if not importer:
            return jsonify({'success': False, 'error': f'Unknown classifier: {classifier}'}), 400

        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'No files uploaded'}), 400

        # Verify user has access to this location
        is_admin = current_user.has_role('admin')
        if not is_admin:
            conn = get_pam_db_connection()
            try:
                user_inst_ids = [i.id for i in current_user.institutions]
                row = conn.execute(text("""
                    SELECT 1 FROM location_institutions
                    WHERE location_id = :loc AND institution_id = ANY(:insts)
                    LIMIT 1
                """), {'loc': location_id, 'insts': user_inst_ids}).fetchone()
                if not row:
                    return jsonify({'success': False, 'error': 'Access denied to this location'}), 403
            finally:
                conn.close()

        engine = get_pam_engine()
        processor = PAMImportProcessor(engine, location_id, importer)
        stats = processor.process_batch(files)

        current_app.logger.info(
            f"PAM import: user={current_user.id}, location={location_id}, "
            f"classifier={classifier}, stats={stats}"
        )
        return jsonify({'success': True, 'stats': stats})

    except Exception as e:
        current_app.logger.error(f"PAM import API error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500






