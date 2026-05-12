# myproject/app/pam/pam_evaluation_utils.py

from flask import current_app
from .utils import get_pam_db_connection
from sqlalchemy import text
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score, cohen_kappa_score
import numpy as np
import pandas as pd
import math
import os
import subprocess
import shutil
from datetime import datetime

def convert_numpy_types(obj):
    """
    Рекурсивно конвертує numpy типи в стандартні Python типи для збереження в БД.
    """
    import numpy as np
    
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    else:
        return obj

def get_species_for_dropdown():
    """
    Отримує список видів, які мають хоча б одну верифікацію,
    для відображення у випадаючому списку адмінки.

    Додатково повертає для кожного виду:
      • verified_segments — кількість сегментів виду, які мають ≥1 верифікацію
      • total_verifications — загальна кількість верифікацій по сегментах виду
    Це дозволяє користувачу одразу бачити, чи варто запускати перерахунок
    (поріг — мінімум 5 сегментів з верифікаціями).
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        query = """
            SELECT s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en,
                   COUNT(DISTINCT seg.id) AS verified_segments,
                   COUNT(sv.id)           AS total_verifications
            FROM species s
            JOIN segments seg               ON s.species_id = seg.species_id
            JOIN segment_verifications sv   ON seg.id = sv.segment_id
            WHERE sv.verification_result IS NOT NULL
            GROUP BY s.species_id, s.scientific_name, s.common_name_uk, s.common_name_en
            ORDER BY s.scientific_name
        """
        return conn.execute(text(query)).fetchall()
    except Exception as e:
        current_app.logger.error(f"Error getting species list: {e}")
        return []
    finally:
        if conn: conn.close()


def _get_species_diagnostic(conn, species_id, min_verifications):
    """
    Повертає діагностичну інформацію про чому конкретний вид міг бути
    пропущений під час перерахунку. Використовується для побудови
    зрозумілих повідомлень для користувача.

    Повертає dict:
      species_name           — наукова назва
      total_segments         — усі сегменти виду (з верифікаціями чи без)
      verified_segments      — сегменти, що мають хоч одну верифікацію
      segments_meeting_min   — сегменти з ≥ min_verifications верифікаціями
      total_verifications    — загальна кількість верифікацій
      min_verifications      — поточний поріг
      required_segments      — мінімальна кількість сегментів (5)
    """
    row = conn.execute(text("""
        SELECT
            s.scientific_name,
            (SELECT COUNT(*) FROM segments WHERE species_id = :sid)                                          AS total_segments,
            (SELECT COUNT(DISTINCT seg.id)
               FROM segments seg
               JOIN segment_verifications sv ON sv.segment_id = seg.id
              WHERE seg.species_id = :sid AND sv.verification_result IS NOT NULL)                           AS verified_segments,
            (SELECT COUNT(sv.id)
               FROM segments seg
               JOIN segment_verifications sv ON sv.segment_id = seg.id
              WHERE seg.species_id = :sid AND sv.verification_result IS NOT NULL)                           AS total_verifications,
            (SELECT COUNT(*) FROM (
                SELECT seg.id
                  FROM segments seg
                  JOIN segment_verifications sv ON sv.segment_id = seg.id
                 WHERE seg.species_id = :sid AND sv.verification_result IS NOT NULL
                 GROUP BY seg.id
                HAVING COUNT(sv.verification_result) >= :minv
            ) AS x)                                                                                          AS segments_meeting_min
        FROM species s
        WHERE s.species_id = :sid
    """), {'sid': species_id, 'minv': min_verifications}).fetchone()

    if not row:
        return None
    return {
        'species_name':         row.scientific_name,
        'total_segments':       int(row.total_segments or 0),
        'verified_segments':    int(row.verified_segments or 0),
        'segments_meeting_min': int(row.segments_meeting_min or 0),
        'total_verifications':  int(row.total_verifications or 0),
        'min_verifications':    int(min_verifications),
        'required_segments':    5,
    }


def _build_insufficient_data_message(diag):
    """Будує читабельне повідомлення на основі діагностики виду."""
    name = diag['species_name']
    if diag['total_segments'] == 0:
        return f"{name}: у виду немає жодного сегмента."
    if diag['verified_segments'] == 0:
        return (f"{name}: є {diag['total_segments']} сегмент(ів), "
                f"але жоден ще не верифіковано.")
    if diag['segments_meeting_min'] < diag['required_segments']:
        return (f"{name}: {diag['segments_meeting_min']} сегмент(ів) з "
                f"≥{diag['min_verifications']} верифікаціями "
                f"(всього {diag['verified_segments']} верифікованих сегментів, "
                f"{diag['total_verifications']} верифікацій). "
                f"Потрібно мінімум {diag['required_segments']} — "
                f"додайте верифікацій або зменште поріг.")
    # Безпечний fallback
    return (f"{name}: недостатньо даних "
            f"(сегменти ≥{diag['min_verifications']} вериф.: "
            f"{diag['segments_meeting_min']}, потрібно ≥{diag['required_segments']}).")

def calculate_species_metrics(species_id, min_verifications=2, consensus_threshold=2.0/3.0):
    """
    Розраховує Precision та логістичну регресію для конкретного виду.
    Додано: Бутстреп для розрахунку 95% довірчого інтервалу Precision.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        query = """
            SELECT seg.id, seg.confidence_level,
                   AVG(CASE WHEN sv.verification_result = 1 THEN 1.0 ELSE 0.0 END) as avg_verification
            FROM segments seg
            JOIN segment_verifications sv ON seg.id = sv.segment_id
            WHERE seg.species_id = :species_id 
            AND sv.verification_result IS NOT NULL
            GROUP BY seg.id, seg.confidence_level
            HAVING COUNT(sv.verification_result) >= :min_verifications
        """
        
        results = conn.execute(text(query), {
            'species_id': species_id, 
            'min_verifications': min_verifications
        }).fetchall()
        
        if len(results) < 5:
            return None
        
        # 1. Формуємо масив бінарних результатів (1 - правильно, 0 - помилка)
        binary_outcomes = []
        
        for result in results:
            avg_verification = result[2]
            
            if avg_verification >= consensus_threshold:
                binary_outcomes.append(1)
            elif avg_verification <= (1 - consensus_threshold):
                binary_outcomes.append(0)
        
        total_n = len(binary_outcomes)
        if total_n == 0:
            return None
            
        # 2. Розрахунок Precision (середнє значення масиву)
        outcomes_np = np.array(binary_outcomes)
        precision = float(outcomes_np.mean())
        
        # 3. Бутстреп для CI (95%)
        n_bootstraps = 10000
        # Генеруємо індекси для вибірки з поверненням
        # Це швидше, ніж np.random.choice для самого масиву в циклі
        boot_means = []
        
        # Якщо вибірка дуже мала, бутстреп може бути нестабільним, але це краще ніж нічого
        if total_n >= 3:
            for _ in range(n_bootstraps):
                # Вибірка з поверненням
                sample = np.random.choice(outcomes_np, size=total_n, replace=True)
                boot_means.append(sample.mean())
            
            lower_ci = float(np.percentile(boot_means, 2.5))
            upper_ci = float(np.percentile(boot_means, 97.5))
        else:
            # Якщо даних критично мало, інтервал = precision
            lower_ci = precision
            upper_ci = precision

        # 4. Логістична регресія (без змін)
        logistic_results = calculate_logistic_regression(species_id, min_verifications, consensus_threshold)
        
        result_dict = {
            'species_id': species_id,
            'precision_score': precision,
            'precision_lower_ci': lower_ci,
            'precision_upper_ci': upper_ci,
            'total_samples': total_n,
            
            # Логістичні параметри (обов'язково!)
            'logistic_beta0': logistic_results.get('beta0'),
            'logistic_beta1': logistic_results.get('beta1'),
            'logistic_r_squared': logistic_results.get('r_squared'),
            'logistic_n_samples': logistic_results.get('n_samples'),
            'logistic_status': logistic_results.get('status'),
            
            # Пороги
            'p0_9_threshold': logistic_results.get('p0_9_threshold'),
            'p0_9_lower_ci': logistic_results.get('p0_9_lower_ci'),
            'p0_9_upper_ci': logistic_results.get('p0_9_upper_ci'),
            
            'p0_95_threshold': logistic_results.get('p0_95_threshold'),
            'p0_95_lower_ci': logistic_results.get('p0_95_lower_ci'),
            'p0_95_upper_ci': logistic_results.get('p0_95_upper_ci'),
            
            'p0_99_threshold': logistic_results.get('p0_99_threshold'),
            'p0_99_lower_ci': logistic_results.get('p0_99_lower_ci'),
            'p0_99_upper_ci': logistic_results.get('p0_99_upper_ci'),
        }
        
        return result_dict
        
    except Exception as e:
        current_app.logger.error(f"Error calculating metrics for species {species_id}: {e}")
        return None
    finally:
        if conn: conn.close()

