"""FastAPI application: `uvicorn app.web.main:app`."""
import csv
import html
import os
from contextlib import asynccontextmanager
import io
import logging
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, text
from starlette.middleware.sessions import SessionMiddleware

from .. import appsettings, excel, mailer, scheduler
from ..bootstrap import bootstrap
from ..config import Config
from ..db import get_db
from ..fields import EXCEL_FIELDS, LABEL, LEAD_FIELDS, OUTREACH_STATUSES, PIPELINE_STAGES, PROTECTED, QUEUE_STATUSES
from ..models import (AuditLog, Coverage, EmailLog, Exclusion, IndianCompany, Lead, LeadHistory,
                      PipelineEntry, Run, User, utcnow)
from ..research import planner
from ..security import hash_password, password_problems, verify_password
from .core import (HERE, LoginRequired, audit, check_csrf, current_user, flash, redirect, render, require)

log = logging.getLogger("web")
PAGE = 50

@asynccontextmanager
async def lifespan(_app):
    bootstrap()
    yield


app = FastAPI(title="Leadvera", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=Config.secret_key(), session_cookie="af_session",
                   max_age=Config.SESSION_HOURS * 3600, same_site="lax", https_only=Config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; "
        "frame-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'self'")
    if Config.COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc):
    return redirect("/login", next=request.url.path)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/healthz")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width, initial-scale=1'>"
                        f"<title>Error {exc.status_code}</title><link rel=stylesheet href=/static/app.css>"
                        f"<script src=/static/theme.js></script>"
                        f"<div class='errorpage'><div class='errorcode'>{exc.status_code}</div>"
                        f"<p>{html.escape(str(exc.detail))}</p>"
                        f"<a class='btn primary' href='/'>Back to dashboard</a></div>", status_code=exc.status_code)


@app.get("/healthz")
def healthz(db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    last = db.query(Run).order_by(Run.id.desc()).first()
    return {"ok": True, "last_run": last.status if last else None}


# ---------------------------------------------------------------- auth
@app.get("/login")
def login_form(request: Request, db=Depends(get_db), next: str = "/"):
    if current_user(request, db):
        return redirect("/")
    return render(request, db, "login.html", next=next)


@app.post("/login")
async def login(request: Request, db=Depends(get_db)):
    form = await check_csrf(request)
    email = str(form.get("email", "")).strip().lower()
    nxt = str(form.get("next") or "/")
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    u = db.query(User).filter_by(email=email).first()
    if u and u.locked_until and u.locked_until > utcnow():
        flash(request, "Too many failed attempts. Try again in 15 minutes.", "error")
        return redirect("/login", next=nxt)
    if not u or not u.active or not verify_password(str(form.get("password", "")), u.password_hash):
        if u:
            u.failed_logins = (u.failed_logins or 0) + 1
            if u.failed_logins >= 5:
                u.locked_until, u.failed_logins = utcnow() + timedelta(minutes=15), 0
            db.commit()
        db.add(AuditLog(action="login_failed", target=email, ip=request.client.host if request.client else None))
        db.commit()
        flash(request, "Invalid email or password.", "error")
        return redirect("/login", next=nxt)
    request.session.clear()
    request.session.update(uid=u.id, iat=datetime.now().timestamp())
    u.failed_logins, u.last_login_at = 0, utcnow()
    db.commit()
    request.state.user = u
    audit(db, request, "login", u.email)
    return redirect(nxt)


@app.post("/logout")
async def logout(request: Request, db=Depends(get_db)):
    await check_csrf(request)
    request.session.clear()
    return redirect("/login")


@app.get("/account")
def account(request: Request, db=Depends(get_db)):
    require(request, db)
    return render(request, db, "account.html")


@app.post("/account/password")
async def change_password(request: Request, db=Depends(get_db)):
    u = require(request, db)
    form = await check_csrf(request)
    if not verify_password(str(form.get("current", "")), u.password_hash):
        flash(request, "Current password is wrong.", "error")
        return redirect("/account")
    new = str(form.get("new", ""))
    if probs := password_problems(new):
        flash(request, "New password needs " + ", ".join(probs) + ".", "error")
        return redirect("/account")
    u.password_hash = hash_password(new)
    db.commit()
    audit(db, request, "password_changed", u.email)
    flash(request, "Password updated.")
    return redirect("/account")


# ---------------------------------------------------------------- dashboard
@app.get("/")
def dashboard(request: Request, db=Depends(get_db)):
    require(request, db)
    s = appsettings.get_all(db)
    verified = db.query(Lead).filter_by(verification_status="VERIFIED")
    by_pri = dict(verified.with_entities(Lead.priority, func.count()).group_by(Lead.priority).all())
    queued = db.query(Lead).filter(Lead.verification_status.in_(QUEUE_STATUSES))
    web = queued.filter(Lead.web_verified_at != None)  # noqa: E711
    web_total = web.count()
    queue = queued.count() - web_total
    runs = db.query(Run).order_by(Run.id.desc()).limit(14).all()
    last = runs[0] if runs else None
    today_new = db.query(Lead).filter(Lead.verification_status == "VERIFIED",
                                      Lead.date_first_discovered == date.today()).count()
    stages = dict(db.query(PipelineEntry.opportunity_stage, func.count()).group_by(PipelineEntry.opportunity_stage).all())
    recent = verified.order_by(Lead.verified_at.desc()).limit(10).all()
    recent_web = web.order_by(Lead.web_verified_at.desc()).limit(10).all()
    followups = (db.query(PipelineEntry, Lead).join(Lead, Lead.id == PipelineEntry.lead_pk)
                 .filter(PipelineEntry.next_follow_up != None, PipelineEntry.next_follow_up <= date.today() + timedelta(days=7))  # noqa: E711
                 .order_by(PipelineEntry.next_follow_up).limit(8).all())
    now = scheduler.local_now(appsettings.get_all(db, include_secrets=False))
    chart = [{"label": r.run_date.strftime("%d %b") if r.run_date else str(r.id),
              "value": (r.stats or {}).get("new_verified", 0), "status": r.status, "id": r.id}
             for r in reversed(runs)]
    setup = {
        "smtp": bool(s.get("smtp_host") and s.get("smtp_from_email")),
        "recipients": bool(s.get("email_recipients")),
        "api_key": bool(Config.ANTHROPIC_API_KEY or s.get("anthropic_api_key")) if s.get("research_backend") == "api"
        else bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or s.get("claude_code_oauth_token")),
        "backend": s.get("research_backend"),
        "india_data": db.query(IndianCompany.id).first() is not None,
    }
    return render(request, db, "dashboard.html", bars=bar_chart(chart), total=verified.count(), by_pri=by_pri, queue=queue, last=last,
                  today_new=today_new, target=s["daily_target"], stages=stages, recent=recent, followups=followups,
                  chart=chart, settings=s, now=now, setup=setup, web_total=web_total, recent_web=recent_web)


