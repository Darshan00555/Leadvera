import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import IntegrityError

from .config import Config
from .models import Lock, utcnow

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=64)


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password, stored):
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def password_problems(pw):
    probs = []
    if len(pw) < 12:
        probs.append("at least 12 characters")
    if pw.lower() == pw or pw.upper() == pw:
        probs.append("mixed upper and lower case")
    if not any(c.isdigit() for c in pw):
        probs.append("a digit")
    return probs


def _fernet():
    key = hashlib.sha256(("af-portal-secrets:" + Config.secret_key()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError):
        return ""


def acquire_lock(db, name, owner, stale_after=timedelta(hours=6)):
    """Insert-or-fail mutex. A lock older than stale_after is assumed to belong to a dead process."""
    existing = db.get(Lock, name)
    if existing and utcnow() - existing.acquired_at > stale_after:
        db.delete(existing)
        db.commit()
    try:
        db.add(Lock(name=name, owner=owner, acquired_at=utcnow()))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def refresh_lock(db, name):
    lk = db.get(Lock, name)
    if lk:
        lk.acquired_at = utcnow()
        db.commit()


def release_lock(db, name, owner):
    lk = db.get(Lock, name)
    if lk and lk.owner == owner:
        db.delete(lk)
        db.commit()
