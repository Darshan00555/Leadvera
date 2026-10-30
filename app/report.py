"""Section 13 daily report, rendered as HTML email + plain text."""
import html

from .config import Config
from .fields import QUEUE_STATUSES
from .models import Lead


def _rows(st, run):
    return [
        ("Total candidates discovered", st.get("candidates", 0)),
        ("Indian registrations verified", st.get("indian_verified", 0)),
        ("U.S. LLC registrations verified", st.get("us_verified", 0)),
        ("Entity relationships verified", st.get("relationships_verified", 0)),
        ("New verified leads added", st.get("new_verified", 0)),
        ("  of which promoted from Research Queue", st.get("promoted", 0)),
        ("A / B / C priority (new)", f'{st.get("A", 0)} / {st.get("B", 0)} / {st.get("C", 0)}'),
        ("Partially verified candidates", st.get("partial", 0)),
        ("Unverified candidates (queued)", st.get("unverified", 0)),
        ("New web-verified leads (online evidence)", st.get("web_verified", 0)),
        ("Duplicates / possible duplicates", st.get("duplicates", 0)),
        ("Disqualified candidates", st.get("disqualified", 0)),
        ("New leads with decision-maker", st.get("with_decision_maker", 0)),
        ("New leads with business contact channel", st.get("with_contact", 0)),
        ("Existing records updated", st.get("updated", 0)),
        ("Errors", len(run.errors or [])),
        ("Blocked / inaccessible sources", len(run.blocked_sources or [])),
        ("Cumulative verified leads", st.get("total_verified", 0)),
        ("Cumulative web-verified leads", st.get("total_web", 0)),
        ("Open Research Queue items", st.get("queue_size", 0)),
        ("Research cost (USD, est.)", f"{run.cost_usd or 0:.2f}"),
    ]