def bar_chart(points, width=640, height=190, pad_l=34, pad_b=26, pad_t=18):
    """Geometry for a single-series column chart: <=24px bars, 4px rounded tops, square at the baseline."""
    top = max([p["value"] for p in points] + [1])
    step = 10 ** max(0, len(str(int(top))) - 1)
    nice = -(-top // step) * step if top > 5 else 5
    plot_w, plot_h = width - pad_l - 8, height - pad_b - pad_t
    base = pad_t + plot_h
    slot = plot_w / max(1, len(points))
    bw = min(24.0, slot * 0.6)
    bars = []
    for i, p in enumerate(points):
        x = pad_l + i * slot + (slot - bw) / 2
        h = plot_h * p["value"] / nice
        y = base - h
        r = min(4.0, h, bw / 2)
        path = (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bw - r:.1f} "
                f"Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{base:.1f} Z") if h > 0 else ""
        bars.append(dict(p, x=x, y=y, w=bw, cx=x + bw / 2, path=path, slot_x=pad_l + i * slot, slot_w=slot,
                         show_label=(i == len(points) - 1) or p["value"] == top and top > 0))
    grid = [dict(y=base - plot_h * f, label=f"{nice * f:g}") for f in (0, 0.5, 1)]
    return dict(width=width, height=height, base=base, pad_l=pad_l, pad_t=pad_t, bars=bars, grid=grid)


