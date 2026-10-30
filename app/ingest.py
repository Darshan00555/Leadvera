"""Upsert research candidates into the database (workflow steps 8-11).

Each candidate is committed on its own, so a crash mid-run keeps everything already processed
(checkpointing), and the outcome is returned to the research agent as feedback.
"""
from datetime import date, datetime

from sqlalchemy import or_

from . import rules as R
from .fields import PROTECTED, QUEUE_STATUSES, RESEARCH_FIELDS, URL_LIST_FIELDS
from .models import Exclusion, Lead, LeadHistory, PipelineEntry, utcnow

EMPTY_STATS = dict(candidates=0, indian_verified=0, us_verified=0, relationships_verified=0,
                   new_verified=0, A=0, B=0, C=0, partial=0, unverified=0, duplicates=0, disqualified=0,
                   with_decision_maker=0, with_contact=0, updated=0, promoted=0, web_verified=0)

_DATE_FIELDS = {"indian_incorporation_date", "us_formation_date", "email_verification_date"}


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _parse_date(v):
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def clean(raw):
    """Keep only researcher-writable fields, normalised."""
    rec = {}
    for k in RESEARCH_FIELDS:
        v = raw.get(k)
        if _blank(v) or v == []:
            continue
        if k in URL_LIST_FIELDS:
            v = "\n".join(R.split_urls(v))
        elif k in _DATE_FIELDS:
            v = _parse_date(v)
            if v is None:
                continue
        elif isinstance(v, str):
            v = v.strip()
        rec[k] = v
    country = R.country_of(rec)
    rec["foreign_country"] = country
    if rec.get("us_state"):
        rec["us_state"] = rec["us_state"].upper()[:2] if country == "US" else rec["us_state"].upper()[:8]
    if rec.get("cin_llpin"):
        rec["cin_llpin"] = rec["cin_llpin"].upper().replace(" ", "")
    if rec.get("verification_status"):
        rec["verification_status"] = rec["verification_status"].upper()
    return rec


def _as_dict(lead):
    return {c.name: getattr(lead, c.name) for c in Lead.__table__.columns}


def _soft_match(db, rec, exclude_pk=None):
    conds = []
    if rec.get("cin_llpin"):
        conds.append(("same CIN/LLPIN", Lead.cin_llpin == rec["cin_llpin"]))
    country = R.country_of(rec)
    if country == "US" and rec.get("us_entity_number") and rec.get("us_state"):
        conds.append(("same US entity", (Lead.us_state == rec["us_state"]) &
                       (Lead.us_entity_number == rec["us_entity_number"])))
    elif country != "US" and rec.get("us_entity_number"):
        conds.append(("same foreign entity", (Lead.foreign_country == country) &
                       (Lead.us_entity_number == rec["us_entity_number"])))
    dom = R.domain_of(rec.get("indian_website"))
    if dom:
        conds.append(("same website domain", Lead.domain == dom))
    nn = R.norm_name(rec.get("indian_legal_name"))
    if nn:
        conds.append(("same normalised legal name", Lead.norm_name == nn))
    for label, cond in conds:
        q = db.query(Lead).filter(cond, Lead.verification_status != "DISQUALIFIED")
        if exclude_pk:
            q = q.filter(Lead.id != exclude_pk)
        hit = q.first()
        if hit:
            return label, hit
    return None, None


def _exclude(db, rec, typ, reason, run_id, canonical=""):
    db.add(Exclusion(
        date=date.today(), candidate_name=rec.get("indian_legal_name") or rec.get("indian_brand_name")
        or rec.get("us_llc_legal_name"), cin_llpin=rec.get("cin_llpin"), us_state=rec.get("us_state"),
        us_entity_number=rec.get("us_entity_number"), domain=R.domain_of(rec.get("indian_website")),
        type=typ, reason=reason, canonical_lead_id=canonical, source_urls=rec.get("source_urls"),
        payload={k: (v.isoformat() if isinstance(v, date) else v) for k, v in rec.items()}, run_id=run_id))


