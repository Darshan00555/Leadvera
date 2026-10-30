"""Daily schedule: queue the research run, send the morning report, alert on failures.

All state lives in the database (report_marks, runs), so the worker is restart-safe and running two
worker replicas cannot double-send: the unique (kind, local_date) mark is the arbiter.
"""
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from . import appsettings, excel, mailer, report
from .models import EmailLog, ReportSchedule, Run, utcnow

log = logging.getLogger("scheduler")
FINISHED = ("completed", "failed", "cancelled")
INTERIM = " (interim - research still running)"


def local_now(settings):
    return datetime.now(ZoneInfo(settings["timezone"]))


def _hhmm(s):
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


def _mark(db, kind, day):
    try:
        db.add(ReportSchedule(kind=kind, local_date=day))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _has_mark(db, kind, day):
    return db.query(ReportSchedule).filter_by(kind=kind, local_date=day).first() is not None


def maybe_queue_research(db, settings, now):
    if not settings["schedule_enabled"] or now.weekday() not in settings["run_days"]:
        return None
    if now.time() < _hhmm(settings["research_start_time"]):
        return None
    day = now.date()
    if _has_mark(db, "research_queued", day) or not _mark(db, "research_queued", day):
        return None
    run = Run(run_date=day, trigger="schedule", status="queued")
    db.add(run)
    db.commit()
    log.info("queued scheduled run %s for %s", run.id, day)
    return run


def send_run_report(db, run, settings, kind="daily_report", interim=False):
    subject, text, html_body = report.build(db, run, settings)
    if interim:
        subject += INTERIM
    run.report_subject, run.report_text, run.report_html = subject, text, html_body
    db.commit()
    attachments = []
    if settings.get("email_attach_workbook"):
        attachments.append((f"{appsettings.file_slug(settings)}_India_US_LLC_Leads_{run.run_date}.xlsx",
                            excel.build_workbook(db)))
    mailer.send(db, kind, settings["email_recipients"], subject, text, html_body,
                cc=settings.get("email_cc") or [], attachments=attachments, run_id=run.id)
    if not interim:
        run.report_sent_at = utcnow()
        db.commit()


def _failed_today(db, kind, now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return db.query(EmailLog).filter(EmailLog.kind == kind, EmailLog.status == "failed", EmailLog.at >= since).count()


def maybe_send_daily_report(db, settings, now):
    if not settings["schedule_enabled"] or now.time() < _hhmm(settings["report_time"]):
        return None
    day = now.date()
    if _has_mark(db, "report_sent", day):
        return _maybe_send_final(db, settings, day)
    run = db.query(Run).filter(Run.run_date == day).order_by(Run.id.desc()).first()
    if run is None:
        return None
    report_at = datetime.combine(day, _hhmm(settings["report_time"]), tzinfo=now.tzinfo)
    still_running = run.status not in FINISHED
    if still_running and now < report_at + timedelta(minutes=int(settings["report_wait_minutes"])):
        return None  # give the research a little longer
    if not mailer.smtp_configured(settings) or not settings.get("email_recipients"):
        return None  # sent once Settings -> Email is filled in; retrying every tick would only pile up errors
    if run.report_sent_at and not still_running:
        _mark(db, "report_sent", day)  # already emailed when the (manual) run finished - don't send it twice
        return None
    if _failed_today(db, "daily_report", now) >= 5:
        return None  # stop hammering a broken SMTP config; failures are visible on the Email log page
    try:
        send_run_report(db, run, settings, interim=still_running)
    except mailer.MailError as e:
        log.warning("daily report failed: %s", e)
        run.errors = (run.errors or []) + [f"report email failed: {e}"]
        db.commit()
        return None
    _mark(db, "report_sent", day)
    return run


def _maybe_send_final(db, settings, day):
    """If the 11:00 email went out as interim, send the final report once that run finishes."""
    run = db.query(Run).filter(Run.run_date == day, Run.report_sent_at.is_(None),
                               Run.report_subject.like(f"%{INTERIM}")).order_by(Run.id.desc()).first()
    if not run or run.status not in FINISHED:
        return None
    if not _mark(db, "final_report_sent", day):
        return None
    try:
        send_run_report(db, run, settings)
    except mailer.MailError as e:
        run.errors = (run.errors or []) + [f"final report email failed: {e}"]
        db.commit()
    return run


def email_finished_run(db, run, settings):
    """Manual / test runs: email the report (with the workbook export) as soon as the run ends.
    Scheduled runs are covered by the daily report instead."""
    want = run.email_report if run.email_report is not None else settings.get("email_after_run")
    if run.trigger != "manual" or not want or run.report_sent_at:
        return False
    if not mailer.smtp_configured(settings) or not settings.get("email_recipients"):
        return False
    try:
        send_run_report(db, run, settings, kind="run_report")
        return True
    except mailer.MailError as e:
        log.warning("run report email failed: %s", e)
        run.errors = (run.errors or []) + [f"run report email failed: {e}"]
        db.commit()
        return False


def alert_failure(db, run, settings):
    if not settings.get("email_alert_on_failure") or not settings.get("email_recipients"):
        return
    subj = f'{settings["email_subject_prefix"]} ALERT: research run {run.id} {run.status}'
    body = f"Run {run.id} ({run.run_date}) ended with status {run.status}.\n\nErrors:\n" + \
        "\n".join(f"- {e}" for e in (run.errors or [])[-10:])
    try:
        mailer.send(db, "alert", settings["email_recipients"], subj, body, f"<pre>{body}</pre>", run_id=run.id)
    except mailer.MailError as e:
        log.warning("alert email failed: %s", e)


def tick(db):
    settings = appsettings.get_all(db, include_secrets=True)
    now = local_now(settings)
    maybe_queue_research(db, settings, now)
    maybe_send_daily_report(db, settings, now)
