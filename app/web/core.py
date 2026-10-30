"""Shared web plumbing: templates, auth, roles, CSRF, flash messages, audit."""
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import jurisdictions as J
from ..config import Config
from ..models import AuditLog, User, utcnow
from ..research.planner import describe_targets

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

ROLE_RANK = {"sales": 1, "analyst": 2, "admin": 3}


class LoginRequired(Exception):
    pass


def current_user(request: Request, db):
    uid = request.session.get("uid")
    issued = request.session.get("iat", 0)
    if not uid or datetime.now().timestamp() - issued > Config.SESSION_HOURS * 3600:
        return None
    u = db.get(User, uid)
    return u if u and u.active else None


def require(request, db, role="sales"):
    u = current_user(request, db)
    if u is None:
        raise LoginRequired()
    if ROLE_RANK.get(u.role, 0) < ROLE_RANK[role]:
        raise HTTPException(status_code=403, detail="Your role does not allow this action")
    request.state.user = u
    return u


def csrf_token(request):
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


async def check_csrf(request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf_token", "")), request.session.get("csrf", "") or "x"):
        raise HTTPException(status_code=400, detail="Form expired - reload the page and try again")
    return form


def flash(request, msg, kind="ok"):
    request.session["flash"] = list(request.session.get("flash", [])) + [[kind, msg]]


def pop_flash(request):
    return request.session.pop("flash", [])


def audit(db, request, action, target="", detail=None):
    u = getattr(request.state, "user", None)
    db.add(AuditLog(user_id=u.id if u else None, action=action, target=str(target)[:256], detail=detail or {},
                    ip=request.client.host if request.client else None))
    db.commit()


def redirect(url, **params):
    return RedirectResponse(url + (("?" + urlencode(params)) if params else ""), status_code=303)


def render(request, db, name, **ctx):
    from .. import appsettings
    tz = appsettings.get(db, "timezone") or "Asia/Kolkata"
    ctx.update(request=request, user=getattr(request.state, "user", None), csrf=csrf_token(request),
               flashes=pop_flash(request), tzname=tz, base_url=Config.BASE_URL,
               brand=appsettings.get(db, "brand_name") or "Leadvera",
               brand_tagline=appsettings.get(db, "brand_tagline") or "")
    return templates.TemplateResponse(request, name, ctx)


def _fmt_dt(v, tz="Asia/Kolkata"):
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _urls(v):
    from ..rules import split_urls
    return split_urls(v)


def _initials(v):
    words = [w for w in str(v or "").replace("-", " ").split() if w[:1].isalnum()]
    return ("".join(w[0] for w in words[:2]) or "?").upper()


def _evidence_items(v):
    """Split a '; '-joined problem list, keeping '; ' that sits inside parentheses."""
    items, depth, cur = [], 0, ""
    text = str(v or "")
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if ch == ";" and depth <= 0 and text[i + 1:i + 2] == " ":
            items.append(cur.strip())
            cur = ""
            continue
        cur += ch
    items.append(cur.strip())
    return [x for x in items if x]


def _asset_v():
    """Cache-busting token for /static links: changes whenever a stylesheet or script changes."""
    static = HERE / "static"
    return str(int(max(f.stat().st_mtime for f in static.iterdir() if f.suffix in (".css", ".js"))))


templates.env.globals["asset_v"] = _asset_v
templates.env.globals["J"] = J
templates.env.filters["describe_targets"] = describe_targets
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["evidence_items"] = _evidence_items
templates.env.filters["initials"] = _initials
templates.env.filters["urls"] = _urls
templates.env.globals["now"] = utcnow
templates.env.globals["timedelta"] = timedelta