def _apply_scoring(rec, status, problems):
    pts, parts = R.score(rec, status)
    rec["verification_status"] = status
    rec["lead_score"], rec["priority"] = pts, R.priority(pts)
    rec["confidence_score"] = R.confidence(rec, status)
    rec["entity_pair_key"] = R.entity_pair_key(rec)
    rec["norm_name"] = R.norm_name(rec.get("indian_legal_name"))
    rec["domain"] = R.domain_of(rec.get("indian_website"))
    if status in QUEUE_STATUSES:
        rec["missing_evidence"] = "; ".join(problems)
        rec["next_research_step"] = R.next_research_step(problems)
    else:
        rec["missing_evidence"] = rec["next_research_step"] = None
    return parts


def _count_new_verified(stats, rec):
    stats["new_verified"] += 1
    stats[rec["priority"]] += 1
    stats["with_decision_maker"] += bool(rec.get("decision_maker_name") or rec.get("founder_ceo_name"))
    stats["with_contact"] += bool(rec.get("business_email") or rec.get("business_phone"))


def _update_web_tier(db, lead, rec, stats):
    """Flag / unflag a queue item as web-verified. Returns True when it is web-verified now."""
    ok = lead.verification_status in QUEUE_STATUSES and R.web_evidence(rec)[0]
    if ok and not lead.web_verified_at:
        lead.web_verified_at = utcnow()
        _ensure_pipeline(db, lead)
        stats["web_verified"] = stats.get("web_verified", 0) + 1
    elif not ok and lead.web_verified_at and lead.verification_status != "VERIFIED":
        lead.web_verified_at = None
    return ok


def _ensure_pipeline(db, lead):
    if not db.query(PipelineEntry).filter_by(lead_pk=lead.id).first():
        db.add(PipelineEntry(lead_pk=lead.id, lead_owner=lead.assigned_owner, opportunity_stage="New"))


