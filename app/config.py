"""Process-level configuration from environment variables.

Anything an admin should change without a redeploy (recipients, schedule, budgets, SMTP) lives in
the database instead - see app/appsettings.py.
"""
import os
import secrets


def _bool(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/portal.db")
    SECRET_KEY = os.environ.get("SECRET_KEY") or ""
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    COOKIE_SECURE = _bool("COOKIE_SECURE", BASE_URL.startswith("https://"))
    SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
    WORKER_POLL_SECONDS = int(os.environ.get("WORKER_POLL_SECONDS", "30"))
    DATA_DIR = os.environ.get("DATA_DIR", "./data")
    USER_AGENT = os.environ.get(
        "RESEARCH_USER_AGENT", "LeadveraResearchBot/1.0 (business-registry research; respects robots.txt)")
    BOOTSTRAP_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
    BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    @classmethod
    def secret_key(cls):
        if cls.SECRET_KEY:
            return cls.SECRET_KEY
        # Dev fallback only: persist a random key so sessions survive restarts.
        os.makedirs(cls.DATA_DIR, exist_ok=True)
        path = os.path.join(cls.DATA_DIR, ".secret_key")
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write(secrets.token_urlsafe(48))
            os.chmod(path, 0o600)
        with open(path) as fh:
            cls.SECRET_KEY = fh.read().strip()
        return cls.SECRET_KEY
