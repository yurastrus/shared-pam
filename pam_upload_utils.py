# SPDX-License-Identifier: AGPL-3.0-only
import os
import re
import zipfile
import shutil
import traceback
from datetime import datetime, date, time
from flask import current_app
from werkzeug.utils import secure_filename
from sqlalchemy import text
from .utils import get_pam_db_connection

def parse_audio_filename(filename):
    """
    Parse an audio filename and extract its components.

    Filename formats:
    - 0.118_STAVY_20250303_184602_sec216_part1.wav  (new format)
    - 0.804_K1_20241110_074902.wav                  (old format)
    - 0.120_ROZTOCH-POND_20230911_010102_sec270_part1.wav  (new format with hyphen)

    Returns a dict with keys:
    - confidence: float (0.118, 0.804)
    - location: str (STAVY, K1, ROZTOCH-POND)
    - date: datetime.date
    - time: datetime.time
    - original_filename: str
    """
    try:
        # Extended regex for audio filename parsing.
        # Captures: confidence_location_date_time and ignores everything after (sec216_part1, etc.).
        pattern = r'^(\d+\.\d+)_([A-Za-z0-9\-]+)_(\d{8})_(\d{6})(?:_.*)?\.(wav|flac)$'
        match = re.match(pattern, filename, re.IGNORECASE)
        
        if not match:
            raise ValueError(f"Неправильний формат імені файлу: {filename}")
        
        confidence_str, location, date_str, time_str, _ = match.groups()
        
        # Parse component values.
        confidence = float(confidence_str)
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"Точність повинна бути між 0 і 1, отримано: {confidence}")
        
        # Parse date: 20250303 → 2025-03-03.
        parsed_date = datetime.strptime(date_str, '%Y%m%d').date()
        
        # Parse time: 184602 → 18:46:02.
        parsed_time = datetime.strptime(time_str, '%H%M%S').time()
        
        return {
            'confidence': confidence,
            'location': location,
            'date': parsed_date,
            'time': parsed_time,
            'original_filename': filename
        }
        
    except Exception as e:
        current_app.logger.error(f"Error parsing file {filename}: {e}")
        raise ValueError(f"Не вдалося розпарсити файл {filename}: {str(e)}")

def validate_species_folder(species_name, conn):
    """Check whether the species exists in the species table and return its ID."""
    try:
        result = conn.execute(
            text("SELECT species_id FROM species WHERE scientific_name = :species_name"),
            {"species_name": species_name}
        ).fetchone()
        
        if result:
            return result[0]  # species_id
        else:
            # Log the unknown species.
            current_app.logger.warning(f"Unknown species: {species_name}")
            return None
            
    except Exception as e:
        current_app.logger.error(f"Error checking species {species_name}: {e}")
        raise

def check_duplicate_segment(filename, conn):
    """Check whether a segment with this filename already exists."""
    try:
        result = conn.execute(
            text("SELECT id FROM segments WHERE filename = :filename"),
            {"filename": filename}
        ).fetchone()
        
        return result is not None
        
    except Exception as e:
        current_app.logger.error(f"Error checking duplicate {filename}: {e}")
        raise

def save_segment_to_db(parsed_data, species_id, file_path, conn):
    """Save a segment to the database."""
    try:
        conn.execute(text("""
            INSERT INTO segments 
            (species_id, filename, confidence_level, location_name, 
             recorded_date, recorded_time, file_path, upload_date, status)
            VALUES (:species_id, :filename, :confidence_level, :location_name, 
                    :recorded_date, :recorded_time, :file_path, :upload_date, :status)
        """), {
            "species_id": species_id,
            "filename": parsed_data['original_filename'],
            "confidence_level": parsed_data['confidence'],
            "location_name": parsed_data['location'],
            "recorded_date": parsed_data['date'],
            "recorded_time": parsed_data['time'],
            "file_path": file_path,
            "upload_date": datetime.now(),
            "status": 'pending'
        })
        
        current_app.logger.info(f"Segment saved: {parsed_data['original_filename']}")
        
    except Exception as e:
        current_app.logger.error(f"Error saving segment {parsed_data['original_filename']}: {e}")
        raise

