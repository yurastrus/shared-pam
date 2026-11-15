# SPDX-License-Identifier: AGPL-3.0-only
"""Shared PAM analytics job for the admin page and cron."""

import logging
from concurrent.futures import Future
from threading import Thread

from flask import current_app
from sqlalchemy import text
from sqlalchemy.orm import Session

from .analytics_inputs import CHECKPOINT_TABLE, read_checkpoint, read_inputs, save_checkpoint, select_species
from .biotope_autoassign import _ensure_calculation_log
from .utils import get_pam_engine

SOURCE = "pam_analytics"
# PostgreSQL session lock in pam_db, shared by web workers and cron.
LOCK_KEY = 871014210
logger = logging.getLogger(__name__)
STATUS_QUERY = text("""
    SELECT status, last_calculated_at
    FROM pam_calculation_log WHERE source_name = :source
""")


def get_status():
    with get_pam_engine().begin() as conn:
        if not conn.scalar(text("SELECT to_regclass('pam_calculation_log')")):
            return {"status": "idle"}
        params = {"source": SOURCE}
        row = conn.execute(STATUS_QUERY, params).one_or_none()
        if row is None:
            return {"status": "idle"}
        status = row.status
        if status == "running" and not conn.scalar(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory' AND granted AND objsubid = 1
                    AND classid = 0 AND objid = :key
                    AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            )
        """), {"key": LOCK_KEY}):
            # The job may have finished during this poll.
            row = conn.execute(STATUS_QUERY, params).one()
            status = "failed" if row.status == "running" else row.status
        return {
            "status": status,
            "last_calculated_at": row.last_calculated_at.isoformat() if row.last_calculated_at else None,
        }


def _run(started=None, *, force=True):
    from .pam_analytics_calculator import _get_species_confidence_thresholds, run_pam_analytics

    with get_pam_engine().connect() as conn:
        acquired = conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
        )
        if not acquired:
            if started is not None:
                started.set_result(False)
            logger.info("PAM analytics already running")
            return
        try:
            # Calculation queries use other connections. Keep this session
            # alive while it holds the lock, even during a long calculation.
            conn.execute(text("SET idle_session_timeout = 0"))
            _ensure_calculation_log(conn)
            conn.execute(text("""
                INSERT INTO pam_calculation_log (source_name, status, started_at)
                VALUES (:source, 'running', NOW())
                ON CONFLICT (source_name) DO UPDATE SET
                    status = 'running', started_at = NOW(), error_message = NULL
            """), {"source": SOURCE})
            conn.commit()
            if started is not None:
                started.set_result(True)

            status = "failed"
            recalculated = False
            try:
                # Keep reads consistent without holding locks during the calculation.
                with conn.begin():
                    conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    conn.execute(text("SET LOCAL statement_timeout = '90s'"))
                    has_checkpoint_table = conn.scalar(
                        text("SELECT to_regclass(:table)"), {"table": CHECKPOINT_TABLE}
                    )
                    if has_checkpoint_table:
                        with Session(bind=conn) as session:
                            thresholds = _get_species_confidence_thresholds(session)
                            snapshot = read_inputs(conn, thresholds)
                        previous = read_checkpoint(conn)
                        selected = select_species(snapshot, previous, force=force)
                    elif force:
                        selected = thresholds = snapshot = None
                    else:
                        raise RuntimeError("PAM analytics checkpoint migration has not been applied")

                if selected or selected is None:
                    logger.info("Starting PAM analytics for %s species",
                                "all" if selected is None else len(selected))
                    run_pam_analytics(
                        current_app.config["PAM_DATABASE_URI"],
                        force=force, trends_only=False, biotopes_only=False,
                        selected_counts=selected, thresholds=thresholds,
                    )
                    recalculated = True
                    logger.info("PAM analytics recalculation completed")
                else:
                    logger.info("PAM analytics inputs unchanged; skipped recalculation")
                if snapshot is not None and snapshot != previous:
                    save_checkpoint(conn, snapshot)
                status = "completed"
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute(text("""
                    UPDATE pam_calculation_log
                    SET status = :status, last_calculated_at = CASE
                        WHEN :recalculated THEN NOW() ELSE last_calculated_at
                    END
                    WHERE source_name = :source
                """), {"source": SOURCE, "status": status, "recalculated": recalculated})
                conn.commit()
        finally:
            # A failed transaction must be cleared before unlocking. Never return
            # a session to the pool if its lock could not be released.
            try:
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                conn.execute(text("RESET idle_session_timeout"))
                conn.commit()
            except Exception:
                conn.invalidate()
                raise


def _run_in_thread(app, started):
    with app.app_context():
        try:
            _run(started)
        except Exception as error:
            app.logger.exception("PAM analytics job failed")
            if not started.done():
                started.set_exception(error)


def start():
    started = Future()
    Thread(
        target=_run_in_thread,
        args=(current_app._get_current_object(), started),
        name="pam-analytics",
        daemon=True,
    ).start()
    return started.result()


if __name__ == "__main__":
    from argparse import ArgumentParser

    from app import create_app

    parser = ArgumentParser(description="Refresh PAM analytics when inputs change.")
    parser.add_argument("--force", action="store_true", help="Recalculate all species.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with create_app().app_context():
        _run(force=args.force)
