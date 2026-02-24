import logging
import pandas as pd
import numpy as np
import sys
from datetime import datetime
from sqlalchemy import create_engine, text, Column, Integer, Numeric, DateTime, UniqueConstraint
from sqlalchemy.orm import sessionmaker, declarative_base, Session as SessionType
from sqlalchemy.exc import SQLAlchemyError
from collections import defaultdict

# --- Налаштування логування ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Конфігурація ---
DEFAULT_THRESHOLD = 0.95
MIN_DETECTIONS_PER_YEAR = 50
BOOTSTRAP_ITERATIONS = 10000
RECORDING_DURATION_MIN = 5.0

# --- Налаштування підключення до БД ---
DATABASE_URI = None
engine = None
Session = None

# --- Оголошення моделей SQLAlchemy ---
Base = declarative_base()

class AnalyticsLog(Base):
    __tablename__ = 'analytics_log'
    species_id = Column(Integer, primary_key=True)
    detection_count = Column(Integer, nullable=False)
    last_calculated_at = Column(DateTime(timezone=True), nullable=False)

class SpeciesMonitoringPeriods(Base):
    __tablename__ = 'species_monitoring_periods'
    species_id = Column(Integer, primary_key=True)
    start_month = Column(Integer, nullable=False, default=1)
    end_month = Column(Integer, nullable=False, default=12)

class AnalysisIntermediate(Base):
    __tablename__ = 'analysis_intermediate'
    id = Column(Integer, primary_key=True, autoincrement=True)
    species_id = Column(Integer, nullable=False, index=True)
    location_id = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False, index=True)
    month = Column(Integer, nullable=False)
    month_part = Column(Integer, nullable=False)
    day_part = Column(Integer, nullable=False)
    detection_count = Column(Integer, nullable=False, default=0)
    effort_hours = Column(Numeric(10, 4), nullable=False, default=0.0)

class SpeciesYearlyTrends(Base):
    __tablename__ = 'species_yearly_trends'
    id = Column(Integer, primary_key=True, autoincrement=True)
    species_id = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    institution_id = Column(Integer, nullable=True)
    mean_rai = Column(Numeric(12, 4), nullable=False)
    lower_ci = Column(Numeric(12, 4), nullable=False)
    upper_ci = Column(Numeric(12, 4), nullable=False)
    calculated_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('species_id', 'year', 'institution_id', name='uix_species_year_inst'),)