# ---------------------------------------------------------------- leads
def _lead_query(db, status, q, priority, us_state, owner, outreach, since, market=""):
    qry = db.query(Lead)
    if market == "US":
        qry = qry.filter(or_(Lead.foreign_country == None, Lead.foreign_country == "US"))  # noqa: E711
    elif market:
        qry = qry.filter(Lead.foreign_country == market.upper())
    if status == "queue":
        qry = qry.filter(Lead.verification_status.in_(QUEUE_STATUSES), Lead.web_verified_at == None)  # noqa: E711
    elif status == "web":
        qry = qry.filter(Lead.verification_status.in_(QUEUE_STATUSES), Lead.web_verified_at != None)  # noqa: E711
    elif status:
        qry = qry.filter(Lead.verification_status == status)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(Lead.indian_legal_name.ilike(like), Lead.indian_brand_name.ilike(like),
                             Lead.us_llc_legal_name.ilike(like), Lead.cin_llpin.ilike(like), Lead.lead_id.ilike(like),
                             Lead.decision_maker_name.ilike(like), Lead.founder_ceo_name.ilike(like),
                             Lead.industry.ilike(like), Lead.domain.ilike(like)))
    if priority:
        qry = qry.filter(Lead.priority == priority)
    if us_state:
        qry = qry.filter(Lead.us_state == us_state.upper())
    if owner:
        qry = qry.filter(Lead.assigned_owner == owner)
    if outreach:
        qry = qry.filter(Lead.outreach_status == outreach)
    if since:
        try:
            qry = qry.filter(Lead.date_first_discovered >= date.fromisoformat(since))
        except ValueError:
            pass
    return qry


@app.get("/leads")
def leads(request: Request, db=Depends(get_db), status: str = "VERIFIED", q: str = "", priority: str = "",
          us_state: str = "", owner: str = "", outreach: str = "", since: str = "", sort: str = "score",
          market: str = "", page: int = 1):
    require(request, db)
    qry = _lead_query(db, status, q, priority, us_state, owner, outreach, since, market)
    order = {"score": Lead.lead_score.desc(), "newest": Lead.id.desc(), "name": Lead.indian_legal_name.asc(),
             "checked": Lead.date_last_checked.desc()}.get(sort, Lead.lead_score.desc())
    total = qry.count()
    rows = qry.order_by(order, Lead.id.desc()).offset((max(page, 1) - 1) * PAGE).limit(PAGE).all()
    states = [s for (s,) in db.query(Lead.us_state).distinct().order_by(Lead.us_state) if s]
    owners = [o for (o,) in db.query(Lead.assigned_owner).distinct() if o]
    markets = sorted({m or "US" for (m,) in db.query(Lead.foreign_country).distinct()})
    return render(request, db, "leads.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
                  f=dict(status=status, q=q, priority=priority, us_state=us_state, owner=owner, outreach=outreach,
                         since=since, sort=sort, market=market), states=states, owners=owners, markets=markets,
                  outreach_statuses=OUTREACH_STATUSES)


@app.get("/leads/{pk}")
def lead_detail(pk: int, request: Request, db=Depends(get_db)):
    require(request, db)
    lead = db.get(Lead, pk) or _404()
    history = db.query(LeadHistory).filter_by(lead_pk=pk).order_by(LeadHistory.id.desc()).limit(100).all()
    pipe = db.query(PipelineEntry).filter_by(lead_pk=pk).first()
    users = db.query(User).filter_by(active=True).order_by(User.name).all()
    groups = {}
    for a, label, g in LEAD_FIELDS:
        groups.setdefault(g, []).append((a, label))
    return render(request, db, "lead.html", lead=lead, history=history, pipe=pipe, groups=groups, users=users,
                  outreach_statuses=OUTREACH_STATUSES, stages=PIPELINE_STAGES, users_by_id={u.id: u for u in users})


@app.post("/leads/{pk}")
async def lead_update(pk: int, request: Request, db=Depends(get_db)):
    u = require(request, db)
    form = await check_csrf(request)
    lead = db.get(Lead, pk) or _404()
    changed = []
    for field in sorted(PROTECTED):
        if field in form:
            new = str(form.get(field) or "").strip() or None
            if getattr(lead, field) != new:
                db.add(LeadHistory(lead_pk=pk, user_id=u.id, field=field, old_value=getattr(lead, field), new_value=new))
                setattr(lead, field, new)
                changed.append(field)
    pipe = db.query(PipelineEntry).filter_by(lead_pk=pk).first()
    if pipe and lead.assigned_owner and "assigned_owner" in changed:
        pipe.lead_owner = lead.assigned_owner
    db.commit()
    audit(db, request, "lead_updated", lead.lead_id, {"fields": changed})
    flash(request, "Saved." if changed else "No changes.")
    return redirect(f"/leads/{pk}")


