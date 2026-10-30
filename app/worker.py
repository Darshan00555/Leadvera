"""Background worker: `python -m app.worker`.

Runs the scheduler tick every WORKER_POLL_SECONDS and executes queued research runs in a thread,
one at a time across all replicas (DB lock "research").
"""
import logging
import os
import signal
import socket
import threading
import time
import traceback
from datetime import timedelta

from . import appsettings, scheduler
from .bootstrap import bootstrap
from .config import Config
from .db import SessionLocal
from .models import Run, utcnow
from .security import acquire_lock, refresh_lock, release_lock

log = logging.getLogger("worker")
OWNER = f"{socket.gethostname()}:{os.getpid()}"
_stop = threading.Event()


def execute_run(run_id):
    from .research.claude_code import make_researcher
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        run.status, run.started_at, run.heartbeat_at = "running", utcnow(), utcnow()
        db.commit()
        try:
            stop = make_researcher(db, run).execute()
            run.status = "cancelled" if stop == "cancelled by user" else "completed"
        except Exception as e:
            db.rollback()
            run = db.get(Run, run_id)
            run.status = "failed"
            run.errors = (run.errors or []) + [f"{type(e).__name__}: {e}"]
            run.log = (run.log or []) + [traceback.format_exc()[-3000:]]
            run.stats = dict(run.stats or {}, stop_reason=f"failed: {type(e).__name__}")
        run.finished_at = utcnow()
        db.commit()
        settings = appsettings.get_all(db, include_secrets=True)
        if run.status == "failed":
            scheduler.alert_failure(db, run, settings)
        else:
            scheduler.email_finished_run(db, run, settings)
    finally:
        db.close()


def _research_loop():
    while not _stop.is_set():
        db = SessionLocal()
        try:
            run = db.query(Run).filter_by(status="queued").order_by(Run.id).first()
            if run and acquire_lock(db, "research", OWNER, stale_after=timedelta(minutes=30)):
                stop_hb = threading.Event()

                def heartbeat():
                    hb = SessionLocal()
                    try:
                        while not stop_hb.wait(60):
                            refresh_lock(hb, "research")
                    finally:
                        hb.close()
                t = threading.Thread(target=heartbeat, daemon=True)
                t.start()
                try:
                    log.info("starting run %s", run.id)
                    execute_run(run.id)
                finally:
                    stop_hb.set()
                    release_lock(db, "research", OWNER)
        except Exception:
            log.exception("research loop error")
            db.rollback()
        finally:
            db.close()
        _stop.wait(Config.WORKER_POLL_SECONDS)


def recover_orphans():
    """Runs left 'running' by a crashed worker are marked failed so the day's report is not blocked."""
    db = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(minutes=30)
        for r in db.query(Run).filter(Run.status == "running").all():
            if not r.heartbeat_at or r.heartbeat_at < cutoff:
                r.status, r.finished_at = "failed", utcnow()
                r.errors = (r.errors or []) + ["worker stopped while this run was in progress"]
                r.stats = dict(r.stats or {}, stop_reason="worker interrupted")
        db.commit()
    finally:
        db.close()


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bootstrap()
    recover_orphans()
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    threading.Thread(target=_research_loop, daemon=True, name="research").start()
    log.info("worker %s started", OWNER)
    while not _stop.is_set():
        db = SessionLocal()
        try:
            scheduler.tick(db)
        except Exception:
            log.exception("scheduler tick failed")
            db.rollback()
        finally:
            db.close()
        _stop.wait(Config.WORKER_POLL_SECONDS)
    log.info("worker stopping")


if __name__ == "__main__":
    main()
