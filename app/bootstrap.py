import logging

from .config import Config
from .db import SessionLocal, init_db
from .models import User
from .security import hash_password

log = logging.getLogger("bootstrap")


def bootstrap():
    """Create tables and the ADMIN_EMAIL / ADMIN_PASSWORD admin if that account does not exist yet.
    An existing account is never modified."""
    Config.secret_key()
    init_db()
    db = SessionLocal()
    try:
        email = Config.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
        if email and Config.BOOTSTRAP_ADMIN_PASSWORD and not db.query(User).filter(User.email == email).first():
            db.add(User(email=email, name="Administrator", role="admin",
                        password_hash=hash_password(Config.BOOTSTRAP_ADMIN_PASSWORD)))
            db.commit()
            log.info("created bootstrap admin %s", Config.BOOTSTRAP_ADMIN_EMAIL)
    finally:
        db.close()