def ensure_system_user(conn):
    """
    Ensure the system user (ID 0) exists for locally-verified files.
    Creates the user if absent.

    Returns:
        int: System user ID (always 0).
    """
    try:
        # Check whether the user with ID 0 exists.
        # Use the main database for the users table.
        from flask import current_app
        from sqlalchemy import create_engine, text as sql_text
        
        # Open a connection to the main database.
        main_engine = create_engine(current_app.config['SQLALCHEMY_DATABASE_URI'])
        main_conn = main_engine.connect()
        
        try:
            # Check whether the user exists.
            result = main_conn.execute(sql_text("""
                SELECT id FROM users WHERE id = 0
            """)).fetchone()
            
            if not result:
                # Create the system user.
                current_app.logger.info("Creating system user with ID 0 for local verifications")
                main_conn.execute(sql_text("""
                    INSERT INTO users (id, username, email, password_hash, active, created_at)
                    VALUES (0, 'system_local_verification', 'system@local.verification', 'no_password', false, CURRENT_TIMESTAMP)
                    ON CONFLICT (id) DO NOTHING
                """))
                main_conn.commit()
                current_app.logger.info("System user created successfully")
        
        finally:
            main_conn.close()
            
        return 0
        
    except Exception as e:
        current_app.logger.error(f"Error ensuring system user: {e}")
        return 0  # Return 0 even on error.

def save_segment_with_verification(parsed_data, species_id, file_path, conn, verification_result=None, system_user_id=0):
    """
    Save a segment to the database together with its verification result (if any).

    Args:
        parsed_data: Parsed file data.
        species_id: Species ID.
        file_path: Path to the audio file.
        conn: PAM database connection.
        verification_result: None (unverified), 0 (negative), 1 (positive).
        system_user_id: User ID for the verification (0 for the system user).
    """
    try:
        # First save the segment.
        conn.execute(text("""
            INSERT INTO segments 
            (species_id, filename, confidence_level, location_name, 
             recorded_date, recorded_time, file_path, upload_date, status)
            VALUES (:species_id, :filename, :confidence_level, :location_name, 
                    :recorded_date, :recorded_time, :file_path, :upload_date, :status)
        """), {
            "species_id": species_id,
            "filename": parsed_data['original_filename'],
            "confidence_level": parsed_data['confidence'],
            "location_name": parsed_data['location'],
            "recorded_date": parsed_data['date'],
            "recorded_time": parsed_data['time'],
            "file_path": file_path,
            "upload_date": datetime.now(),
            "status": 'completed' if verification_result is not None else 'pending'
        })
        
        # Save the verification result if one is provided.
        if verification_result is not None:
            # Get the ID of the newly created segment.
            segment_result = conn.execute(text("""
                SELECT id FROM segments 
                WHERE filename = :filename AND species_id = :species_id 
                ORDER BY upload_date DESC LIMIT 1
            """), {
                "filename": parsed_data['original_filename'],
                "species_id": species_id
            }).fetchone()
            
            if segment_result:
                segment_id = segment_result[0]
                
                # Save the verification.
                conn.execute(text("""
                    INSERT INTO segment_verifications 
                    (segment_id, user_id, verification_result, verified_at)
                    VALUES (:segment_id, :user_id, :verification_result, CURRENT_TIMESTAMP)
                """), {
                    "segment_id": segment_id,
                    "user_id": system_user_id,
                    "verification_result": verification_result
                })
                
                current_app.logger.info(f"Saved segment with verification: {parsed_data['original_filename']} -> {verification_result}")
            else:
                current_app.logger.error(f"Could not find the created segment: {parsed_data['original_filename']}")
        else:
            current_app.logger.info(f"Saved segment without verification: {parsed_data['original_filename']}")
        
    except Exception as e:
        current_app.logger.error(f"Error saving segment with verification {parsed_data['original_filename']}: {e}")
        raise