def find_optimal_threshold(confidences, true_labels, step=0.05):
    """
    Знаходить оптимальний поріг confidence для максимізації F1-score.
    
    Args:
        confidences: список значень confidence
        true_labels: список істинних позначок
        step: крок для перебору порогів
        
    Returns:
        float: оптимальний поріг confidence
    """
    try:
        current_app.logger.info(f"Finding optimal threshold for {len(confidences)} samples")
        current_app.logger.info(f"Confidences range: {min(confidences)} to {max(confidences)}")
        current_app.logger.info(f"True labels distribution: {sum(true_labels)} positive out of {len(true_labels)}")
        
        best_threshold = 0.5
        best_f1 = 0
        
        for threshold in np.arange(0.1, 1.0, step):
            y_pred = [1 if c >= threshold else 0 for c in confidences]
            current_f1 = f1_score(true_labels, y_pred, zero_division=0)
            
            if current_f1 > best_f1:
                best_f1 = current_f1
                best_threshold = threshold
        
        current_app.logger.info(f"Optimal threshold: {best_threshold} with F1: {best_f1}")
        return best_threshold
        
    except Exception as e:
        current_app.logger.error(f"Error in find_optimal_threshold: {e}")
        return 0.5  # Fallback значення

def convert_numpy_types(obj):
    """
    Рекурсивно конвертує numpy типи в стандартні Python типи для збереження в БД.
    """
    import numpy as np
    
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    else:
        return obj