def build(db, run, settings):
    st = dict(run.stats or {})
    # Live totals: a run that failed early never computed them.
    st["total_verified"] = db.query(Lead).filter_by(verification_status="VERIFIED").count()
    queued = db.query(Lead).filter(Lead.verification_status.in_(QUEUE_STATUSES))
    web_q = queued.filter(Lead.web_verified_at != None)  # noqa: E711
    st["total_web"] = web_q.count()
    st["queue_size"] = queued.count() - st["total_web"]
    target = int(settings["daily_target"])
    new = st.get("new_verified", 0)
    shortfall = max(0, target - new - st.get("web_verified", 0))
    day = run.run_date.isoformat() if run.run_date else ""
    failed = run.status != "completed"
    subject = (f'{settings["email_subject_prefix"]} {day}: {new} new verified leads (daily limit {target}) | '
               f'total {st.get("total_verified", 0)}'
               + (f' | +{st["web_verified"]} web-verified' if st.get("web_verified") else "")
               + (f" | RUN {run.status.upper()}" if failed else ""))
    recs = (run.recommendations or [])[:3]
    top, web_new = [], []
    if run.started_at:
        q = db.query(Lead).filter(Lead.verification_status == "VERIFIED", Lead.verified_at >= run.started_at)
        if run.finished_at:
            q = q.filter(Lead.verified_at <= run.finished_at)
        top = q.order_by(Lead.lead_score.desc()).limit(20).all()
        wq = web_q.filter(Lead.web_verified_at >= run.started_at)
        if run.finished_at:
            wq = wq.filter(Lead.web_verified_at <= run.finished_at)
        web_new = wq.order_by(Lead.id).limit(30).all()
    link = f"{Config.BASE_URL}/runs/{run.id}"
    leads_link = f"{Config.BASE_URL}/leads?since={day}"

    brand = settings.get("brand_name") or "Lead report"
    t = [f"{brand} - India + US LLC Lead Report ({day})", ""]
    if failed:
        t += [f"RUN STATUS: {run.status.upper()} - {st.get('stop_reason', '')}", ""]
    t += [f"{k}: {v}" for k, v in _rows(st, run)]
    if shortfall:
        t += ["", f"Below the daily limit by {shortfall}. Only records passing every verification gate count as verified."]
    t += ["", f"Stopped because: {st.get('stop_reason', 'n/a')}"]
    if top:
        t += ["", "Top new leads:"] + [
            f"- [{r.priority}{r.lead_score}] {r.indian_brand_name or r.indian_legal_name} <-> {r.us_llc_legal_name} "
            f"({r.us_state}) - {r.outreach_angle or ''}" for r in top]
    if web_new:
        t += ["", "New web-verified leads (cited online, not yet confirmed on official registries):"] + [
            f"- {r.indian_brand_name or r.indian_legal_name} ({r.cin_llpin}) <-> {r.us_llc_legal_name} "
            f"({r.foreign_country or 'US'}) - {r.founder_ceo_name or r.decision_maker_name or ''}" for r in web_new]
    if run.blocked_sources:
        t += ["", "Blocked sources:"] + [f"- {b}" for b in run.blocked_sources]
    if run.errors:
        t += ["", "Errors:"] + [f"- {e}" for e in run.errors[:20]]
    t += ["", "Recommendations for tomorrow:"] + [f"{i}. {r}" for i, r in enumerate(recs, 1)]
    t += ["", f"Run details: {link}", f"New leads: {leads_link}"]
    text = "\n".join(t)

    e = html.escape
    kpi = "".join(
        f"<td style='padding:12px 16px;background:#f3f6fb;border-radius:8px'><div style='font-size:12px;"
        f"color:#5b6b82'>{e(k)}</div><div style='font-size:24px;font-weight:700;color:#14284b'>{e(str(v))}</div></td>"
        f"<td style='width:8px'></td>"
        for k, v in (("New verified", new), ("New web-verified", st.get("web_verified", 0)), ("Daily limit", target),
                     ("Total verified", st.get("total_verified", 0)), ("Total web-verified", st.get("total_web", 0))))
    tr = "".join(f"<tr><td style='padding:5px 16px 5px 0;border-bottom:1px solid #eef1f5'>{e(k)}</td>"
                 f"<td style='text-align:right;border-bottom:1px solid #eef1f5'><b>{e(str(v))}</b></td></tr>"
                 for k, v in _rows(st, run))
    lead_rows = "".join(
        f"<tr><td>{e(r.priority or '')}</td><td>{r.lead_score}</td>"
        f"<td><a href='{e(Config.BASE_URL)}/leads/{r.id}'>{e(r.indian_brand_name or r.indian_legal_name or '')}</a></td>"
        f"<td>{e(r.us_llc_legal_name or '')} ({e(r.us_state or '')})</td>"
        f"<td>{e(r.decision_maker_name or r.founder_ceo_name or '')}</td><td>{e(r.outreach_angle or '')}</td></tr>"
        for r in top)
    banner = (f"<p style='background:#fdecea;color:#8a1c12;padding:10px 12px;border-radius:6px'><b>Run "
              f"{e(run.status)}</b> - {e(str(st.get('stop_reason', '')))}</p>") if failed else ""
    short = (f"<p style='color:#5b6b82'>Below the daily limit by <b>{shortfall}</b>. Only records that pass every "
             f"verification gate count as verified.</p>") if shortfall else ""
    blocked = "".join(f"<li>{e(b)}</li>" for b in run.blocked_sources or [])
    web_rows = "".join(
        f"<tr><td><a href='{e(Config.BASE_URL)}/leads/{r.id}'>{e(r.indian_brand_name or r.indian_legal_name or '')}</a>"
        f"<br><span style='color:#8793a5'>{e(r.cin_llpin or '')}</span></td>"
        f"<td>{e(r.us_llc_legal_name or '')} ({e(r.foreign_country or 'US')})</td>"
        f"<td>{e(r.founder_ceo_name or r.decision_maker_name or '')}</td><td>{e(r.outreach_angle or '')}</td></tr>"
        for r in web_new)
    html_body = f"""<!doctype html><html><body style="margin:0;background:#ffffff">
<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b2430;max-width:820px;margin:0 auto;padding:24px">
<div style="font-size:12px;letter-spacing:.08em;color:#5b6b82;text-transform:uppercase">{e(brand)}</div>
<h1 style="font-size:22px;margin:4px 0 16px;color:#14284b">Daily lead report - {e(day)}</h1>
{banner}<table style="border-collapse:separate"><tr>{kpi}</tr></table>{short}
<h2 style="font-size:16px;margin:24px 0 8px">Run metrics</h2><table style="border-collapse:collapse;min-width:420px">{tr}</table>
<p style="color:#5b6b82">Stopped because: {e(str(st.get('stop_reason', 'n/a')))}</p>
{"<h2 style='font-size:16px;margin:24px 0 8px'>Top new leads</h2><table cellpadding='6' style='border-collapse:collapse;font-size:13px;border:1px solid #e3e8ef'><tr style='background:#f3f6fb;text-align:left'><th>Pri</th><th>Score</th><th>Indian company</th><th>US LLC</th><th>Decision-maker</th><th>Outreach angle</th></tr>" + lead_rows + "</table>" if top else ""}
{"<h2 style='font-size:16px;margin:24px 0 8px'>New web-verified leads</h2><p style='color:#5b6b82;margin:0 0 8px'>Every fact cited from public pages; not yet confirmed on official registries.</p><table cellpadding='6' style='border-collapse:collapse;font-size:13px;border:1px solid #e3e8ef'><tr style='background:#f3f6fb;text-align:left'><th>Indian company</th><th>Overseas entity</th><th>Decision-maker</th><th>Outreach angle</th></tr>" + web_rows + "</table>" if web_new else ""}
{"<h2 style='font-size:16px;margin:24px 0 8px'>Blocked sources</h2><ul>" + blocked + "</ul>" if blocked else ""}
<h2 style="font-size:16px;margin:24px 0 8px">Recommendations for tomorrow</h2><ol>{"".join(f"<li>{e(r)}</li>" for r in recs) or "<li>n/a</li>"}</ol>
<p><a href="{e(link)}" style="background:#1f4fd1;color:#fff;padding:9px 14px;border-radius:6px;text-decoration:none">Open run in portal</a>
&nbsp; <a href="{e(leads_link)}">View new leads</a></p>
<p style="color:#8793a5;font-size:12px">Automated internal report. No outreach has been sent to any lead.</p>
</div></body></html>"""
    return subject, text, html_body