def process_species_folder(species_folder_path, species_name, species_id, upload_directory, system_user_id, stats):
    """
    Process a single species folder, handling optional Positive/Negative subfolders.
    Each file is processed with its own connection (no connection argument accepted).
    """
    processed_any = False
    
    try:
        current_app.logger.info(f"Processing species folder: {species_name}")
        
        items_in_folder = os.listdir(species_folder_path)
        has_positive = 'Positive' in items_in_folder
        has_negative = 'Negative' in items_in_folder
        
        # Process positive verifications.
        if has_positive:
            positive_path = os.path.join(species_folder_path, 'Positive')
            if os.path.isdir(positive_path):
                processed_count = process_audio_files_in_folder(
                    positive_path, species_name, species_id, upload_directory, 
                    system_user_id, stats, verification_result=1
                )
                if processed_count > 0: processed_any = True

        # Process negative verifications.
        if has_negative:
            negative_path = os.path.join(species_folder_path, 'Negative')
            if os.path.isdir(negative_path):
                processed_count = process_audio_files_in_folder(
                    negative_path, species_name, species_id, upload_directory,
                    system_user_id, stats, verification_result=0
                )
                if processed_count > 0: processed_any = True
        
        # Process unverified files in the root of the species folder.
        processed_count = process_audio_files_in_folder(
            species_folder_path, species_name, species_id, upload_directory,
            system_user_id, stats, verification_result=None, 
            exclude_dirs=['Positive', 'Negative']
        )
        if processed_count > 0: processed_any = True
        
        return processed_any
        
    except Exception as e:
        current_app.logger.error(f"Critical error processing species folder {species_name}: {e}")
        return False

def process_audio_files_in_folder(folder_path, species_name, species_id, upload_directory, 
                                system_user_id, stats, verification_result=None, exclude_dirs=None):
    """
    Process audio files in a specific folder using atomic transactions,
    collecting per-file error details.
    """
    if exclude_dirs is None:
        exclude_dirs = []
        
    processed_count = 0
    items = os.listdir(folder_path)
    
    for item in items:
        item_path = os.path.join(folder_path, item)
        
        if os.path.isdir(item_path) or not (item.lower().endswith(('.wav', '.flac'))):
            continue
            
        stats['total_files'] += 1
        
        conn = None
        try:
            conn = get_pam_db_connection()
            is_duplicate = False
            
            with conn.begin():
                if check_duplicate_segment(item, conn):
                    is_duplicate = True
                else:
                    parsed_data = parse_audio_filename(item)
                    if not parsed_data:
                        raise ValueError("Помилка парсингу імені файлу.")

                    final_path = os.path.join(upload_directory, species_name, item)
                    os.makedirs(os.path.dirname(final_path), exist_ok=True)
                    
                    shutil.copy2(item_path, final_path)
                    
                    save_segment_with_verification(
                        parsed_data, species_id, final_path, conn, 
                        verification_result, system_user_id
                    )
            
            if is_duplicate:
                stats['skipped_duplicates'] += 1
                current_app.logger.info(f"Skipping duplicate: {item}")
            else:
                stats['processed_files'] += 1
                processed_count += 1
                stats['processed_species'].add(species_name)
                
                if verification_result == 1:
                    stats['positive_verifications'] += 1
                elif verification_result == 0:
                    stats['negative_verifications'] += 1
            
        except Exception as e:
            stats['parse_errors'] += 1
            error_details = {
                'species': species_name,
                'filename': item,
                'error': str(e)
            }
            stats['error_files'].append(error_details)
            current_app.logger.error(f"Error processing file {item} for species {species_name}: {e}")
            continue
        finally:
            if conn:
                conn.close()
            
    return processed_count