@app.post("/leads/{pk}/disqualify")
async def lead_disqualify(pk: int, request: Request, db=Depends(get_db)):
    u = require(request, db, "analyst")
    form = await check_csrf(request)
    lead = db.get(Lead, pk) or _404()
    reason = str(form.get("reason") or "Disqualified by reviewer").strip()
    db.add(Exclusion(date=date.today(), candidate_name=lead.indian_legal_name or lead.indian_brand_name,
                     cin_llpin=lead.cin_llpin, us_state=lead.us_state, us_entity_number=lead.us_entity_number,
                     domain=lead.domain, type="Disqualified", reason=f"{reason} (by {u.email})",
                     canonical_lead_id=lead.lead_id, source_urls=lead.source_urls, resolved=True, resolved_by=u.id))
    db.add(LeadHistory(lead_pk=pk, user_id=u.id, field="verification_status", old_value=lead.verification_status,
                       new_value="DISQUALIFIED"))
    lead.verification_status = "DISQUALIFIED"
    db.commit()
    audit(db, request, "lead_disqualified", lead.lead_id, {"reason": reason})
    flash(request, f"{lead.lead_id} disqualified.")
    return redirect(f"/leads/{pk}")


# ---------------------------------------------------------------- pipeline
@app.get("/pipeline")
def pipeline(request: Request, db=Depends(get_db), stage: str = "", owner: str = ""):
    require(request, db)
    qry = db.query(PipelineEntry, Lead).join(Lead, Lead.id == PipelineEntry.lead_pk)
    if stage:
        qry = qry.filter(PipelineEntry.opportunity_stage == stage)
    if owner:
        qry = qry.filter(PipelineEntry.lead_owner == owner)
    rows = qry.order_by(PipelineEntry.next_follow_up.is_(None), PipelineEntry.next_follow_up, Lead.lead_score.desc()).limit(500).all()
    counts = dict(db.query(PipelineEntry.opportunity_stage, func.count()).group_by(PipelineEntry.opportunity_stage).all())
    mrr = db.query(func.coalesce(func.sum(PipelineEntry.est_mrr_usd), 0)).filter(
        PipelineEntry.opportunity_stage.notin_(("Lost",))).scalar()
    owners = [o for (o,) in db.query(PipelineEntry.lead_owner).distinct() if o]
    return render(request, db, "pipeline.html", rows=rows, stages=PIPELINE_STAGES, counts=counts, mrr=mrr,
                  f=dict(stage=stage, owner=owner), owners=owners)


@app.post("/pipeline/{pid}")
async def pipeline_update(pid: int, request: Request, db=Depends(get_db)):
    require(request, db)
    form = await check_csrf(request)
    p = db.get(PipelineEntry, pid) or _404()
    for f in ("lead_owner", "channel", "response", "opportunity_stage", "actual_outcome", "notes"):
        if f in form:
            setattr(p, f, str(form.get(f) or "").strip() or None)
    for f in ("outreach_date", "last_contact", "next_follow_up"):
        if f in form:
            v = str(form.get(f) or "").strip()
            setattr(p, f, date.fromisoformat(v) if v else None)
    if "est_mrr_usd" in form:
        v = str(form.get("est_mrr_usd") or "").strip()
        p.est_mrr_usd = float(v) if v else None
    p.meeting_booked = form.get("meeting_booked") == "on"
    db.commit()
    audit(db, request, "pipeline_updated", pid)
    flash(request, "Pipeline updated.")
    back = str(form.get("back") or "/pipeline")
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/pipeline")