def ingest_candidate(db, raw, run_id=None, stats=None, default_owner=""):
    """Process one candidate. Returns a dict the research agent receives as feedback."""
    stats = stats if stats is not None else dict(EMPTY_STATS)
    today = date.today()
    incoming = clean(raw)
    stats["candidates"] += 1

    existing = db.query(Lead).filter_by(entity_pair_key=R.entity_pair_key(incoming)).first()
    base = _as_dict(existing) if existing else {}
    merged = dict(base)
    for k, v in incoming.items():
        if k in PROTECTED and not _blank(base.get(k)):
            continue
        merged[k] = v
    merged["verification_status"] = incoming.get("verification_status", "UNVERIFIED")

    status, problems, flags = R.gate(merged)
    stats["indian_verified"] += bool(flags.get("indian"))
    stats["us_verified"] += bool(flags.get("us"))
    stats["relationships_verified"] += bool(flags.get("relationship")) and status == "VERIFIED"

    if status == "DISQUALIFIED":
        stats["disqualified"] += 1
        _exclude(db, incoming, "Disqualified", "; ".join(problems), run_id,
                 existing.lead_id if existing else "")
        db.commit()
        return {"outcome": "disqualified", "reasons": problems}

    if problems:
        merged["research_limitations"] = "; ".join(dict.fromkeys(filter(None, [
            incoming.get("research_limitations"), *problems])))
    merged["date_last_checked"] = merged["last_enrichment_date"] = today

    if existing is None:
        label, soft = _soft_match(db, {**incoming, "us_state": incoming.get("us_state")})
        if soft:
            stats["duplicates"] += 1
            _exclude(db, incoming, "Possible Duplicate - Review",
                     f"{label} as {soft.lead_id} ({soft.verification_status})", run_id, soft.lead_id)
            db.commit()
            return {"outcome": "possible_duplicate", "matches": soft.lead_id, "reason": label,
                    "hint": "If this is the same company, resubmit with the existing record's CIN and US entity "
                            "number so it updates in place."}
        _apply_scoring(merged, status, problems)
        merged.update(date_first_discovered=today, first_run_id=run_id,
                      outreach_status=merged.get("outreach_status") or "Not Contacted",
                      assigned_owner=merged.get("assigned_owner") or default_owner or None,
                      times_revisited=0)
        lead = Lead(**{k: v for k, v in merged.items() if hasattr(Lead, k) and k != "id"})
        db.add(lead)
        db.flush()
        lead.lead_id = f"IN-{R.country_of(merged)}-{lead.id:06d}"
        web = False
        if status == "VERIFIED":
            lead.verified_at = utcnow()
            _ensure_pipeline(db, lead)
            _count_new_verified(stats, merged)
        else:
            stats["partial" if status == "PARTIALLY VERIFIED" else "unverified"] += 1
            web = _update_web_tier(db, lead, merged, stats)
        db.commit()
        return {"outcome": "verified_lead_added" if status == "VERIFIED" else "queued_for_research",
                "lead_id": lead.lead_id, "status": status, "web_verified": web, "lead_score": lead.lead_score,
                "priority": lead.priority, "missing": problems}

    # Existing record.
    was = existing.verification_status
    if was == "VERIFIED" and status != "VERIFIED":
        note = f"{today}: re-check returned {status} ({'; '.join(problems)}) - review"
        _log(db, existing, "research_limitations", existing.research_limitations, note, run_id)
        existing.research_limitations = "; ".join(filter(None, [existing.research_limitations, note]))
        existing.date_last_checked = today
        stats["updated"] += 1
        db.commit()
        return {"outcome": "kept_verified_flagged_for_review", "lead_id": existing.lead_id,
                "reasons": problems}

    _apply_scoring(merged, status, problems)
    if was in QUEUE_STATUSES:
        merged["times_revisited"] = int(existing.times_revisited or 0) + 1
    changed = 0
    for k, v in merged.items():
        if k in ("id", "lead_id", "date_first_discovered", "created_at", "first_run_id") or not hasattr(Lead, k):
            continue
        if getattr(existing, k) != v:
            if k not in ("date_last_checked", "last_enrichment_date", "times_revisited"):
                _log(db, existing, k, getattr(existing, k), v, run_id)
                changed += 1
            setattr(existing, k, v)

    if was in QUEUE_STATUSES and status == "VERIFIED":
        existing.verified_at = utcnow()
        _ensure_pipeline(db, existing)
        stats["promoted"] += 1
        _count_new_verified(stats, merged)
        outcome = "promoted_to_verified"
    else:
        stats["duplicates"] += 1
        if changed:
            stats["updated"] += 1
        outcome = "updated_existing" if changed else "already_known_no_change"
    web = _update_web_tier(db, existing, merged, stats)
    db.commit()
    return {"outcome": outcome, "lead_id": existing.lead_id, "status": existing.verification_status,
            "web_verified": web, "fields_changed": changed, "missing": problems}


def refresh_web_tier(db):
    """Re-evaluate the web-verified flag for every queue item (e.g. after the rule was introduced)."""
    stats = {"web_verified": 0}
    for lead in db.query(Lead).filter(Lead.verification_status.in_(QUEUE_STATUSES)).all():
        _update_web_tier(db, lead, _as_dict(lead), stats)
    db.commit()
    return stats["web_verified"]


def _log(db, lead, field, old, new, run_id, user_id=None):
    db.add(LeadHistory(lead_pk=lead.id, run_id=run_id, user_id=user_id, field=field,
                       old_value=None if old is None else str(old), new_value=None if new is None else str(new)))


def known_lookup(db, name=None, cin=None, domain=None, limit=5):
    """Used by the agent to skip companies already in the database before spending research on them."""
    conds = []
    if cin:
        conds.append(Lead.cin_llpin == cin.strip().upper())
    if domain:
        conds.append(Lead.domain == R.domain_of(domain))
    if name:
        nn = R.norm_name(name)
        if nn:
            conds.append(Lead.norm_name == nn)
            conds.append(Lead.norm_name.like(f"{nn[:40]}%"))
    if not conds:
        return []
    rows = db.query(Lead).filter(or_(*conds)).limit(limit).all()
    return [{"lead_id": r.lead_id, "indian_legal_name": r.indian_legal_name, "us_llc_legal_name": r.us_llc_legal_name,
             "us_state": r.us_state, "status": r.verification_status, "missing": r.missing_evidence} for r in rows]
