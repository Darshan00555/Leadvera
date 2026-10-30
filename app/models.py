from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)

from .db import Base
from .fields import LEAD_FIELDS, QUEUE_EXTRA


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


_INT = {"confidence_score", "lead_score", "times_revisited"}
_DATE = {"indian_incorporation_date", "us_formation_date", "email_verification_date",
         "date_first_discovered", "date_last_checked", "last_enrichment_date"}
_SHORT = {"lead_id", "verification_status", "priority", "cin_llpin", "us_state", "us_entity_number", "foreign_country",
          "us_llc_status", "outreach_status", "indian_company_status", "email_verification_status"}
_INDEXED = {"lead_id", "verification_status", "priority", "cin_llpin", "us_state", "us_entity_number",
            "outreach_status", "assigned_owner", "date_first_discovered", "norm_name", "domain"}


def _lead_columns():
    cols = {}
    for attr, _, _ in LEAD_FIELDS + QUEUE_EXTRA:
        if attr == "entity_pair_key":
            continue
        if attr in _INT:
            typ = Integer
        elif attr in _DATE:
            typ = Date
        elif attr in _SHORT:
            typ = String(64)
        elif attr.endswith(("_url", "_urls", "evidence", "description", "notes", "limitations", "signals",
                            "angle", "missing_evidence")):
            typ = Text
        else:
            typ = String(512)
        cols[attr] = Column(typ, index=attr in _INDEXED)
    return cols


Lead = type("Lead", (Base,), {
    "__tablename__": "leads",
    "__doc__": "One Indian-entity / US-LLC relationship. Verified leads and research-queue items share this "
               "table and are told apart by verification_status.",
    "id": Column(Integer, primary_key=True),
    "entity_pair_key": Column(String(256), unique=True, nullable=False, index=True),
    "norm_name": Column(String(256), index=True),
    "domain": Column(String(256), index=True),
    "first_run_id": Column(Integer, ForeignKey("runs.id"), nullable=True),
    "verified_at": Column(DateTime),
    "web_verified_at": Column(DateTime, index=True),  # queue item fully evidenced from public web pages (rules.web_evidence)
    "created_at": Column(DateTime, default=utcnow),
    "updated_at": Column(DateTime, default=utcnow, onupdate=utcnow),
    **_lead_columns(),
})


class LeadHistory(Base):
    """Field-level change log so historical verification and edits are never lost."""
    __tablename__ = "lead_history"
    id = Column(Integer, primary_key=True)
    lead_pk = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    field = Column(String(64))
    old_value = Column(Text)
    new_value = Column(Text)
    at = Column(DateTime, default=utcnow, index=True)


class Exclusion(Base):
    __tablename__ = "exclusions"
    id = Column(Integer, primary_key=True)
    date = Column(Date)
    candidate_name = Column(String(512))
    cin_llpin = Column(String(64), index=True)
    us_state = Column(String(8))
    us_entity_number = Column(String(64))
    domain = Column(String(256))
    type = Column(String(64), index=True)          # Disqualified | Possible Duplicate - Review | Duplicate
    reason = Column(Text)
    canonical_lead_id = Column(String(64))
    source_urls = Column(Text)
    payload = Column(JSON)                          # full candidate, so a reviewer can restore it
    resolved = Column(Boolean, default=False)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class PipelineEntry(Base):
    __tablename__ = "pipeline"
    id = Column(Integer, primary_key=True)
    lead_pk = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), unique=True)
    lead_owner = Column(String(256))
    outreach_date = Column(Date)
    channel = Column(String(64))
    last_contact = Column(Date)
    response = Column(Text)
    meeting_booked = Column(Boolean, default=False)
    opportunity_stage = Column(String(64), default="New", index=True)
    next_follow_up = Column(Date, index=True)
    est_mrr_usd = Column(Float)
    actual_outcome = Column(Text)
    notes = Column(Text)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    run_date = Column(Date, index=True)             # local (IST) business date
    trigger = Column(String(32))                    # schedule | manual
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(32), default="queued", index=True)  # queued|running|completed|failed|cancelled
    cancel_requested = Column(Boolean, default=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    heartbeat_at = Column(DateTime)
    stats = Column(JSON, default=dict)
    queries = Column(JSON, default=list)
    query_groups = Column(JSON, default=list)
    sources = Column(JSON, default=list)
    errors = Column(JSON, default=list)
    blocked_sources = Column(JSON, default=list)
    recommendations = Column(JSON, default=list)
    log = Column(JSON, default=list)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    web_searches = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    report_subject = Column(String(512))
    report_html = Column(Text)
    report_text = Column(Text)
    report_sent_at = Column(DateTime)
    email_report = Column(Boolean)                  # manual runs: email the report when finished (None = setting)
    targets = Column(JSON)                          # per-run focus: markets, us_states, indian_locations, industries...
    created_at = Column(DateTime, default=utcnow)


class Coverage(Base):
    __tablename__ = "coverage"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), index=True)
    date = Column(Date, index=True)
    query_group = Column(String(256), index=True)
    industry = Column(String(128))
    indian_state = Column(String(128))
    us_state = Column(String(8))
    market = Column(String(8))                      # country code of the overseas entity (US, GB, AU, DE, ...)
    pattern = Column(String(128))
    sources = Column(Text)
    queries_run = Column(Integer, default=0)
    candidates_yielded = Column(Integer, default=0)
    verified_yielded = Column(Integer, default=0)


class IndianCompany(Base):
    """Local copy of MCA / data.gov.in company master data, imported by an admin."""
    __tablename__ = "indian_companies"
    id = Column(Integer, primary_key=True)
    cin = Column(String(32), unique=True, index=True)
    name = Column(String(512))
    norm_name = Column(String(512), index=True)
    status = Column(String(64))
    state = Column(String(128))
    incorporation_date = Column(String(32))
    company_class = Column(String(64))
    activity = Column(String(512))
    source_url = Column(String(1024))
    imported_at = Column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    name = Column(String(256))
    role = Column(String(16), default="sales")       # admin | analyst | sales
    password_hash = Column(String(512), nullable=False)
    active = Column(Boolean, default=True)
    failed_logins = Column(Integer, default=0)
    locked_until = Column(DateTime)
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(128), primary_key=True)
    value = Column(JSON)
    secret = Column(Boolean, default=False)          # value is Fernet-encrypted text
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class EmailLog(Base):
    __tablename__ = "email_log"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    kind = Column(String(32))                        # daily_report | test | alert
    recipients = Column(Text)
    subject = Column(String(512))
    status = Column(String(16))                      # sent | failed
    error = Column(Text)
    attempts = Column(Integer, default=1)
    at = Column(DateTime, default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), index=True)
    target = Column(String(256))
    detail = Column(JSON)
    ip = Column(String(64))
    at = Column(DateTime, default=utcnow, index=True)


class Lock(Base):
    """Cross-process mutex (works on SQLite and Postgres): a row exists while the lock is held."""
    __tablename__ = "locks"
    name = Column(String(64), primary_key=True)
    owner = Column(String(128))
    acquired_at = Column(DateTime, default=utcnow)


class ReportSchedule(Base):
    __tablename__ = "report_marks"
    __table_args__ = (UniqueConstraint("kind", "local_date"),)
    id = Column(Integer, primary_key=True)
    kind = Column(String(32))                        # research_started | report_sent
    local_date = Column(Date)
    at = Column(DateTime, default=utcnow)
