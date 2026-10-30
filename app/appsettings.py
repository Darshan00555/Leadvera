"""Admin-editable settings stored in the database, with typed defaults."""
from .models import Setting
from .security import decrypt, encrypt

DEFAULTS = {
    # Workspace branding (shown in the portal, reports, emails and export file names)
    "brand_name": "Leadvera",
    "brand_tagline": "Verified lead intelligence",
    # Schedule (local time in `timezone`)
    "timezone": "Asia/Kolkata",
    "schedule_enabled": True,
    "research_start_time": "08:30",      # research starts early so the report is ready by report_time
    "report_time": "11:00",
    "report_wait_minutes": 120,          # if research is still running at report_time, wait up to this long
    "run_days": [0, 1, 2, 3, 4, 5, 6],   # Monday=0
    # Research
    "research_backend": "claude_code",   # claude_code = Claude Pro/Max subscription via Claude Code | api = API key
    "claude_code_model": "sonnet",       # alias passed to `claude --model` (sonnet | opus | fable)
    "claude_code_batch_minutes": 10,     # wall-clock limit for one assignment (was 25 — tighter = less credit burn)
    "claude_code_effort": "medium",      # low | medium | high — controls research depth per company
    "claude_code_oauth_token": "",       # from `claude setup-token`; stored encrypted
    "daily_target": 50,                  # daily lead limit: new verified + web-verified leads across all runs
    "model": "claude-sonnet-5-5",        # was claude-opus-5-5 (40x more expensive — sonnet is more than good enough)
    "effort": "low",                     # was "high" — low effort = no extended thinking, far fewer tokens
    "max_run_minutes": 60,               # was 150
    "max_cost_usd_per_run": 4.0,         # was 25.0 — hard guardrail per run
    "max_batches_per_run": 10,           # was 40 — 10 focused assignments beats 40 shallow ones
    "max_turns_per_batch": 12,           # was 30 — enough to find + verify a lead per assignment
    "web_search_max_uses_per_batch": 10, # was 25
    "web_fetch_max_uses_per_batch": 15,  # was 40
    "revisit_queue_per_run": 5,          # was 30 — save budget for new leads
    "target_markets": ["US"],            # US, GB, AU, NZ, EU/EEA country codes, or the "EU" / "EEA" shortcuts
    "industries": ["SaaS", "IT services", "software development", "digital marketing agency", "e-commerce",
                   "edtech", "fintech", "healthtech", "consulting", "AI/ML", "design agency", "data analytics",
                   "cybersecurity", "logistics tech", "media and content", "HR tech"],
    "us_states": ["WY", "DE", "FL", "NM", "TX", "CA", "NY", "NJ", "WA", "NV", "GA", "IL"],
    "indian_states": ["Karnataka", "Maharashtra", "Delhi", "Telangana", "Tamil Nadu", "Gujarat", "Haryana",
                      "Uttar Pradesh", "West Bengal", "Kerala", "Rajasthan", "Punjab", "Madhya Pradesh"],
    "blocked_domains": [],               # never fetch these
    # Email
    "email_recipients": [],
    "email_cc": [],
    "email_subject_prefix": "[Leadvera]",
    "email_attach_workbook": True,
    "email_alert_on_failure": True,
    "email_after_run": True,             # email the report as soon as a manual / test run finishes
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",         # starttls | ssl | none
    "smtp_username": "",
    "smtp_password": "",                 # stored encrypted
    "smtp_from_email": "",
    "smtp_from_name": "Leadvera Reports",
    # Research API key may also come from the ANTHROPIC_API_KEY env var (env wins).
    "anthropic_api_key": "",             # stored encrypted
    # Google Gemini API key may also come from the GEMINI_API_KEY env var (env wins).
    "gemini_api_key": "",                # stored encrypted
    "gemini_model": "gemini-3.5-flash",  # gemini-3.5-flash | gemini-3.8-flash | gemini-3.1-pro-preview
    # Leads
    "default_owner": "",
}
SECRET_KEYS = {"smtp_password", "anthropic_api_key", "claude_code_oauth_token", "gemini_api_key"}


def get(db, key):
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    if row.secret:
        return decrypt(row.value or "")
    return row.value


def file_slug(settings):
    """Brand name as a file-name prefix, e.g. 'Leadvera' or 'Acme_Leads'."""
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", str(settings.get("brand_name") or "")).strip("_") or "Leads"


def get_all(db, include_secrets=False):
    out = dict(DEFAULTS)
    for row in db.query(Setting).all():
        if row.secret:
            out[row.key] = decrypt(row.value or "") if include_secrets else ("••••••••" if row.value else "")
        else:
            out[row.key] = row.value
    return out


def set_many(db, values, user_id=None):
    changed = []
    for key, val in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        secret = key in SECRET_KEYS
        if secret and val in (None, "", "••••••••"):
            continue  # blank secret field in a form means "keep the current one"
        row = db.get(Setting, key) or Setting(key=key)
        new = encrypt(val) if secret else val
        if row.value != new:
            changed.append(key)
        row.value, row.secret, row.updated_by = new, secret, user_id
        db.add(row)
    db.commit()
    return changed