def process_zip_archive(zip_file, upload_directory):
    """
    Main function for processing a ZIP archive of audio segments.

    Args:
        zip_file: werkzeug FileStorage object.
        upload_directory: Directory for saving files.

    Returns:
        dict: Processing statistics, including a list of files with errors.
    """
    conn = None
    temp_extract_dir = None
    
    try:
        current_app.logger.info(f"Starting ZIP processing. Upload directory: {upload_directory}")
        
        conn = get_pam_db_connection()
        current_app.logger.info("PAM database connection established")
        
        system_user_id = ensure_system_user(conn)
        
        temp_extract_dir = os.path.join(upload_directory, f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(temp_extract_dir, exist_ok=True)
        
        zip_filename = secure_filename(zip_file.filename)
        zip_path = os.path.join(temp_extract_dir, zip_filename)
        
        try:
            zip_file.seek(0)
            with open(zip_path, 'wb') as f:
                shutil.copyfileobj(zip_file, f)
            with zipfile.ZipFile(zip_path, 'r') as test_zip:
                if test_zip.testzip() is not None:
                    raise ValueError(f"Пошкоджений файл в архіві: {test_zip.testzip()}")
        except Exception as e:
            current_app.logger.error(f"Failed to save or validate ZIP file: {e}")
            raise ValueError(f"Не вдалося зберегти або перевірити ZIP файл: {str(e)}")

        # Processing statistics.
        stats = {
            'total_files': 0,
            'processed_files': 0,
            'skipped_duplicates': 0,
            'unknown_species': 0,
            'parse_errors': 0,
            'processed_species': set(),
            'positive_verifications': 0,
            'negative_verifications': 0,
            'unverified_files': 0,
            'error_files': []  # Error list initialisation.
        }
        
        extract_target_dir = os.path.join(temp_extract_dir, 'extracted')
        MAX_TOTAL_EXTRACTED = 5 * 1024 * 1024 * 1024  # 5GB — comfortable cap vs 1GB compressed limit

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            total_size = sum(info.file_size for info in zip_ref.infolist())
            if total_size > MAX_TOTAL_EXTRACTED:
                raise ValueError(
                    f"ZIP refuses extraction: uncompressed size {total_size} bytes > "
                    f"limit {MAX_TOTAL_EXTRACTED} bytes (potential zip bomb)"
                )
            zip_ref.extractall(extract_target_dir)
        current_app.logger.info("ZIP extraction completed")
        
        current_app.logger.info("Processing extracted files with new logic")
        
        for species_name in os.listdir(extract_target_dir):
            species_folder_path = os.path.join(extract_target_dir, species_name)
            
            if not os.path.isdir(species_folder_path):
                continue
                
            species_id = validate_species_folder(species_name, conn)
            if not species_id:
                stats['unknown_species'] += 1
                
                try:  # Count files safely for the report.
                    num_files = len([f for f in os.listdir(species_folder_path) if os.path.isfile(os.path.join(species_folder_path, f))])
                except Exception:
                    num_files = 'N/A'
                
                # Add "unknown species" error details.
                error_details = {
                    'species': species_name,
                    'filename': f'Вся папка ({num_files} файлів)',
                    'error': 'Невідомий вид (відсутній у базі даних)'
                }
                stats['error_files'].append(error_details)
                current_app.logger.warning(f"Unknown species, skipping folder: {species_name}")
                continue
            
            try:
                process_species_folder(
                    species_folder_path, species_name, species_id, upload_directory, 
                    system_user_id, stats
                )
            except Exception as e:
                current_app.logger.error(f"Critical error processing species {species_name}: {e}")
        
        stats['unverified_files'] = stats['processed_files'] - stats['positive_verifications'] - stats['negative_verifications']
        stats['processed_species'] = list(stats['processed_species'])
        
        current_app.logger.info(f"Processing completed. Stats: {stats}")
        return stats
        
    except Exception as e:
        current_app.logger.error(f"Error in process_zip_archive: {e}")
        current_app.logger.error(f"Traceback: {traceback.format_exc()}")
        raise
        
    finally:
        if conn:
            conn.close()
        if temp_extract_dir and os.path.exists(temp_extract_dir):
            try:
                shutil.rmtree(temp_extract_dir)
                current_app.logger.info(f"Temp directory cleaned up: {temp_extract_dir}")
            except Exception as e:
                current_app.logger.error(f"Error cleaning up temp directory: {e}")

def get_upload_statistics():
    """Return overall statistics for uploaded segments."""
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Total segment count.
        total_segments = conn.execute(text("SELECT COUNT(*) FROM segments")).fetchone()[0]
        
        # Count by status.
        status_results = conn.execute(text("""
            SELECT status, COUNT(*) 
            FROM segments 
            GROUP BY status
        """)).fetchall()
        status_counts = dict(status_results)
        
        # Count the real number of unique species that have segments.
        total_species_with_segments = conn.execute(text(
            "SELECT COUNT(DISTINCT species_id) FROM segments"
        )).fetchone()[0]
        
        # Count by species (top 10 for the page list).
        species_top10_results = conn.execute(text("""
            SELECT s.scientific_name, COUNT(seg.id)
            FROM segments seg
            JOIN species s ON seg.species_id = s.species_id
            GROUP BY s.species_id, s.scientific_name
            ORDER BY COUNT(seg.id) DESC
            LIMIT 10
        """)).fetchall()
        species_top10_counts = dict(species_top10_results)
        
        return {
            'total_segments': total_segments,
            'status_counts': status_counts,
            'total_species_with_segments': total_species_with_segments,
            'species_counts': species_top10_counts
        }
        
    except Exception as e:
        current_app.logger.error(f"Error retrieving statistics: {e}")
        return {}
    finally:
        if conn:
            conn.close()