class SpeciesBiotopeYearlyActivity(Base):
    __tablename__ = 'species_biotope_yearly_activity'
    id = Column(Integer, primary_key=True, autoincrement=True)
    species_id = Column(Integer, nullable=False, index=True)
    biotope_id = Column(Integer, nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    institution_id = Column(Integer, nullable=True)
    detection_count = Column(Integer, nullable=False, default=0)
    effort_hours = Column(Numeric(10, 4), nullable=False, default=0.0)
    __table_args__ = (UniqueConstraint('species_id', 'biotope_id', 'year', 'institution_id', name='uix_species_biotope_year_inst'),)


def _populate_monitoring_periods(session: SessionType):
    logging.info("Syncing species with monitoring periods table...")
    try:
        all_species_ids = set(pd.read_sql("SELECT species_id FROM species", session.bind)['species_id'])
        existing_species_ids = set(pd.read_sql("SELECT species_id FROM species_monitoring_periods", session.bind)['species_id'])
        new_species_ids = all_species_ids - existing_species_ids
        if new_species_ids:
            logging.info(f"Found {len(new_species_ids)} new species. Adding with default periods (Jan-Dec)...")
            new_periods = [SpeciesMonitoringPeriods(species_id=sp_id) for sp_id in new_species_ids]
            session.bulk_save_objects(new_periods)
            session.commit()
    except Exception as e:
        logging.error(f"Error populating monitoring periods: {e}", exc_info=True)
        session.rollback(); raise


def _get_species_confidence_thresholds(session: SessionType, default_threshold: float = DEFAULT_THRESHOLD) -> defaultdict:
    """
    Отримує порогові значення p0.95 для кожного виду з таблиці evaluation.
    Для кожного виду вибирається найновіший розрахунок, що відповідає критеріям.
    Якщо для виду немає валідного порогу, буде використано значення за замовчуванням.
    """
    logging.info(f"Fetching species-specific confidence thresholds (p=0.95), fallback to {default_threshold}...")
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
        )
        SELECT species_id, p0_95_threshold
        FROM RankedEvaluations
        WHERE rn = 1
    """)
    try:
        db_result = session.execute(query).mappings().fetchall()
        thresholds = {row['species_id']: float(row['p0_95_threshold']) for row in db_result}
        threshold_map = defaultdict(lambda: default_threshold)
        threshold_map.update(thresholds)
        logging.info(f"Loaded {len(thresholds)} custom thresholds. Using {default_threshold} for others.")
        return threshold_map
    except Exception as e:
        logging.error(f"Could not fetch evaluation thresholds: {e}", exc_info=True)
        logging.warning("Falling back to default threshold for ALL species.")
        return defaultdict(lambda: default_threshold)


def _precalculate_effort_map(session: SessionType):
    logging.info("Pre-calculating monitoring effort map...")
    sql_query = "SELECT r.datetime_start, l.location_id FROM recordings r JOIN locations l ON r.location_id = l.location_id WHERE r.datetime_start IS NOT NULL;"
    df = pd.read_sql(sql_query, session.bind, parse_dates=['datetime_start'])
    if df.empty: return pd.DataFrame()
    if pd.api.types.is_datetime64_any_dtype(df['datetime_start']) and df['datetime_start'].dt.tz is not None:
        df['datetime_start'] = df['datetime_start'].dt.tz_localize(None)
    df['year'] = df['datetime_start'].dt.year
    df['month'] = df['datetime_start'].dt.month
    df['day'] = df['datetime_start'].dt.day
    df['month_part'] = pd.cut(df['day'], bins=[0, 10, 20, 31], labels=[1, 2, 3], right=True).astype(int)
    df['day_part'] = (df['datetime_start'].dt.hour // 6) + 1
    grouping_keys = ['year', 'location_id', 'month', 'month_part', 'day_part']
    effort_df = df.groupby(grouping_keys).size().reset_index(name='recording_count')
    effort_df['effort_hours'] = (effort_df['recording_count'] * RECORDING_DURATION_MIN) / 60.0
    logging.info(f"Effort map calculated with {len(effort_df)} unique blocks.")
    return effort_df.drop(columns=['recording_count'])


def _precalculate_biotope_effort_map(session: SessionType) -> pd.DataFrame:
    """
    Розраховує сумарний час моніторингу (в годинах) для кожного біотопу за кожен рік.
    """
    logging.info("Pre-calculating monitoring effort map per biotope per year...")
    sql_query = text("""
        SELECT
            lb.biotope_id,
            EXTRACT(YEAR FROM r.datetime_start) as year,
            COUNT(r.recording_id) as recording_count
        FROM recordings r
        JOIN location_biotopes lb ON r.location_id = lb.location_id
        WHERE r.datetime_start IS NOT NULL
        GROUP BY lb.biotope_id, EXTRACT(YEAR FROM r.datetime_start)
    """)
    try:
        effort_df = pd.read_sql(sql_query, session.bind)
        if effort_df.empty:
            logging.warning("No recording effort data found for biotopes.")
            return pd.DataFrame(columns=['biotope_id', 'year', 'effort_hours'])
        effort_df['effort_hours'] = (effort_df['recording_count'] * RECORDING_DURATION_MIN) / 60.0
        effort_df.drop(columns=['recording_count'], inplace=True)
        effort_df.dropna(subset=['year'], inplace=True)
        effort_df['year'] = effort_df['year'].astype(int)
        logging.info(f"Biotope effort map calculated with {len(effort_df)} unique biotope-year blocks.")
        return effort_df
    except Exception as e:
        logging.error(f"Failed during biotope effort map calculation: {e}", exc_info=True)
        return pd.DataFrame(columns=['biotope_id', 'year', 'effort_hours'])


def _get_species_to_process(session: SessionType, force: bool, trends_only: bool, biotopes_only: bool):
    logging.info("Determining which species need processing...")
    if trends_only:
        logging.info("Mode '--trends-only': will re-calculate trends for all species present in 'analysis_intermediate'.")
        species_df = pd.read_sql("SELECT DISTINCT species_id FROM analysis_intermediate", session.bind)
        species_df['count'] = 0 
        return species_df.set_index('species_id')
    if biotopes_only:
        logging.info("Mode '--biotopes-only': will re-calculate biotope activity for all species with detections.")
        species_df = pd.read_sql("SELECT DISTINCT species_id FROM detections", session.bind)
        species_df['count'] = 0
        return species_df.set_index('species_id')
    species_counts_sql = "SELECT species_id, COUNT(detection_id) as count FROM detections GROUP BY species_id"
    current_counts_df = pd.read_sql(species_counts_sql, session.bind).set_index('species_id')
    if force:
        logging.info("Force flag is set. All species will be processed.")
        return current_counts_df
    log_counts_df = pd.read_sql("SELECT species_id, detection_count FROM analytics_log", session.bind).set_index('species_id')
    comparison_df = current_counts_df.join(log_counts_df, how='left', rsuffix='_log').fillna(0)
    changed_species_df = comparison_df[comparison_df['count'] != comparison_df['detection_count']]
    logging.info(f"Found {len(changed_species_df)} species with changed data that need processing.")
    return changed_species_df.drop(columns=['detection_count'])


def _calculate_and_save_biotope_activity(
    session: SessionType, 
    species_ids: list, 
    biotope_effort_df: pd.DataFrame, 
    confidence_thresholds: defaultdict,
    institution_id: int = None
):
    """
    Розраховує активність видів по біотопах.
    Підтримує фільтрацію по установі:
    - Якщо institution_id is None -> Глобальний розрахунок.
    - Якщо institution_id задано -> Враховуються тільки локації цієї установи.
    """
    if not species_ids:
        return

    # 1. Очищення старих даних для конкретного контексту (глобально або для установи)
    # Це критично, щоб не дублювати дані при повторному запуску
    query_filter = session.query(SpeciesBiotopeYearlyActivity).filter(
        SpeciesBiotopeYearlyActivity.species_id.in_(species_ids)
    )
    
    if institution_id is None:
        query_filter = query_filter.filter(SpeciesBiotopeYearlyActivity.institution_id.is_(None))
    else:
        query_filter = query_filter.filter(SpeciesBiotopeYearlyActivity.institution_id == institution_id)
        
    query_filter.delete(synchronize_session=False)

    # 2. Формування динамічних умов для SQL
    # Індивідуальні пороги впевненості (Confidence Thresholds)
    case_conditions = [
        f"WHEN d.species_id = {int(sid)} THEN d.confidence >= {float(thr)}" 
        for sid, thr in confidence_thresholds.items() if sid in species_ids
    ]
    
    default_threshold = confidence_thresholds.default_factory()
    if case_conditions:
        case_statement = "CASE\n" + "\n".join(case_conditions) + f"\nELSE d.confidence >= {default_threshold}\nEND"
        where_confidence_clause = f"({case_statement})"
    else:
        where_confidence_clause = f"d.confidence >= {default_threshold}"

    # Динамічний JOIN та WHERE для інституції
    inst_join = ""
    inst_where = ""
    sql_params = {'species_ids': tuple(species_ids)}

    if institution_id is not None:
        # Якщо рахуємо для установи, приєднуємо таблицю зв'язків
        inst_join = "JOIN location_institutions li ON r.location_id = li.location_id"
        inst_where = "AND li.institution_id = :inst_id"
        sql_params['inst_id'] = institution_id

    # 3. Виконання запиту на отримання детекцій
    sql_query = text(f"""
        SELECT 
            d.species_id, 
            lb.biotope_id, 
            EXTRACT(YEAR FROM r.datetime_start) as year, 
            COUNT(d.detection_id) as detection_count
        FROM detections d
        JOIN recordings r ON d.recording_id = r.recording_id
        JOIN location_biotopes lb ON r.location_id = lb.location_id
        {inst_join}
        WHERE 
            d.species_id IN :species_ids 
            AND {where_confidence_clause}
            {inst_where}
        GROUP BY d.species_id, lb.biotope_id, EXTRACT(YEAR FROM r.datetime_start)
    """)
    
    try:
        detections_df = pd.read_sql(sql_query, session.bind, params=sql_params)
        
        if detections_df.empty:
            return

        detections_df.dropna(subset=['year'], inplace=True)
        detections_df['year'] = detections_df['year'].astype(int)

        # 4. Об'єднання з даними про зусилля (Effort)
        # Примітка: biotope_effort_df повинен бути попередньо відфільтрований або глобальний
        # в залежності від логіки виклику. Тут ми просто робимо merge.
        merged_df = pd.merge(biotope_effort_df, detections_df, on=['biotope_id', 'year'], how='left')
        
        # Заповнюємо пропуски для видів/років без детекцій, але з зусиллям
        merged_df['detection_count'] = merged_df['detection_count'].fillna(0)
        
        # Вибираємо тільки ті рядки, де species_id є у списку (або додалися через merge)
        # Оскільки merge left, у нас будуть рядки з effort, але без species_id.
        # Нам потрібно зберегти записи тільки для наших species_ids
        final_records = []
        
        # Оптимізація: ітеруємося по унікальних видах, щоб "розмножити" дані effort для кожного виду
        unique_species = set(species_ids)
        
        # Перетворюємо на словник для швидкого доступу
        effort_dict = biotope_effort_df.set_index(['biotope_id', 'year']).to_dict('index')
        det_dict = detections_df.set_index(['species_id', 'biotope_id', 'year'])['detection_count'].to_dict()

        bulk_data = []
        
        # Проходимо по наявних детекціях (це швидше ніж декартовий добуток)
        # Але це не додасть нульові записи. Якщо потрібні нулі - логіка має бути іншою.
        # В поточній реалізації зберігаємо тільки фактичні дані (детекції > 0 або effort > 0)
        # Для спрощення зберігаємо результат merge, фільтруючи NaN у species_id
        
        # Відновлюємо species_id для записів, які прийшли з detections_df
        # Ті записи, що прийшли з effort_df і не зматчились, матимуть NaN species_id.
        # Ми їх відкидаємо, бо ми не зберігаємо "нульову активність" для всіх видів, щоб не роздувати базу.
        final_df = merged_df.dropna(subset=['species_id']).copy()

        if final_df.empty:
            return

        # 5. Підготовка до вставки
        final_df['institution_id'] = institution_id  # Важливо: записуємо контекст

        # Приведення типів для коректної серіалізації в БД
        final_df = final_df.astype({
            'species_id': 'int64', 
            'biotope_id': 'int64', 
            'year': 'int64', 
            'detection_count': 'int64', 
            'effort_hours': 'float64'
        })
        
        # Конвертація NaN у None для поля institution_id (якщо воно не задане)
        records = final_df.where(pd.notnull(final_df), None).to_dict(orient='records')
        
        session.bulk_insert_mappings(SpeciesBiotopeYearlyActivity, records)
        logging.info(f"Saved {len(records)} biotope activity records (Institution: {institution_id}).")

    except Exception as e:
        logging.error(f"Failed during biotope activity calculation: {e}", exc_info=True)
        raise


def _calculate_and_save_trends(
    session: SessionType, 
    intermediate_df: pd.DataFrame, 
    monitoring_periods: pd.DataFrame, 
    species_id: int,
    institution_id: int = None
):
    """
    Розраховує RAI (Relative Abundance Index) та довірчі інтервали.
    intermediate_df вже повинен містити дані, відфільтровані для конкретної установи (або глобальні).
    """
    if intermediate_df.empty:
        return
    
    # 1. Очищення старих трендів для цього виду та цієї установи
    query_filter = session.query(SpeciesYearlyTrends).filter(
        SpeciesYearlyTrends.species_id == species_id
    )
    
    if institution_id is None:
        query_filter = query_filter.filter(SpeciesYearlyTrends.institution_id.is_(None))
    else:
        query_filter = query_filter.filter(SpeciesYearlyTrends.institution_id == institution_id)
        
    query_filter.delete(synchronize_session=False)

    # 2. Фільтрація за фенологією (періодом моніторингу виду)
    try:
        species_period = monitoring_periods.loc[species_id]
        season_df = intermediate_df[
            (intermediate_df['month'] >= species_period['start_month']) & 
            (intermediate_df['month'] <= species_period['end_month'])
        ]
    except KeyError:
        # Якщо для виду не задано період, беремо весь рік
        season_df = intermediate_df

    if season_df.empty:
        return

    # 3. Агрегація даних по роках
    yearly_summary = season_df.groupby('year').agg(
        total_detections=('detection_count', 'sum'),
        total_effort=('effort_hours', 'sum')
    ).reset_index()

    # Відкидаємо роки, де детекцій замало для статистичної значущості
    valid_years_summary = yearly_summary[yearly_summary['total_detections'] >= MIN_DETECTIONS_PER_YEAR]
    
    if valid_years_summary.empty:
        return
        
    trends_to_save = []
    
    # 4. Розрахунок RAI та Bootstrapping для кожного року
    for _, row in valid_years_summary.iterrows():
        year = row['year']
        year_df = season_df[season_df['year'] == year]
        
        # Захист від ділення на нуль
        if year_df['effort_hours'].sum() == 0:
            continue
        
        # RAI для кожного блоку даних (день/локація)
        # RAI = Detections / Effort (hours)
        with np.errstate(divide='ignore', invalid='ignore'):
            rai_values = (year_df['detection_count'] / year_df['effort_hours'])
            rai_values = rai_values.replace([np.inf, -np.inf], np.nan).dropna().values
        
        if len(rai_values) == 0:
            continue
        
        # Bootstrapping (Monte Carlo resampling)
        # Якщо точок мало, бутстреп ненадійний, беремо просте середнє
        if len(rai_values) < 5:
             mean_rai = float(np.mean(rai_values))
             lower_ci, upper_ci = mean_rai, mean_rai
        else:
            # Генеруємо BOOTSTRAP_ITERATIONS вибірок
            resampled_means = np.random.choice(rai_values, size=(BOOTSTRAP_ITERATIONS, len(rai_values)), replace=True).mean(axis=1)
            
            mean_rai = float(np.mean(resampled_means))
            lower_ci = float(np.percentile(resampled_means, 2.5))
            upper_ci = float(np.percentile(resampled_means, 97.5))

        trends_to_save.append(SpeciesYearlyTrends(
            species_id=species_id,
            year=int(year),
            mean_rai=mean_rai,
            lower_ci=lower_ci,
            upper_ci=upper_ci,
            institution_id=institution_id  # Зберігаємо контекст
        ))
        
    # 5. Збереження в БД
    if trends_to_save:
        session.bulk_save_objects(trends_to_save)
        # logging.info(f"Saved trends for species {species_id} (Inst: {institution_id}), Years: {len(trends_to_save)}")


def _get_location_institution_map(session: SessionType) -> pd.DataFrame:
    """
    Завантажує зв'язок між локаціями та установами.
    Повертає DataFrame з колонками [location_id, institution_id].
    """
    sql = "SELECT location_id, institution_id FROM location_institutions"
    return pd.read_sql(sql, session.bind)

def _get_all_institution_ids(session: SessionType) -> list:
    """Отримує список ID всіх установ."""
    # Перевіряємо, чи існує таблиця institutions (для безпеки)
    try:
        return [r[0] for r in session.execute(text("SELECT id FROM institutions")).fetchall()]
    except Exception:
        return []

def _run_calculation_cycle(session: SessionType, species_to_process_df: pd.DataFrame, trends_only: bool, biotopes_only: bool):
    """
    Оновлений цикл: розраховує тренди ГЛОБАЛЬНО та ОКРЕМО для кожної установи.
    """
    if species_to_process_df.empty:
        logging.info("No species to process. Exiting.")
        return

    # Завантажуємо допоміжні дані
    confidence_thresholds = _get_species_confidence_thresholds(session)
    biotope_effort_df = _precalculate_biotope_effort_map(session)
    loc_inst_map = _get_location_institution_map(session) # Мапа локацій
    all_institutions = _get_all_institution_ids(session)  # Список установ

    # --- БЛОК 1: БІОТОПИ (Biotopes Only або повний цикл) ---
    # Примітка: Поки що розраховуємо біотопи тільки ГЛОБАЛЬНО (None), 
    # оскільки biotope_effort_df агрегований без урахування установ. 
    # Для повної підтримки фільтрів по біотопах треба перебудувати карту зусиль (наступні кроки).
    if biotopes_only:
        logging.info("--- Running in --biotopes-only mode (Global only) ---")
        all_species_ids = species_to_process_df.index.tolist()
        try:
            if all_species_ids:
                _calculate_and_save_biotope_activity(session, all_species_ids, biotope_effort_df, confidence_thresholds, institution_id=None)
                session.commit()
            logging.info("Biotope activity calculation complete.")
        except Exception:
            session.rollback()
            logging.error("Biotope activity calculation failed.", exc_info=True)
        return
    
    # --- БЛОК 2: ТРЕНДИ (Основний цикл) ---
    effort_df = _precalculate_effort_map(session)
    if effort_df.empty and not trends_only: return
    monitoring_periods = pd.read_sql("SELECT * FROM species_monitoring_periods", session.bind).set_index('species_id')
    
    all_species_ids = species_to_process_df.index.tolist()
    total_species = len(all_species_ids)

    # Попередній розрахунок біотопів для повного циклу (Глобально)
    if not trends_only and all_species_ids:
        try:
            _calculate_and_save_biotope_activity(session, all_species_ids, biotope_effort_df, confidence_thresholds, institution_id=None)
            session.commit()
        except Exception:
            session.rollback()
            logging.error("Could not complete global biotope activity pre-calculation.")

    # Головний цикл по видах
    for i, species_id in enumerate(all_species_ids):
        logging.info(f"--- Processing species {species_id} ({i+1}/{total_species}) ---")
        try:
            intermediate_df = pd.DataFrame()
            
            # Або завантажуємо існуючі, або генеруємо нові проміжні дані
            if trends_only:
                sql_intermediate = text("SELECT * FROM analysis_intermediate WHERE species_id = :sid")
                intermediate_df = pd.read_sql(sql_intermediate, session.bind, params={'sid': species_id})
                if intermediate_df.empty:
                    logging.warning(f"No intermediate data found for species {species_id}. Skipping.")
                    continue
            else:
                session.query(AnalysisIntermediate).filter(AnalysisIntermediate.species_id == species_id).delete(synchronize_session=False)
                confidence_threshold = confidence_thresholds[species_id]
                
                sql_species = text("""
                    SELECT d.confidence, r.datetime_start, l.location_id 
                    FROM detections d 
                    JOIN recordings r ON d.recording_id = r.recording_id 
                    JOIN locations l ON r.location_id = l.location_id 
                    WHERE d.species_id = :species_id AND d.confidence >= :confidence AND r.datetime_start IS NOT NULL
                """)
                species_df = pd.read_sql(sql_species, session.bind, params={'species_id': species_id, 'confidence': confidence_threshold}, parse_dates=['datetime_start'])

                if species_df.empty: 
                    logging.info(f"No detections above threshold for species {species_id}. Skipping.")
                    continue

                if pd.api.types.is_datetime64_any_dtype(species_df['datetime_start']) and species_df['datetime_start'].dt.tz is not None:
                    species_df['datetime_start'] = species_df['datetime_start'].dt.tz_localize(None)
                
                # Агрегація даних (день/локація)
                species_df['year'] = species_df['datetime_start'].dt.year
                species_df['month'] = species_df['datetime_start'].dt.month
                species_df['day'] = species_df['datetime_start'].dt.day
                species_df['month_part'] = pd.cut(species_df['day'], bins=[0, 10, 20, 31], labels=[1, 2, 3], right=True).astype(int)
                species_df['day_part'] = (species_df['datetime_start'].dt.hour // 6) + 1
                
                grouping_keys = ['year', 'location_id', 'month', 'month_part', 'day_part']
                detections_agg = species_df.groupby(grouping_keys).size().reset_index(name='detection_count')
                
                # Merge з effort (зусиллями)
                intermediate_df = pd.merge(effort_df, detections_agg, on=grouping_keys, how='left').fillna(0)
                intermediate_df['species_id'] = species_id
                
                if not intermediate_df.empty:
                    session.bulk_insert_mappings(AnalysisIntermediate, intermediate_df.to_dict(orient='records'))
            
            # === РОЗРАХУНОК ТРЕНДІВ ===
            
            # 1. Глобальний розрахунок (Всі установи разом)
            _calculate_and_save_trends(session, intermediate_df, monitoring_periods, species_id, institution_id=None)
            
            # 2. Розрахунок по кожній установі окремо
            if not loc_inst_map.empty and not intermediate_df.empty:
                # Додаємо institution_id до даних через merge по location_id
                merged_inter = pd.merge(intermediate_df, loc_inst_map, on='location_id', how='inner')
                
                # Групуємо по установі і рахуємо тренди для кожної групи
                for inst_id, group_df in merged_inter.groupby('institution_id'):
                    _calculate_and_save_trends(session, group_df, monitoring_periods, species_id, institution_id=int(inst_id))

            # Логування прогресу
            if not trends_only:
                current_detection_count = int(species_to_process_df.loc[species_id, 'count'])
                log_entry = session.get(AnalyticsLog, species_id)
                if log_entry:
                    log_entry.detection_count = current_detection_count
                    log_entry.last_calculated_at = datetime.utcnow()
                else:
                    session.add(AnalyticsLog(species_id=int(species_id), detection_count=current_detection_count, last_calculated_at=datetime.utcnow()))
            
            session.commit()
            
        except Exception as e:
            logging.error(f"Failed to process species {species_id}: {e}", exc_info=True)
            session.rollback()

def run_pam_analytics(db_uri: str, force: bool, trends_only: bool, biotopes_only: bool):
    global DATABASE_URI, engine, Session
    DATABASE_URI = db_uri
    try:
        engine = create_engine(DATABASE_URI)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            logging.info("--- Starting Full PAM Analytics Calculation Cycle ---")
            if not trends_only and not biotopes_only:
                _populate_monitoring_periods(session)
            species_to_process_df = _get_species_to_process(session, force, trends_only, biotopes_only)
            _run_calculation_cycle(session, species_to_process_df, trends_only, biotopes_only)
            logging.info("--- PAM Analytics Calculation Cycle Completed Successfully ---")
    except SQLAlchemyError as e:
        logging.error(f"Database error during analytics calculation: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)


if __name__ == '__main__':
    import os
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    sys.path.insert(0, project_root)
    
    force_run = '--force' in sys.argv
    trends_only_run = '--trends-only' in sys.argv
    biotopes_only_run = '--biotopes-only' in sys.argv

    if sum([force_run, trends_only_run, biotopes_only_run]) > 1:
        logging.error("Помилка: Прапорці --force, --trends-only, та --biotopes-only є взаємовиключними. Використовуйте тільки один.")
        sys.exit(1)

    try:
        from app import create_app
        app = create_app()
        with app.app_context():
            pam_db_uri = app.config.get('PAM_DATABASE_URI')
            if not pam_db_uri:
                raise ValueError("PAM_DATABASE_URI is not set in the Flask config.")
            run_pam_analytics(pam_db_uri, force=force_run, trends_only=trends_only_run, biotopes_only=biotopes_only_run)
    except Exception as e:
        logging.error(f"Failed to run script: {e}", exc_info=True)