def recalculate_all_metrics(user_id, min_verifications=1, consensus_threshold=2.0/3.0, target_species_id=None):
    """
    Перераховує метрики.
    Якщо передано target_species_id - рахує тільки для нього.
    Якщо None - рахує для всіх (як раніше).
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # 1. Формуємо запит на вибірку видів
        base_query = """
            SELECT DISTINCT seg.species_id, s.scientific_name
            FROM segments seg
            JOIN species s ON seg.species_id = s.species_id
            JOIN segment_verifications sv ON seg.id = sv.segment_id
            WHERE sv.verification_result IS NOT NULL
        """
        
        params = {}
        
        # Якщо вибрано конкретний вид, додаємо фільтр
        if target_species_id is not None:
            base_query += " AND seg.species_id = :target_id"
            params['target_id'] = target_species_id
            
        base_query += """
            GROUP BY seg.species_id, s.scientific_name
            HAVING COUNT(DISTINCT seg.id) >= 5
        """
        
        species_list = conn.execute(text(base_query), params).fetchall()

        if not species_list:
            # Diagnose WHY there are no eligible species.
            if target_species_id is not None:
                diag = _get_species_diagnostic(conn, target_species_id, min_verifications)
                error_msg = _build_insufficient_data_message(diag) if diag else \
                    'Вид не знайдено у базі даних.'
                return {
                    'success': False,
                    'reason': 'insufficient_data',
                    'error': error_msg,
                    'diagnostic': diag,
                }
            return {
                'success': False,
                'reason': 'no_eligible_species',
                'error': ('У базі немає жодного виду з мінімум 5 сегментами, '
                          'які мають хоча б одну верифікацію.'),
            }
        
        # 2. Позначаємо попередні розрахунки як неактуальні
        # ВАЖЛИВО: Якщо рахуємо один вид, скидаємо is_current тільки для нього!
        if target_species_id is not None:
            conn.execute(text("UPDATE evaluation SET is_current = FALSE WHERE species_id = :sid"), 
                         {'sid': target_species_id})
        else:
            conn.execute(text("UPDATE evaluation SET is_current = FALSE"))
        
        calculated_species = []
        failed_species = []          # legacy: list of names
        failed_species_detail = []   # NEW: list of dicts with per-species reason
        logistic_stats = {'calculated': 0, 'insufficient_data': 0, 'error': 0}
        
        for species_id, scientific_name in species_list:
            # ... (Тут код залишається без змін, він вже використовує species_id з циклу) ...
            # ... ВИКЛИК calculate_species_metrics ...
            # ... INSERT INTO evaluation ...
            
            # КОПІЮЄМО ВАШ ІСНУЮЧИЙ КОД ЦИКЛУ ТУТ
            # Просто переконайтеся, що змінні всередині циклу не змінилися
            
            current_app.logger.info(f"Calculating metrics for species {scientific_name} (ID: {species_id})")
            metrics = calculate_species_metrics(species_id, min_verifications, consensus_threshold)
            
            if metrics:
                metrics = convert_numpy_types(metrics)
                # ... (Ваш великий INSERT запит тут) ...
                conn.execute(text("""
                    INSERT INTO evaluation (
                        species_id, precision_score, 
                        precision_lower_ci, precision_upper_ci,
                        total_samples,
                        calculation_version, calculated_by_user_id, is_current,
                        logistic_beta0, logistic_beta1, logistic_r_squared, 
                        logistic_n_samples, logistic_status, logistic_calculated_at,
                        p0_9_threshold, p0_9_lower_ci, p0_9_upper_ci,
                        p0_95_threshold, p0_95_lower_ci, p0_95_upper_ci,
                        p0_99_threshold, p0_99_lower_ci, p0_99_upper_ci
                    ) VALUES (
                        :species_id, :precision_score, 
                        :precision_lower_ci, :precision_upper_ci,
                        :total_samples, 
                        1, :user_id, TRUE,
                        :logistic_beta0, :logistic_beta1, :logistic_r_squared,
                        :logistic_n_samples, :logistic_status, CURRENT_TIMESTAMP,
                        :p0_9_threshold, :p0_9_lower_ci, :p0_9_upper_ci,
                        :p0_95_threshold, :p0_95_lower_ci, :p0_95_upper_ci,
                        :p0_99_threshold, :p0_99_lower_ci, :p0_99_upper_ci
                    )
                """), {
                    # ... (Ваші параметри) ...
                    'species_id': metrics['species_id'], 
                    'precision_score': metrics['precision_score'], 
                    'precision_lower_ci': metrics.get('precision_lower_ci'),
                    'precision_upper_ci': metrics.get('precision_upper_ci'),
                    'total_samples': metrics['total_samples'], 
                    'user_id': user_id,
                    'logistic_beta0': metrics['logistic_beta0'],
                    'logistic_beta1': metrics['logistic_beta1'],
                    'logistic_r_squared': metrics['logistic_r_squared'],
                    'logistic_n_samples': metrics['logistic_n_samples'],
                    'logistic_status': metrics['logistic_status'],
                    'p0_9_threshold': metrics['p0_9_threshold'],
                    'p0_9_lower_ci': metrics.get('p0_9_lower_ci'),
                    'p0_9_upper_ci': metrics.get('p0_9_upper_ci'),
                    'p0_95_threshold': metrics['p0_95_threshold'],
                    'p0_95_lower_ci': metrics.get('p0_95_lower_ci'),
                    'p0_95_upper_ci': metrics.get('p0_95_upper_ci'),
                    'p0_99_threshold': metrics['p0_99_threshold'],
                    'p0_99_lower_ci': metrics.get('p0_99_lower_ci'),
                    'p0_99_upper_ci': metrics.get('p0_99_upper_ci')
                })
                
                calculated_species.append(scientific_name)
                status = metrics.get('logistic_status', 'error')
                logistic_stats[status] = logistic_stats.get(status, 0) + 1
            else:
                failed_species.append(scientific_name)
                # Diagnose why this species fell short (after passing base filter)
                diag = _get_species_diagnostic(conn, species_id, min_verifications)
                if diag:
                    failed_species_detail.append({
                        'species_id': species_id,
                        'name': scientific_name,
                        'message': _build_insufficient_data_message(diag),
                        'diagnostic': diag,
                    })

        conn.commit()
        
        # ... (Формування результату) ...
        result = {
            'success': True,
            'calculated_count': len(calculated_species),
            'failed_count': len(failed_species),
            'calculated_species': calculated_species,
            'failed_species': failed_species,
            'failed_species_detail': failed_species_detail,
            'total_species_checked': len(species_list),
            'logistic_regression_stats': logistic_stats,
            'mode': 'single' if target_species_id else 'all'
        }
        
        current_app.logger.info(f"Metrics recalculation completed by user {user_id}: {result}")
        return result
        
    except Exception as e:
        if conn: conn.rollback()
        current_app.logger.error(f"Error recalculating metrics: {e}")
        return {'success': False, 'reason': 'exception', 'error': str(e)}
    finally:
        if conn: conn.close()

def get_evaluation_summary():
    """
    Повертає тільки загальну статистику для карток на сторінці.
    СПРОЩЕНО: тепер без топ-видів, тільки загальні цифри.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Загальна статистика по поточних метриках
        summary_query = """
            SELECT 
                COUNT(*) as total_species,
                SUM(total_samples) as total_samples,
                MAX(calculated_at) as last_calculation
            FROM evaluation 
            WHERE is_current = TRUE
        """
        
        summary = conn.execute(text(summary_query)).fetchone()
        
        # Перевіряємо чи є взагалі дані
        if not summary or summary[0] == 0:
            current_app.logger.warning("No evaluation data found")
            return {
                'summary': {
                    'total_species': 0,
                    'total_samples': 0,
                    'last_calculation': None
                },
                'logistic_summary': {
                    'logistic_calculated': 0,
                    'logistic_insufficient': 0,
                    'logistic_error': 0
                }
            }
        
        # Статистика логістичної регресії
        logistic_stats_query = """
            SELECT 
                COUNT(CASE WHEN logistic_status = 'calculated' THEN 1 END) as logistic_calculated,
                COUNT(CASE WHEN logistic_status = 'insufficient_data' THEN 1 END) as logistic_insufficient,
                COUNT(CASE WHEN logistic_status = 'error' THEN 1 END) as logistic_error
            FROM evaluation 
            WHERE is_current = TRUE
        """
        
        logistic_stats = conn.execute(text(logistic_stats_query)).fetchone()
        
        return {
            'summary': {
                'total_species': summary[0] or 0,
                'total_samples': summary[1] or 0,
                'last_calculation': summary[2] if summary[2] else None
            },
            'logistic_summary': {
                'logistic_calculated': logistic_stats[0] or 0,
                'logistic_insufficient': logistic_stats[1] or 0,
                'logistic_error': logistic_stats[2] or 0
            }
        }
        
    except Exception as e:
        current_app.logger.error(f"Error getting evaluation summary: {e}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return {
            'summary': {
                'total_species': 0,
                'total_samples': 0,
                'last_calculation': None
            },
            'logistic_summary': {
                'logistic_calculated': 0,
                'logistic_insufficient': 0,
                'logistic_error': 0
            }
        }
    finally:
        if conn:
            conn.close()

