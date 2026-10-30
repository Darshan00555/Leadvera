import re

import pytest
from fastapi.testclient import TestClient

from app.ingest import ingest_candidate
from app.models import Lead, User
from app.security import hash_password

from .conftest import GOOD

PW = "Str0ngPassword!"


@pytest.fixture()
def client(db):
    from app.db import get_db
    from app.web.main import app
    for email, role in (("admin@example.com", "admin"), ("sales@example.com", "sales")):
        db.add(User(email=email, name=role, role=role, password_hash=hash_password(PW)))
    db.commit()
    ingest_candidate(db, GOOD)

    def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def csrf(c, url="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def login(c, email):
    r = c.post("/login", data={"email": email, "password": PW, "csrf_token": csrf(c), "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_requires_login(client):
    r = client.get("/leads", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_bad_password_and_lockout(client, db):
    for _ in range(5):
        client.post("/login", data={"email": "sales@example.com", "password": "nope", "csrf_token": csrf(client)})
    r = client.post("/login", data={"email": "sales@example.com", "password": PW, "csrf_token": csrf(client)},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/login")  # locked even with the right password


def test_csrf_enforced(client):
    login(client, "admin@example.com")
    r = client.post("/runs", data={"csrf_token": "forged"})
    assert r.status_code == 400


def test_sales_role_cannot_reach_admin(client):
    login(client, "sales@example.com")
    assert client.get("/settings").status_code == 403
    assert client.get("/export/workbook.xlsx").status_code == 403
    assert client.get("/leads").status_code == 200


def test_admin_pages_and_exports(client):
    login(client, "admin@example.com")
    for url in ("/", "/leads", "/leads?status=queue", "/leads/1", "/pipeline", "/runs", "/exclusions", "/coverage",
                "/settings", "/settings?tab=email", "/settings?tab=research", "/settings?tab=data", "/users",
                "/audit", "/email-log", "/account"):
        assert client.get(url).status_code == 200, url
    x = client.get("/export/workbook.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert "Testco" in client.get("/export/leads.csv").text


def test_sales_edit_logged_and_protected(client, db):
    login(client, "sales@example.com")
    tok = csrf(client, "/leads/1")
    r = client.post("/leads/1", data={"csrf_token": tok, "notes": "Spoke to founder", "outreach_status": "Contacted",
                                      "assigned_owner": "", "next_sales_action": "Send proposal"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    lead = db.get(Lead, 1)
    assert lead.notes == "Spoke to founder" and lead.outreach_status == "Contacted"
    ingest_candidate(db, {**GOOD, "business_phone": "+1 555"})
    db.expire_all()
    assert db.get(Lead, 1).notes == "Spoke to founder"


def test_settings_save_and_run_queue(client):
    login(client, "admin@example.com")
    tok = csrf(client, "/settings?tab=email")
    r = client.post("/settings", data={"csrf_token": tok, "tab": "email", "email_recipients": "a@example.com, b@example.com",
                                       "smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_security": "starttls",
                                       "smtp_from_email": "bot@example.com", "smtp_password": "pw"}, follow_redirects=True)
    assert "a@example.com, b@example.com" in r.text
    bad = client.post("/settings", data={"csrf_token": tok, "tab": "email", "email_recipients": "not-an-email"},
                      follow_redirects=True)
    assert "Invalid email address" in bad.text
    r = client.post("/runs", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/runs", data={"csrf_token": tok}, follow_redirects=True)
    assert "already queued" in r.text


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "default-src 'self'" in r.headers["content-security-policy"]


def test_subscription_backend_settings(client, db):
    from app import appsettings
    from app.models import Setting
    login(client, "admin@example.com")
    tok = csrf(client, "/settings?tab=research")
    form = {"csrf_token": tok, "tab": "research", "research_backend": "claude_code", "claude_code_model": "sonnet",
            "claude_code_batch_minutes": "20", "claude_code_oauth_token": "sk-ant-oat01-abc", "daily_target": "200",
            "model": "claude-opus-5-5", "effort": "high", "max_run_minutes": "150", "max_cost_usd_per_run": "25",
            "max_batches_per_run": "10", "max_turns_per_batch": "30", "web_search_max_uses_per_batch": "25",
            "web_fetch_max_uses_per_batch": "40", "revisit_queue_per_run": "10", "industries": "SaaS",
            "us_states": "wy, de", "indian_states": "Karnataka"}
    r = client.post("/settings", data=form, follow_redirects=True)
    assert "Saved" in r.text and "saved — leave blank to keep" in r.text
    assert appsettings.get(db, "claude_code_oauth_token") == "sk-ant-oat01-abc"
    assert "sk-ant-oat01-abc" not in str(db.get(Setting, "claude_code_oauth_token").value)
    bad = client.post("/settings", data={**form, "claude_code_oauth_token": "not-a-token"}, follow_redirects=True)
    assert "setup-token" in bad.text