# ---------------------------------------------------------------- runs
@app.get("/runs")
def runs(request: Request, db=Depends(get_db), page: int = 1):
    require(request, db)
    qry = db.query(Run).order_by(Run.id.desc())
    total = qry.count()
    rows = qry.offset((max(page, 1) - 1) * PAGE).limit(PAGE).all()
    return render(request, db, "runs.html", rows=rows, page=page, pages=max(1, -(-total // PAGE)))


@app.get("/runs/new")
def run_new(request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    busy = db.query(Run).filter(Run.status.in_(("queued", "running"))).order_by(Run.id.desc()).first()
    return render(request, db, "run_new.html", s=appsettings.get_all(db), busy=busy)


@app.post("/runs")
async def run_now(request: Request, db=Depends(get_db)):
    u = require(request, db, "analyst")
    form = await check_csrf(request)
    targets = {}
    if form.get("custom"):
        try:
            assignments = int(form.get("assignments") or 0) or None
        except ValueError:
            assignments = None
        targets = planner.clean_targets(
            markets=form.getlist("markets"), us_states=_list(form.get("us_states")),
            locations=form.getlist("locations") + _list(form.get("locations_other")),
            industries=form.getlist("industries") + _list(form.get("industries_other")),
            assignments=assignments, revisit=form.get("revisit") == "on",
            max_leads=int(form.get("max_leads")) if str(form.get("max_leads") or "").isdigit() else None)
        if not targets.get("markets"):
            flash(request, "Choose at least one country for the overseas company.", "error")
            return redirect("/runs/new")
    if db.query(Run).filter(Run.status.in_(("queued", "running"))).count():
        flash(request, "A run is already queued or in progress.", "error")
        return redirect("/runs")
    r = Run(run_date=date.today(), trigger="manual", status="queued", requested_by=u.id, targets=targets or None,
            email_report=(form.get("email_report") == "on") if form.get("custom") else None)
    db.add(r)
    db.commit()
    audit(db, request, "run_queued", r.id, {"targets": targets} if targets else None)
    flash(request, f"Run {r.id} queued" + (f" — focus: {planner.describe_targets(targets)}" if targets else "")
          + ". The worker starts it within a minute.")
    return redirect(f"/runs/{r.id}")


@app.get("/runs/{rid}")
def run_detail(rid: int, request: Request, db=Depends(get_db)):
    require(request, db)
    run = db.get(Run, rid) or _404()
    cov = db.query(Coverage).filter_by(run_id=rid).order_by(Coverage.id).all()
    emails = db.query(EmailLog).filter_by(run_id=rid).order_by(EmailLog.id.desc()).all()
    stats, recent = _live_stats(db, run)
    return render(request, db, "run.html", run=run, cov=cov, emails=emails, stats=stats, recent=recent)


def _live_stats(db, run):
    """Run stats are only saved after each assignment; while a run is going, count its finds straight from the
    database so the page shows them the moment they are saved. Returns (stats, recent finds)."""
    st = dict(run.stats or {})
    new = db.query(Lead).filter(Lead.first_run_id == run.id)
    live = {"candidates": new.count() + db.query(Exclusion).filter(Exclusion.run_id == run.id).count(),
            "new_verified": new.filter(Lead.verification_status == "VERIFIED").count(),
            "web_verified": new.filter(Lead.web_verified_at != None).count()}  # noqa: E711
    for k, v in live.items():
        st[k] = max(int(st.get(k) or 0), v)
    recent = [{"id": l.id, "lead_id": l.lead_id, "name": l.indian_brand_name or l.indian_legal_name or "",
               "overseas": l.us_llc_legal_name or "", "country": l.foreign_country or "US",
               "status": "Web-verified" if l.web_verified_at and l.verification_status != "VERIFIED"
               else (l.verification_status or "").title()}
              for l in new.order_by(Lead.id.desc()).limit(10)]
    return st, recent


@app.get("/runs/{rid}/report")
def run_report(rid: int, request: Request, db=Depends(get_db)):
    require(request, db)
    run = db.get(Run, rid) or _404()
    if not run.report_html:
        from .. import report
        _, _, html_body = report.build(db, run, appsettings.get_all(db))
        return HTMLResponse(html_body)
    return HTMLResponse(run.report_html)


@app.post("/runs/{rid}/cancel")
async def run_cancel(rid: int, request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    await check_csrf(request)
    run = db.get(Run, rid) or _404()
    if run.status == "queued":
        run.status, run.finished_at = "cancelled", utcnow()
    elif run.status == "running":
        run.cancel_requested = True
    db.commit()
    audit(db, request, "run_cancel", rid)
    flash(request, "Cancellation requested.")
    return redirect(f"/runs/{rid}")


@app.post("/runs/{rid}/send-report")
async def run_send(rid: int, request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    await check_csrf(request)
    run = db.get(Run, rid) or _404()
    try:
        scheduler.send_run_report(db, run, appsettings.get_all(db, include_secrets=True), kind="manual_report",
                                  interim=run.status not in scheduler.FINISHED)
        flash(request, "Report emailed.")
    except mailer.MailError as e:
        flash(request, f"Email failed: {e}", "error")
    audit(db, request, "report_sent_manually", rid)
    return redirect(f"/runs/{rid}")


@app.get("/api/runs/{rid}/status")
def run_status(rid: int, request: Request, db=Depends(get_db)):
    require(request, db)
    run = db.get(Run, rid) or _404()
    stats, recent = _live_stats(db, run)
    return {"status": run.status, "stats": stats, "recent": recent, "cost_usd": run.cost_usd,
            "log_tail": (run.log or [])[-25:]}


# ---------------------------------------------------------------- exclusions / coverage / logs
@app.get("/exclusions")
def exclusions(request: Request, db=Depends(get_db), type: str = "", show: str = "open", page: int = 1):
    require(request, db, "analyst")
    qry = db.query(Exclusion)
    if type:
        qry = qry.filter(Exclusion.type == type)
    if show == "open":
        qry = qry.filter(Exclusion.resolved == False)  # noqa: E712
    total = qry.count()
    rows = qry.order_by(Exclusion.id.desc()).offset((max(page, 1) - 1) * PAGE).limit(PAGE).all()
    lead_pks = {l.lead_id: l.id for l in db.query(Lead.lead_id, Lead.id).filter(
        Lead.lead_id.in_([r.canonical_lead_id for r in rows if r.canonical_lead_id]))}
    return render(request, db, "exclusions.html", rows=rows, page=page, pages=max(1, -(-total // PAGE)),
                  f=dict(type=type, show=show), lead_pks=lead_pks)


@app.post("/exclusions/{xid}/resolve")
async def exclusion_resolve(xid: int, request: Request, db=Depends(get_db)):
    u = require(request, db, "analyst")
    await check_csrf(request)
    x = db.get(Exclusion, xid) or _404()
    x.resolved, x.resolved_by = True, u.id
    db.commit()
    audit(db, request, "exclusion_resolved", xid)
    return redirect("/exclusions")


@app.get("/coverage")
def coverage(request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    agg = (db.query(Coverage.industry, Coverage.market, Coverage.us_state, func.count(), func.sum(Coverage.queries_run),
                    func.sum(Coverage.candidates_yielded), func.sum(Coverage.verified_yielded), func.max(Coverage.date))
           .filter(Coverage.industry != "").group_by(Coverage.industry, Coverage.market, Coverage.us_state)
           .order_by(func.sum(Coverage.verified_yielded).desc()).all())
    recent = db.query(Coverage).order_by(Coverage.id.desc()).limit(100).all()
    return render(request, db, "coverage.html", agg=agg, recent=recent)


@app.get("/email-log")
def email_log(request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    rows = db.query(EmailLog).order_by(EmailLog.id.desc()).limit(200).all()
    return render(request, db, "email_log.html", rows=rows)


@app.get("/audit")
def audit_log(request: Request, db=Depends(get_db)):
    require(request, db, "admin")
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(300).all()
    users = {u.id: u.email for u in db.query(User).all()}
    return render(request, db, "audit.html", rows=rows, users=users)


# ---------------------------------------------------------------- exports
@app.get("/export/workbook.xlsx")
def export_xlsx(request: Request, db=Depends(get_db)):
    require(request, db, "analyst")
    audit(db, request, "export_workbook")
    data = excel.build_workbook(db)
    fn = f"{appsettings.file_slug(appsettings.get_all(db))}_India_US_LLC_Leads_{date.today()}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.get("/export/leads.csv")
def export_csv(request: Request, db=Depends(get_db), status: str = "VERIFIED", q: str = "", priority: str = "",
               us_state: str = "", owner: str = "", outreach: str = "", since: str = "", market: str = ""):
    require(request, db, "analyst")
    audit(db, request, "export_csv", status)
    attrs = [a for a, _, _ in EXCEL_FIELDS]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([LABEL[a] for a in attrs])
    for l in _lead_query(db, status, q, priority, us_state, owner, outreach, since, market).order_by(Lead.id):
        w.writerow([_csv_safe(getattr(l, a)) for a in attrs])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="leads_{date.today()}.csv"'})


def _csv_safe(v):
    """Neutralise spreadsheet formula injection from scraped text."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# ---------------------------------------------------------------- settings / users (admin)
@app.get("/settings")
def settings_page(request: Request, db=Depends(get_db), tab: str = "schedule"):
    require(request, db, "admin")
    s = appsettings.get_all(db)
    india_count = db.query(IndianCompany).count()
    from ..research.claude_code import find_claude
    return render(request, db, "settings.html", s=s, tab=tab, india_count=india_count,
                  env_api_key=bool(Config.ANTHROPIC_API_KEY),
                  env_gemini_key=bool(Config.GEMINI_API_KEY),
                  env_cc_token=bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                  claude_cli=find_claude())


def _list(v):
    import re
    return [x.strip() for x in re.split(r"[,\n;]+", str(v or "")) if x.strip()]


@app.post("/settings")
async def settings_save(request: Request, db=Depends(get_db)):
    u = require(request, db, "admin")
    form = await check_csrf(request)
    tab = str(form.get("tab") or "schedule")
    vals, errors = {}, []
    try:
        if tab == "schedule":
            from zoneinfo import ZoneInfo
            ZoneInfo(str(form.get("timezone")))
            for k in ("research_start_time", "report_time"):
                datetime.strptime(str(form.get(k)), "%H:%M")
            brand = " ".join(str(form.get("brand_name") or "").split())
            if not 2 <= len(brand) <= 40:
                errors.append("Workspace name must be 2-40 characters")
            vals = dict(brand_name=brand, brand_tagline=" ".join(str(form.get("brand_tagline") or "").split())[:80],
                        timezone=str(form.get("timezone")), schedule_enabled=form.get("schedule_enabled") == "on",
                        research_start_time=str(form.get("research_start_time")),
                        report_time=str(form.get("report_time")),
                        report_wait_minutes=int(form.get("report_wait_minutes") or 0),
                        run_days=[int(d) for d in form.getlist("run_days")])
        elif tab == "email":
            import re
            rcpts, cc = _list(form.get("email_recipients")), _list(form.get("email_cc"))
            bad = [a for a in rcpts + cc if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
            if bad:
                errors.append("Invalid email address: " + ", ".join(bad))
            vals = dict(email_recipients=rcpts, email_cc=cc,
                        email_subject_prefix=str(form.get("email_subject_prefix") or appsettings.DEFAULTS["email_subject_prefix"]),
                        email_attach_workbook=form.get("email_attach_workbook") == "on",
                        email_alert_on_failure=form.get("email_alert_on_failure") == "on",
                        email_after_run=form.get("email_after_run") == "on",
                        smtp_host=str(form.get("smtp_host") or "").strip(), smtp_port=int(form.get("smtp_port") or 587),
                        smtp_security=str(form.get("smtp_security") or "starttls"),
                        smtp_username=str(form.get("smtp_username") or "").strip(),
                        smtp_password=str(form.get("smtp_password") or ""),
                        smtp_from_email=str(form.get("smtp_from_email") or "").strip(),
                        smtp_from_name=str(form.get("smtp_from_name") or "").strip())
        elif tab == "research":
            vals = dict(research_backend=str(form.get("research_backend") or "claude_code"),
                        claude_code_model=str(form.get("claude_code_model") or "sonnet"),
                        claude_code_effort=str(form.get("claude_code_effort") or "medium"),
                        claude_code_batch_minutes=int(form.get("claude_code_batch_minutes") or 25),
                        claude_code_oauth_token=str(form.get("claude_code_oauth_token") or "").strip(),
                        gemini_api_key=str(form.get("gemini_api_key") or "").strip(),
                        gemini_model=str(form.get("gemini_model") or "gemini-2.0-flash"),
                        daily_target=int(form.get("daily_target")), model=str(form.get("model")),
                        effort=str(form.get("effort")), max_run_minutes=int(form.get("max_run_minutes")),
                        max_cost_usd_per_run=float(form.get("max_cost_usd_per_run")),
                        max_batches_per_run=int(form.get("max_batches_per_run")),
                        max_turns_per_batch=int(form.get("max_turns_per_batch")),
                        web_search_max_uses_per_batch=int(form.get("web_search_max_uses_per_batch")),
                        web_fetch_max_uses_per_batch=int(form.get("web_fetch_max_uses_per_batch")),
                        revisit_queue_per_run=int(form.get("revisit_queue_per_run")),
                        industries=_list(form.get("industries")),
                        target_markets=(planner.clean_targets(markets=form.getlist("target_markets")).get("markets")
                                        or appsettings.get(db, "target_markets")),
                        us_states=[x.upper() for x in _list(form.get("us_states"))],
                        indian_states=list(dict.fromkeys(form.getlist("indian_states")
                                                         + _list(form.get("indian_states_other")))),
                        blocked_domains=[x.lower() for x in _list(form.get("blocked_domains"))],
                        default_owner=str(form.get("default_owner") or "").strip(),
                        anthropic_api_key=str(form.get("anthropic_api_key") or "").strip())
            if form.get("targeting_form") and not form.getlist("target_markets"):
                errors.append("Choose at least one default country for the overseas company")
            if not vals["indian_states"]:
                errors.append("Choose at least one Indian city or state")
            if vals["research_backend"] not in ("claude_code", "api", "gemini"):
                errors.append("Unsupported research backend")
            if vals["gemini_model"] not in ("gemini-3.1-pro-preview", "gemini-pro-latest", "gemini-3.8-flash",
                                            "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest",
                                            "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash-lite"):
                errors.append("Unsupported Gemini model")
            if vals["claude_code_model"] not in ("haiku", "sonnet", "opus", "fable"):
                errors.append("Unsupported Claude Code model")
            if vals["claude_code_effort"] not in ("low", "medium", "high"):
                errors.append("Unsupported Claude Code effort")
            if vals["claude_code_oauth_token"] and not vals["claude_code_oauth_token"].startswith("sk-ant-"):
                errors.append("That does not look like a token from `claude setup-token` (it starts with sk-ant-)")
            if vals["model"] not in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"):
                errors.append("Unsupported model")
            if vals["effort"] not in ("low", "medium", "high", "xhigh", "max"):
                errors.append("Unsupported effort")
    except (ValueError, TypeError, KeyError) as e:
        errors.append(f"Invalid value: {e}")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect("/settings", tab=tab)
    changed = appsettings.set_many(db, vals, u.id)
    audit(db, request, "settings_changed", tab, {"keys": changed})
    flash(request, f"Saved ({len(changed)} changed).")
    return redirect("/settings", tab=tab)


@app.post("/settings/test-email")
async def settings_test_email(request: Request, db=Depends(get_db)):
    require(request, db, "admin")
    await check_csrf(request)
    s = appsettings.get_all(db, include_secrets=True)
    try:
        mailer.send(db, "test", s["email_recipients"], f'{s["email_subject_prefix"]} Test email',
                    f'Your {s["brand_name"]} workspace can send email. The daily report will arrive at '
                    f'{s["report_time"]} ({s["timezone"]}).',
                    f"<p>Your {html.escape(s['brand_name'])} workspace can send email.</p><p>The daily report will arrive at "
                    f'<b>{s["report_time"]}</b> ({s["timezone"]}).</p>', retries=1)
        flash(request, "Test email sent to " + ", ".join(s["email_recipients"]))
    except mailer.MailError as e:
        flash(request, f"Test email failed: {e}", "error")
    return redirect("/settings", tab="email")


@app.post("/settings/india-import")
async def settings_india_import(request: Request, db=Depends(get_db)):
    require(request, db, "admin")
    form = await check_csrf(request)
    upload, source = form.get("file"), str(form.get("source_url") or "").strip()
    from ..rules import is_official_in
    if not upload or not getattr(upload, "filename", ""):
        flash(request, "Choose a CSV file.", "error")
    elif not is_official_in(source):
        flash(request, "Source URL must be the mca.gov.in or data.gov.in page the file came from.", "error")
    else:
        from ..india_import import import_csv
        try:
            res = import_csv(db, upload.file, source)
            audit(db, request, "india_import", upload.filename, res)
            flash(request, f"Imported: {res['inserted']} new, {res['updated']} updated, {res['skipped']} skipped.")
        except ValueError as e:
            flash(request, str(e), "error")
    return redirect("/settings", tab="data")


@app.get("/users")
def users_page(request: Request, db=Depends(get_db)):
    require(request, db, "admin")
    return render(request, db, "users.html", rows=db.query(User).order_by(User.id).all())


@app.post("/users")
async def users_create(request: Request, db=Depends(get_db)):
    require(request, db, "admin")
    form = await check_csrf(request)
    email = str(form.get("email") or "").strip().lower()
    pw, role = str(form.get("password") or ""), str(form.get("role") or "sales")
    if role not in ("admin", "analyst", "sales") or "@" not in email:
        flash(request, "Enter a valid email and role.", "error")
    elif db.query(User).filter_by(email=email).first():
        flash(request, "That email already has an account.", "error")
    elif probs := password_problems(pw):
        flash(request, "Password needs " + ", ".join(probs) + ".", "error")
    else:
        db.add(User(email=email, name=str(form.get("name") or email), role=role, password_hash=hash_password(pw)))
        db.commit()
        audit(db, request, "user_created", email, {"role": role})
        flash(request, f"User {email} created.")
    return redirect("/users")


@app.post("/users/{uid}")
async def users_update(uid: int, request: Request, db=Depends(get_db)):
    me = require(request, db, "admin")
    form = await check_csrf(request)
    u = db.get(User, uid) or _404()
    action = form.get("action")
    if u.id == me.id and action in ("deactivate", "role"):
        flash(request, "You cannot change your own role or deactivate yourself.", "error")
        return redirect("/users")
    if action == "deactivate":
        u.active = False
    elif action == "activate":
        u.active, u.locked_until, u.failed_logins = True, None, 0
    elif action == "role" and form.get("role") in ("admin", "analyst", "sales"):
        u.role = form.get("role")
    elif action == "password":
        pw = str(form.get("password") or "")
        if probs := password_problems(pw):
            flash(request, "Password needs " + ", ".join(probs) + ".", "error")
            return redirect("/users")
        u.password_hash = hash_password(pw)
    db.commit()
    audit(db, request, f"user_{action}", u.email)
    flash(request, "User updated.")
    return redirect("/users")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


def _404():
    raise HTTPException(status_code=404, detail="Not found")