def cleanup_completed_verifications():
    """
    Видаляє файли завершених верифікацій (для адмінів).
    ОНОВЛЕНО: Тепер також видаляє відповідні файли спектрограм (.png).
    
    Returns:
        dict: Статистика видалення
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        completed_segments = conn.execute(text("""
            SELECT id, file_path, filename 
            FROM segments 
            WHERE status = 'completed'
        """)).fetchall()
        
        deleted_files = 0
        deleted_spectrograms = 0 # <--- Додано для статистики
        deleted_size = 0
        errors = []
        
        for segment_id, file_path, filename in completed_segments:
            try:
                # Видалення аудіофайлу
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    os.remove(file_path)
                    deleted_files += 1
                    deleted_size += file_size
                    
                    # ##### ПОЧАТОК НОВОГО БЛОКУ #####
                    # Спробуємо видалити відповідну спектрограму
                    try:
                        base_path, _ = os.path.splitext(file_path)
                        spectrogram_path = f"{base_path}.png"
                        if os.path.exists(spectrogram_path):
                            os.remove(spectrogram_path)
                            deleted_spectrograms += 1
                    except Exception as spec_e:
                        error_msg = f"Помилка видалення спектрограми для {filename}: {str(spec_e)}"
                        errors.append(error_msg)
                        current_app.logger.error(error_msg)
                    # ##### КІНЕЦЬ НОВОГО БЛОКУ #####

                    # Оновлюємо статус сегменту
                    conn.execute(text(
                        "UPDATE segments SET status = 'archived' WHERE id = :segment_id"
                    ), {'segment_id': segment_id})
                    
            except Exception as e:
                errors.append(f"Помилка видалення аудіофайлу {filename}: {str(e)}")
                current_app.logger.error(f"Error deleting file {file_path}: {e}")
        
        conn.commit()
        
        # Оновлюємо словник результатів
        return {
            'success': True,
            'deleted_files': deleted_files,
            'deleted_spectrograms': deleted_spectrograms, # <--- Додано
            'deleted_size_mb': round(deleted_size / (1024 * 1024), 2),
            'errors': errors
        }
        
    except Exception as e:
        if conn:
            conn.rollback()
        current_app.logger.error(f"Error in cleanup: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()

def calculate_logistic_regression(species_id, min_verifications=2, consensus_threshold=2.0/3.0):
    from sklearn.linear_model import LogisticRegression
    import numpy as np
    
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # Отримуємо дані
        query = """
            SELECT seg.confidence_level,
                   AVG(CASE WHEN sv.verification_result = 1 THEN 1.0 ELSE 0.0 END) as avg_verification
            FROM segments seg
            JOIN segment_verifications sv ON seg.id = sv.segment_id
            WHERE seg.species_id = :species_id 
            AND sv.verification_result IS NOT NULL
            GROUP BY seg.id, seg.confidence_level
            HAVING COUNT(sv.verification_result) >= :min_verifications
        """
        
        results = conn.execute(text(query), {
            'species_id': species_id, 
            'min_verifications': min_verifications
        }).fetchall()
        
        # Підготовка даних
        X_data = []
        y_data = []
        
        for r in results:
            confidence_level = float(r[0])
            avg_verification = float(r[1])
            
            if avg_verification >= consensus_threshold:
                X_data.append(confidence_level)
                y_data.append(1) 
            elif avg_verification <= (1 - consensus_threshold):
                X_data.append(confidence_level)
                y_data.append(0) 
        
        # Перевірки на достатність даних
        if len(X_data) < 10 or len(set(y_data)) < 2:
            return get_empty_logistic_result(len(X_data), 'insufficient_data')

        X = np.array(X_data).reshape(-1, 1)
        y = np.array(y_data)
        
        # --- ДОПОМІЖНА ФУНКЦІЯ: Розрахунок порогів для моделі ---
        def get_thresholds_from_model(model, X_input, y_input):
            try:
                beta0 = float(model.intercept_[0])
                beta1 = float(model.coef_[0][0])
                
                # Функція розрахунку одного порогу
                def calc_single(target_p):
                    if beta1 == 0: return None
                    threshold_raw = (math.log(target_p / (1 - target_p)) - beta0) / beta1
                    if threshold_raw > 1.0: return None
                    if threshold_raw < 0.1: return 0.1 # Технічний мінімум
                    return float(threshold_raw)

                return {
                    'beta0': beta0, 'beta1': beta1,
                    'p0_9': calc_single(0.9),
                    'p0_95': calc_single(0.95),
                    'p0_99': calc_single(0.99)
                }
            except:
                return None

        # 1. ОСНОВНА МОДЕЛЬ
        main_model = LogisticRegression(fit_intercept=True, random_state=42)
        main_model.fit(X, y)
        
        main_metrics = get_thresholds_from_model(main_model, X, y)
        if not main_metrics:
             return get_empty_logistic_result(len(X_data), 'error')

        # Псевдо R² для основної моделі
        y_pred_proba = main_model.predict_proba(X)[:, 1]
        try:
            null_deviance = -2 * np.sum(y * np.log(y.mean()) + (1-y) * np.log(1-y.mean()))
            model_deviance = -2 * np.sum(y * np.log(y_pred_proba) + (1-y) * np.log(1-y_pred_proba))
            r_squared = float(max(0, 1 - model_deviance / null_deviance))
        except:
            r_squared = 0.0

        # 2. БУТСТРЕП (Bootstrap) для інтервалів
        n_iterations = 1000 # Менше ітерацій для швидкості
        boot_p0_9 = []
        boot_p0_95 = []
        boot_p0_99 = []
        
        # Пропускаємо бутстреп, якщо даних дуже мало, бо буде багато помилок
        if len(X) >= 15:
            for _ in range(n_iterations):
                # Випадкові індекси з поверненням
                indices = np.random.choice(len(y), len(y), replace=True)
                X_boot = X[indices]
                y_boot = y[indices]
                
                # Пропускаємо, якщо у вибірці лише один клас (тільки 0 або тільки 1)
                if len(np.unique(y_boot)) < 2:
                    continue
                    
                try:
                    boot_model = LogisticRegression(fit_intercept=True, solver='lbfgs')
                    boot_model.fit(X_boot, y_boot)
                    
                    metrics = get_thresholds_from_model(boot_model, X_boot, y_boot)
                    if metrics:
                        if metrics['p0_9'] is not None: boot_p0_9.append(metrics['p0_9'])
                        if metrics['p0_95'] is not None: boot_p0_95.append(metrics['p0_95'])
                        if metrics['p0_99'] is not None: boot_p0_99.append(metrics['p0_99'])
                except:
                    continue
        
        # Розрахунок перцентилів (2.5% і 97.5%)
        def get_ci(values):
            if len(values) < 10: return None, None # Мало успішних ітерацій
            return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))

        p0_9_lower, p0_9_upper = get_ci(boot_p0_9)
        p0_95_lower, p0_95_upper = get_ci(boot_p0_95)
        p0_99_lower, p0_99_upper = get_ci(boot_p0_99)

        # "Запобіжник" для Квакші (High Precision / Low R²)
        # Якщо ми перезаписуємо основні пороги на 0.1, то інтервали втрачають сенс (або теж стають 0.1)
        positive_rate = y.mean()
        if r_squared < 0.05:
            if positive_rate >= 0.90: 
                main_metrics['p0_9'] = 0.1
                p0_9_lower, p0_9_upper = 0.1, 0.1
            else: main_metrics['p0_9'] = None
            
            if positive_rate >= 0.95: 
                main_metrics['p0_95'] = 0.1
                p0_95_lower, p0_95_upper = 0.1, 0.1
            else: main_metrics['p0_95'] = None
            
            if positive_rate >= 0.99: 
                main_metrics['p0_99'] = 0.1
                p0_99_lower, p0_99_upper = 0.1, 0.1
            else: main_metrics['p0_99'] = None

        return {
            'status': 'calculated',
            'n_samples': int(len(X_data)),
            'beta0': main_metrics['beta0'],
            'beta1': main_metrics['beta1'],
            'r_squared': r_squared,
            
            'p0_9_threshold': main_metrics['p0_9'],
            'p0_9_lower_ci': p0_9_lower, 'p0_9_upper_ci': p0_9_upper,
            
            'p0_95_threshold': main_metrics['p0_95'],
            'p0_95_lower_ci': p0_95_lower, 'p0_95_upper_ci': p0_95_upper,
            
            'p0_99_threshold': main_metrics['p0_99'],
            'p0_99_lower_ci': p0_99_lower, 'p0_99_upper_ci': p0_99_upper
        }
        
    except Exception as e:
        current_app.logger.error(f"Error in logistic regression: {e}")
        return get_empty_logistic_result(0, 'error')
    finally:
        if conn: conn.close()

def get_empty_logistic_result(n, status):
    return {
        'status': status, 'n_samples': n,
        'beta0': None, 'beta1': None, 'r_squared': None,
        'p0_9_threshold': None, 'p0_9_lower_ci': None, 'p0_9_upper_ci': None,
        'p0_95_threshold': None, 'p0_95_lower_ci': None, 'p0_95_upper_ci': None,
        'p0_99_threshold': None, 'p0_99_lower_ci': None, 'p0_99_upper_ci': None
    }

def convert_wav_to_flac():
    """
    Знаходить всі сегменти у форматі .wav і конвертує їх у .flac.
    Оновлює шляхи в базі даних та видаляє оригінальні .wav файли.
    Вимагає наявності FFmpeg в системі.
    
    ОНОВЛЕНО: Повністю перероблено для використання короткоживучих транзакцій
    для кожного файлу, щоб гарантувати атомарність та уникнути помилок блокування транзакцій.
    """
    FFMPEG_PATH = "/usr/bin/ffmpeg"
    if not os.path.exists(FFMPEG_PATH):
        error_msg = (f"FFmpeg не знайдено за шляхом {FFMPEG_PATH}. "
                     "Конвертація неможлива. Перевірте шлях або встановіть FFmpeg.")
        current_app.logger.error(error_msg)
        return {'success': False, 'error': error_msg}

    wav_segments = []
    conn = None

    # КРОК 1: Отримати список файлів для обробки та закрити з'єднання.
    try:
        conn = get_pam_db_connection()
        wav_segments = conn.execute(text(
            "SELECT id, file_path, filename FROM segments WHERE file_path LIKE '%.wav' AND status != 'archived'"
        )).fetchall()
    except Exception as e:
        current_app.logger.error(f"Помилка отримання списку WAV файлів з БД: {e}")
        return {'success': False, 'error': f"Помилка отримання списку файлів: {e}"}
    finally:
        if conn:
            conn.close()

    if not wav_segments:
        return {
            'success': True, 
            'message': 'Не знайдено файлів .wav для конвертації.',
            'converted_count': 0,
            'failed_count': 0,
            'errors': []
        }

    converted_count = 0
    failed_count = 0
    errors = []
    total_to_process = len(wav_segments)
    current_app.logger.info(f"Знайдено {total_to_process} .wav файлів для конвертації.")

    # КРОК 2: Обробити кожен файл в окремій транзакції.
    for index, (segment_id, wav_path, wav_filename) in enumerate(wav_segments):
        current_app.logger.info(f"Обробка файлу {index + 1}/{total_to_process}: ID {segment_id}, шлях {wav_path}")

        if not os.path.exists(wav_path):
            msg = f"Файл .wav для сегмента ID {segment_id} вже відсутній. Пропускаю."
            errors.append(msg)
            failed_count += 1
            current_app.logger.warning(msg)
            continue

        base_path, _ = os.path.splitext(wav_path)
        flac_path = f"{base_path}.flac"
        
        base_filename, _ = os.path.splitext(wav_filename)
        flac_filename = f"{base_filename}.flac"
        
        conn_item = None
        try:
            # Для кожного файлу отримуємо нове з'єднання з пулу
            conn_item = get_pam_db_connection()
            # Використовуємо 'with conn.begin()' для автоматичного commit/rollback
            with conn_item.begin():
                # 1. Конвертація
                command = [
                    FFMPEG_PATH, "-i", wav_path, "-y",
                    "-hide_banner", "-loglevel", "error", flac_path
                ]
                subprocess.run(command, check=True, text=True, capture_output=True)
                
                # 2. Оновлення запису в БД (в рамках транзакції)
                conn_item.execute(text(
                    "UPDATE segments SET file_path = :flac_path, filename = :flac_filename WHERE id = :segment_id"
                ), {
                    'flac_path': flac_path,
                    'flac_filename': flac_filename,
                    'segment_id': segment_id
                })
            # Тут транзакція автоматично комітиться, якщо не було помилок

            # 3. Видалення файлу ТІЛЬКИ ПІСЛЯ успішного commit
            os.remove(wav_path)
            converted_count += 1
            current_app.logger.info(f"   -> Успішно: ID {segment_id} конвертовано та оновлено в БД.")

        except subprocess.CalledProcessError as e:
            # Транзакція автоматично відкотиться завдяки 'with'
            msg = f"Помилка конвертації FFmpeg для ID {segment_id}: {e.stderr}"
            errors.append(msg)
            failed_count += 1
            current_app.logger.error(f"   -> Помилка: {msg}")
        except Exception as e:
            # Транзакція автоматично відкотиться завдяки 'with'
            msg = f"Неочікувана помилка при обробці ID {segment_id}: {str(e)}"
            errors.append(msg)
            failed_count += 1
            current_app.logger.error(f"   -> Помилка: {msg}")
        finally:
            if conn_item:
                conn_item.close()

    return {
        'success': True,
        'converted_count': converted_count,
        'failed_count': failed_count,
        'errors': errors
    }

# app/pam/pam_evaluation_utils.py

def get_species_logistic_data(species_id):
    """
    Отримує параметри моделі та ВСІ точки верифікації для виду.
    """
    conn = None
    try:
        conn = get_pam_db_connection()
        
        # 1. Отримуємо параметри
        query = text("""
            SELECT 
                s.scientific_name, s.common_name_uk, s.common_name_en,
                e.precision_score, e.total_samples,
                e.logistic_beta0, e.logistic_beta1, e.logistic_r_squared,
                e.p0_9_threshold, e.p0_95_threshold, e.p0_99_threshold
            FROM evaluation e
            JOIN species s ON e.species_id = s.species_id
            WHERE e.species_id = :species_id AND e.is_current = TRUE
        """)
        row = conn.execute(query, {'species_id': species_id}).mappings().fetchone()
        
        if not row:
            return None

        # Перетворюємо на словник і ЯВНО кастимо Decimal -> float
        # Це критично для коректної роботи JSON і математики в JS
        params = dict(row)
        for key in ['logistic_beta0', 'logistic_beta1', 'logistic_r_squared', 
                    'p0_9_threshold', 'p0_95_threshold', 'p0_99_threshold']:
            if params.get(key) is not None:
                params[key] = float(params[key])

        # 2. Отримуємо точки
        points_query = text("""
            SELECT 
                seg.id as segment_id,
                seg.confidence_level,
                AVG(CASE WHEN sv.verification_result = 1 THEN 1.0 ELSE 0.0 END) as avg_verification,
                COUNT(sv.id) as verification_count
            FROM segments seg
            JOIN segment_verifications sv ON seg.id = sv.segment_id
            WHERE seg.species_id = :species_id 
            AND sv.verification_result IS NOT NULL
            GROUP BY seg.id, seg.confidence_level
        """)
        
        raw_points = conn.execute(points_query, {'species_id': species_id}).mappings().fetchall()
        
        processed_points = []
        for p in raw_points:
            avg = float(p['avg_verification'])
            count = int(p['verification_count'])
            outcome = 1 if avg >= 0.5 else 0
            
            processed_points.append({
                'segment_id': p['segment_id'],
                'confidence': float(p['confidence_level']),
                'outcome': outcome,
                'avg_verification': avg,
                'verification_count': count
            })

        return {
            'info': params,
            'points': processed_points
        }
        
    except Exception as e:
        current_app.logger.error(f"Error getting logistic data: {e}")
        return None
    finally:
        if conn: conn.close()







