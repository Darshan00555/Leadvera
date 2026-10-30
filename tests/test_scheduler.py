from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import appsettings, mailer, scheduler
from app.models import EmailLog, Run

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "_deliver", lambda s, msg, rcpts: sent.append((msg, rcpts)))
    return sent


def configure(db):
    appsettings.set_many(db, dict(email_recipients=["ops@example.com"], smtp_host="smtp.example.com",
                                  smtp_from_email="bot@example.com", smtp_password="secret-pw"))
    return appsettings.get_all(db, include_secrets=True)


def at(h, m, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=IST)


def test_research_queued_once_after_start_time(db):
    s = configure(db)
    assert scheduler.maybe_queue_research(db, s, at(8, 0)) is None
    assert scheduler.maybe_queue_research(db, s, at(8, 31)) is not None
    assert scheduler.maybe_queue_research(db, s, at(8, 45)) is None  # no second run the same day
    assert db.query(Run).count() == 1


def test_report_sent_at_11_with_workbook(db, outbox):
    s = configure(db)
    run = scheduler.maybe_queue_research(db, s, at(8, 31))
    run.status, run.stats, run.started_at = "completed", {"new_verified": 3, "total_verified": 3}, datetime(2026, 10, 28)
    db.commit()
    assert scheduler.maybe_send_daily_report(db, s, at(10, 59)) is None
    assert scheduler.maybe_send_daily_report(db, s, at(11, 0)) is not None
    assert scheduler.maybe_send_daily_report(db, s, at(11, 1)) is None  # exactly once
    assert len(outbox) == 1
    msg, rcpts = outbox[0]
    assert rcpts == ["ops@example.com"] and "3 new verified leads" in msg["Subject"]
    assert any(p.get_filename(" ").endswith(".xlsx") for p in msg.iter_attachments())


def test_waits_for_running_job_then_sends_interim_then_final(db, outbox):
    s = configure(db)
    run = scheduler.maybe_queue_research(db, s, at(8, 31))
    run.status = "running"
    db.commit()
    assert scheduler.maybe_send_daily_report(db, s, at(11, 30)) is None   # inside wait window
    assert scheduler.maybe_send_daily_report(db, s, at(13, 1)) is not None  # interim after 120 min
    assert "interim" in outbox[-1][0]["Subject"]
    run.status = "completed"
    db.commit()
    scheduler.maybe_send_daily_report(db, s, at(13, 30))
    assert len(outbox) == 2 and "interim" not in outbox[-1][0]["Subject"]
    scheduler.maybe_send_daily_report(db, s, at(13, 45))
    assert len(outbox) == 2


def test_no_second_report_for_earlier_run_after_final_report(db, outbox):
    s = configure(db)
    scheduled = scheduler.maybe_queue_research(db, s, at(8, 31))
    scheduled.status = "failed"
    manual = Run(run_date=scheduled.run_date, trigger="manual", status="completed")
    db.add(manual)
    db.commit()
    assert scheduler.maybe_send_daily_report(db, s, at(11, 0)) is manual
    scheduler.maybe_send_daily_report(db, s, at(11, 1))
    scheduler.maybe_send_daily_report(db, s, at(11, 2))
    assert len(outbox) == 1


def test_smtp_failure_logged_and_retried_later(db, monkeypatch):
    s = configure(db)
    run = scheduler.maybe_queue_research(db, s, at(8, 31))
    run.status = "completed"
    db.commit()
    import smtplib
    monkeypatch.setattr(mailer.time, "sleep", lambda *_: None)
    def boom(*a): raise smtplib.SMTPServerDisconnected("down")
    monkeypatch.setattr(mailer, "_deliver", boom)
    assert scheduler.maybe_send_daily_report(db, s, at(11, 0)) is None
    assert db.query(EmailLog).filter_by(status="failed").count() == 1
    monkeypatch.setattr(mailer, "_deliver", lambda *a: None)
    assert scheduler.maybe_send_daily_report(db, s, at(11, 1)) is not None


