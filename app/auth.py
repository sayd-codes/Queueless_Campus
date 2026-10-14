"""Accounts: registration, login, roles (user / admin) and API sessions.

Passwords are never stored: only a salted PBKDF2-SHA256 hash (Python standard library).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta

from . import config, db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = ("user", "admin")
ITERATIONS = 200_000
_FAILS: dict[str, list[float]] = {}     # email -> times of recent wrong passwords


class AuthLocked(ValueError):
    """Too many wrong passwords for this email; try again later."""


def _now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(test.hex(), digest)
    except (ValueError, TypeError):
        return False


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _public(row) -> dict:
    d = dict(row)
    d.pop("password_hash", None)
    return d


# --------------------------------------------------------------------------- users
def create_user(email: str, password: str, name: str = "", role: str = "user",
                path: str | None = None) -> dict:
    """Create an account. Raises ValueError with a readable message when input is invalid."""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    if len(password or "") < config.MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters.")
    if role not in ROLES:
        raise ValueError("Role must be 'user' or 'admin'.")
    try:
        with db.connect(path) as c:
            cur = c.execute(
                "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, (name or "").strip()[:80], role, hash_password(password),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("An account with this email already exists.")
    return get_user(uid, path)


def get_user(user_id: int, path: str | None = None) -> dict | None:
    with db.connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(r) if r else None


def list_users(path: str | None = None) -> list[dict]:
    with db.connect(path) as c:
        rows = c.execute("SELECT * FROM users ORDER BY role, email").fetchall()
    return [_public(r) for r in rows]


def count_admins(path: str | None = None) -> int:
    with db.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


def set_role(user_id: int, role: str, path: str | None = None) -> dict:
    """Promote / demote a user. The last remaining admin cannot be demoted."""
    if role not in ROLES:
        raise ValueError("Role must be 'user' or 'admin'.")
    user = get_user(user_id, path)
    if user is None:
        raise ValueError("User not found.")
    if user["role"] == "admin" and role != "admin" and count_admins(path) <= 1:
        raise ValueError("There must be at least one admin; promote someone else first.")
    with db.connect(path) as c:
        c.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    return get_user(user_id, path)


# --------------------------------------------------------------------------- login
def _locked(email: str) -> bool:
    now = _now()
    recent = [t for t in _FAILS.get(email, []) if now - t < config.LOGIN_LOCK_SECONDS]
    _FAILS[email] = recent
    return len(recent) >= config.LOGIN_MAX_FAILURES


def authenticate(email: str, password: str, path: str | None = None) -> dict | None:
    """Return the user if email + password are right, None if wrong.

    Raises AuthLocked after too many wrong passwords for the same email.
    """
    email = normalize_email(email)
    if _locked(email):
        raise AuthLocked("Too many failed attempts. Please wait a few minutes and try again.")
    with db.connect(path) as c:
        r = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    stored = r["password_hash"] if r else hash_password("dummy-password-for-timing")
    ok = verify_password(password or "", stored)
    if r is None or not ok:
        _FAILS.setdefault(email, []).append(_now())
        return None
    _FAILS.pop(email, None)
    return _public(r)


# --------------------------------------------------------------------------- API sessions
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(user_id: int, path: str | None = None) -> str:
    """Create a login session and return the secret bearer token (only its hash is stored)."""
    raw = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=config.SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect(path) as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                  (_hash_token(raw), user_id, expires))
    return raw


def user_from_session(raw: str, path: str | None = None) -> dict | None:
    if not raw:
        return None
    with db.connect(path) as c:
        r = c.execute("SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
                      (_hash_token(raw),)).fetchone()
    if r is None or r["expires_at"] < datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        return None
    return get_user(r["user_id"], path)


def end_session(raw: str, path: str | None = None) -> None:
    with db.connect(path) as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw),))
