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
    mean_rai = Column(Numeric(12, 4), nullable=False)
    lower_ci = Column(Numeric(12, 4), nullable=False)
    upper_ci = Column(Numeric(12, 4), nullable=False)
    calculated_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('species_id', 'year', name='_species_year_uc'),)

class SpeciesBiotopeYearlyActivity(Base):
    __tablename__ = 'species_biotope_yearly_activity'
    id = Column(Integer, primary_key=True, autoincrement=True)
    species_id = Column(Integer, nullable=False, index=True)
    biotope_id = Column(Integer, nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    detection_count = Column(Integer, nullable=False, default=0)
    effort_hours = Column(Numeric(10, 4), nullable=False, default=0.0)
    __table_args__ = (UniqueConstraint('species_id', 'year', 'biotope_id', name='_species_year_biotope_uc'),)


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

def _calculate_and_save_biotope_activity(session: SessionType, species_ids: list, biotope_effort_df: pd.DataFrame, confidence_thresholds: defaultdict):
    """
    Розраховує та зберігає річну активність видів по біотопах,
    використовуючи індивідуальні пороги достовірності та дані про зусилля моніторингу.
    """
    if not species_ids: return
    if biotope_effort_df.empty:
        logging.warning("Biotope effort map is empty, cannot calculate biotope activity.")
        return
    logging.info(f"Calculating biotope activity for {len(species_ids)} species...")
    session.query(SpeciesBiotopeYearlyActivity).filter(SpeciesBiotopeYearlyActivity.species_id.in_(species_ids)).delete(synchronize_session=False)

    case_conditions = [f"WHEN d.species_id = {int(sid)} THEN d.confidence >= {float(thr)}" for sid, thr in confidence_thresholds.items() if sid in species_ids]
    
    if case_conditions:
        default_threshold = confidence_thresholds.default_factory()
        case_statement = "CASE\n" + "\n".join(case_conditions) + f"\nELSE d.confidence >= {default_threshold}\nEND"
        where_confidence_clause = f"({case_statement})"
    else:
        default_threshold = confidence_thresholds.default_factory()
        where_confidence_clause = f"d.confidence >= {default_threshold}"

    sql_query = text(f"""
        SELECT d.species_id, lb.biotope_id, EXTRACT(YEAR FROM r.datetime_start) as year, COUNT(d.detection_id) as detection_count
        FROM detections d
        JOIN recordings r ON d.recording_id = r.recording_id
        JOIN location_biotopes lb ON r.location_id = lb.location_id
        WHERE d.species_id IN :species_ids AND {where_confidence_clause}
        GROUP BY d.species_id, lb.biotope_id, EXTRACT(YEAR FROM r.datetime_start)
    """)
    params = {'species_ids': tuple(species_ids)}
    
    try:
        detections_df = pd.read_sql(sql_query, session.bind, params=params)
        detections_df.dropna(subset=['year'], inplace=True)
        if detections_df.empty:
            logging.info("No biotope activity data found for the given species after applying thresholds.")
            return
            
        detections_df['year'] = detections_df['year'].astype(int)
        merged_df = pd.merge(biotope_effort_df, detections_df, on=['biotope_id', 'year'], how='left')
        
        # --- ПОЧАТОК ВИПРАВЛЕННЯ ---
        # Виправляємо FutureWarning, присвоюючи результат назад
        merged_df['detection_count'] = merged_df['detection_count'].fillna(0)
        merged_df['species_id'] = merged_df['species_id'].fillna(-1)
        # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---

        final_df = merged_df[merged_df['species_id'].isin(species_ids)].copy()
        
        if final_df.empty:
            logging.info("Data for selected species is empty after merging with effort. Nothing to save.")
            return
            
        final_df = final_df.astype({'species_id': 'int64', 'biotope_id': 'int64', 'year': 'int64', 'detection_count': 'int64', 'effort_hours': 'float64'})
        session.bulk_insert_mappings(SpeciesBiotopeYearlyActivity, final_df.to_dict(orient='records'))
        logging.info(f"Saved {len(final_df)} records to species_biotope_yearly_activity.")
    except Exception as e:
        logging.error(f"Failed during biotope activity calculation: {e}", exc_info=True)
        raise

def _calculate_and_save_trends(session: SessionType, intermediate_df: pd.DataFrame, monitoring_periods: pd.DataFrame, species_id: int):
    if intermediate_df.empty: return
    try:
        species_period = monitoring_periods.loc[species_id]
        season_df = intermediate_df[(intermediate_df['month'] >= species_period['start_month']) & (intermediate_df['month'] <= species_period['end_month'])]
    except KeyError:
        logging.warning(f"No monitoring period found for species {species_id}. Using full year.")
        season_df = intermediate_df

    yearly_summary = season_df.groupby('year').agg(total_detections=('detection_count', 'sum'), total_effort=('effort_hours', 'sum')).reset_index()
    valid_years_summary = yearly_summary[yearly_summary['total_detections'] >= MIN_DETECTIONS_PER_YEAR]
    
    if valid_years_summary.empty:
        logging.warning(f"Species {species_id} does not have enough detections in any year to calculate trends.")
        return
        
    session.query(SpeciesYearlyTrends).filter(SpeciesYearlyTrends.species_id == species_id).delete(synchronize_session=False)
    
    trends_to_save = []
    for _, row in valid_years_summary.iterrows():
        year = row['year']
        year_df = season_df[season_df['year'] == year]
        
        if year_df['effort_hours'].sum() == 0: continue
        
        rai_values = (year_df['detection_count'] / year_df['effort_hours']).replace([np.inf, -np.inf], np.nan).dropna().values
        if len(rai_values) == 0: continue
        
        bootstrap_means = [np.mean(np.random.choice(rai_values, size=len(rai_values), replace=True)) for _ in range(BOOTSTRAP_ITERATIONS)]
        
        mean_rai = float(np.mean(bootstrap_means))
        
        # --- ПОЧАТОК ВИПРАВЛЕННЯ ---
        # Виправляємо DeprecationWarning, витягуючи перший елемент [0]
        lower_ci = float(np.percentile(bootstrap_means, [2.5])[0])
        upper_ci = float(np.percentile(bootstrap_means, [97.5])[0])
        # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---

        trends_to_save.append(SpeciesYearlyTrends(
            species_id=species_id,
            year=int(year),
            mean_rai=mean_rai,
            lower_ci=lower_ci,
            upper_ci=upper_ci
        ))
        
    if trends_to_save:
        session.bulk_save_objects(trends_to_save)
        logging.info(f"Saved {len(trends_to_save)} yearly trend records for species {species_id}.")

def _run_calculation_cycle(session: SessionType, species_to_process_df: pd.DataFrame, trends_only: bool, biotopes_only: bool):
    """Головний цикл, що керує розрахунками залежно від режиму запуску."""
    if species_to_process_df.empty:
        logging.info("No species to process. Exiting.")
        return

    confidence_thresholds = _get_species_confidence_thresholds(session)
    biotope_effort_df = _precalculate_biotope_effort_map(session)

    if biotopes_only:
        logging.info("--- Running in --biotopes-only mode ---")
        all_species_ids = species_to_process_df.index.tolist()
        try:
            if all_species_ids:
                _calculate_and_save_biotope_activity(session, all_species_ids, biotope_effort_df, confidence_thresholds)
                session.commit()
            logging.info("Biotope activity calculation complete.")
        except Exception:
            session.rollback()
            logging.error("Biotope activity calculation failed.", exc_info=True)
        return
    
    effort_df = _precalculate_effort_map(session)
    if effort_df.empty and not trends_only: return
    monitoring_periods = pd.read_sql("SELECT * FROM species_monitoring_periods", session.bind).set_index('species_id')
    
    all_species_ids = species_to_process_df.index.tolist()
    total_species = len(all_species_ids)

    if not trends_only and all_species_ids:
        try:
            _calculate_and_save_biotope_activity(session, all_species_ids, biotope_effort_df, confidence_thresholds)
            session.commit()
        except Exception:
            session.rollback()
            logging.error("Could not complete biotope activity pre-calculation. Skipping this step.")

    for i, species_id in enumerate(all_species_ids):
        logging.info(f"--- Processing species {species_id} ({i+1}/{total_species}) ---")
        try:
            intermediate_df = pd.DataFrame()
            if trends_only:
                logging.info(f"Loading existing intermediate data for species {species_id}.")
                sql_intermediate = text("SELECT * FROM analysis_intermediate WHERE species_id = :sid")
                intermediate_df = pd.read_sql(sql_intermediate, session.bind, params={'sid': species_id})
                if intermediate_df.empty:
                    logging.warning(f"No intermediate data found for species {species_id}. Skipping.")
                    continue
            else:
                session.query(AnalysisIntermediate).filter(AnalysisIntermediate.species_id == species_id).delete(synchronize_session=False)
                # Застосовуємо індивідуальний поріг для вибірки даних
                confidence_threshold = confidence_thresholds[species_id]
                logging.info(f"Using confidence threshold {confidence_threshold:.3f} for species {species_id}")
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
                species_df['year'] = species_df['datetime_start'].dt.year
                species_df['month'] = species_df['datetime_start'].dt.month
                species_df['day'] = species_df['datetime_start'].dt.day
                species_df['month_part'] = pd.cut(species_df['day'], bins=[0, 10, 20, 31], labels=[1, 2, 3], right=True).astype(int)
                species_df['day_part'] = (species_df['datetime_start'].dt.hour // 6) + 1
                grouping_keys = ['year', 'location_id', 'month', 'month_part', 'day_part']
                detections_agg = species_df.groupby(grouping_keys).size().reset_index(name='detection_count')
                intermediate_df = pd.merge(effort_df, detections_agg, on=grouping_keys, how='left').fillna(0)
                intermediate_df['species_id'] = species_id
                if not intermediate_df.empty:
                    session.bulk_insert_mappings(AnalysisIntermediate, intermediate_df.to_dict(orient='records'))
            
            _calculate_and_save_trends(session, intermediate_df, monitoring_periods, species_id)
            
            if not trends_only:
                current_detection_count = int(species_to_process_df.loc[species_id, 'count'])
                log_entry = session.get(AnalyticsLog, species_id)
                if log_entry:
                    log_entry.detection_count = current_detection_count
                    log_entry.last_calculated_at = datetime.utcnow()
                else:
                    session.add(AnalyticsLog(species_id=int(species_id), detection_count=current_detection_count, last_calculated_at=datetime.utcnow()))
            session.commit()
            logging.info(f"Successfully processed data for species {species_id}.")
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