def test_report_waits_quietly_until_smtp_configured(db, outbox):
    s = appsettings.get_all(db, include_secrets=True)
    run = scheduler.maybe_queue_research(db, s, at(8, 31))
    run.status = "completed"
    db.commit()
    for minute in range(5):
        assert scheduler.maybe_send_daily_report(db, s, at(11, minute)) is None
    assert not run.errors and not outbox
    s = configure(db)
    assert scheduler.maybe_send_daily_report(db, s, at(11, 5)) is not None
    assert len(outbox) == 1


def test_disabled_schedule_does_nothing(db):
    s = configure(db)
    s["schedule_enabled"] = False
    assert scheduler.maybe_queue_research(db, s, at(9, 0)) is None


def test_secrets_encrypted_at_rest(db):
    configure(db)
    from app.models import Setting
    row = db.get(Setting, "smtp_password")
    assert row.secret and "secret-pw" not in str(row.value)
    assert appsettings.get(db, "smtp_password") == "secret-pw"
    assert appsettings.get_all(db)["smtp_password"] == "••••••••"


def test_report_lists_only_this_runs_leads(db):
    from datetime import timedelta
    from app import report
    from app.ingest import ingest_candidate
    from app.models import Lead, utcnow
    from .conftest import GOOD
    s = configure(db)
    ingest_candidate(db, GOOD)
    lead = db.query(Lead).one()
    lead.verified_at = utcnow()
    old = Run(run_date=datetime(2026, 10, 1).date(), status="completed", stats={},
              started_at=utcnow() - timedelta(days=3), finished_at=utcnow() - timedelta(days=3, hours=-1))
    db.add(old); db.commit()
    _, text, _ = report.build(db, old, s)
    assert "Testco" not in text and "Cumulative verified leads: 1" in text


def test_manual_run_report_emailed_when_finished_and_not_resent(db, outbox):
    s = configure(db)
    run = Run(run_date=at(9, 0).date(), trigger="manual", status="completed", stats={"new_verified": 0})
    db.add(run)
    db.commit()
    assert scheduler.email_finished_run(db, run, s) is True
    msg, _ = outbox[0]
    assert any(p.get_filename(" ").endswith(".xlsx") for p in msg.iter_attachments()) and run.report_sent_at
    assert scheduler.email_finished_run(db, run, s) is False  # once only
    assert scheduler.maybe_send_daily_report(db, s, at(11, 0)) is None and len(outbox) == 1


def test_run_report_respects_per_run_choice_and_schedule(db, outbox):
    s = configure(db)
    off = Run(run_date=at(9, 0).date(), trigger="manual", status="completed", email_report=False)
    sched = Run(run_date=at(9, 0).date(), trigger="schedule", status="completed")
    db.add_all([off, sched])
    db.commit()
    assert not scheduler.email_finished_run(db, off, s) and not scheduler.email_finished_run(db, sched, s)
    assert not outbox


def test_daily_lead_limit_counts_verified_and_web_verified(db):
    from types import SimpleNamespace

    from app.ingest import ingest_candidate
    from app.research.agent import Researcher
    from .conftest import GOOD
    s = appsettings.get_all(db)
    s["daily_target"] = 2
    fake = SimpleNamespace(db=db, settings=s)
    assert Researcher.leads_left(fake) == 2
    ingest_candidate(db, GOOD)                                                           # verified
    ingest_candidate(db, {**GOOD, "cin_llpin": "U72900KA2019PTC654321", "us_entity_number": "2",
                          "indian_legal_name": "Othertest Private Limited", "indian_website": "https://o.example",
                          "us_registry_url": "", "verification_status": "PARTIALLY VERIFIED"})  # web-verified
    assert Researcher.leads_left(fake) == 0


def test_run_lead_cap_stops_before_daily_limit(db):
    from types import SimpleNamespace

    from app.research.agent import Researcher
    from app.research.planner import clean_targets
    s = appsettings.get_all(db)
    t = clean_targets(markets=["US"], max_leads=30)
    assert t["max_leads"] == 30
    fake = SimpleNamespace(db=db, settings=s, run=SimpleNamespace(targets=t),
                           stats={"new_verified": 10, "web_verified": 18})
    assert Researcher.leads_left(fake) == 2
    fake.stats["web_verified"] = 20
    assert Researcher.leads_left(fake) == 0
