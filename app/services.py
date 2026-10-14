"""Business actions that combine the database with notifications.

Always issue and serve tokens through these functions (not db.issue_token / db.serve_token
directly) so the right emails are triggered. The dashboard and the API both use them.
"""
from __future__ import annotations

from . import auth, config, db, notifier, queueinfo


def issue_token(counter: str, service_type: str = "general", user: dict | None = None,
                notify_email: str | None = None, notify: bool = True,
                path: str | None = None, background: bool = True) -> dict:
    """Issue a token and send the "token issued" email.

    user:         the logged-in account taking the token (its email is used by default).
    notify_email: send updates to this address instead / for walk-ins without an account.
    notify:       False = no emails for this token.
    Raises ValueError for bad input or when the user already holds too many waiting tokens.
    """
    counter = (counter or "").strip()
    if not counter:
        raise ValueError("Please choose a counter.")
    email = None
    if notify:
        email = auth.normalize_email(notify_email or (user or {}).get("email") or "") or None
        if email and not auth.EMAIL_RE.match(email):
            raise ValueError("Please enter a valid email address.")
    if user and db.active_token_count(user["id"], counter, path) >= config.MAX_ACTIVE_TOKENS_PER_COUNTER:
        raise ValueError(f"You already have a waiting token at {counter}. "
                         "Please wait for it to be served before taking another.")
    token = db.issue_token(counter, (service_type or "general").strip()[:80] or "general",
                           path=path, user_id=user["id"] if user else None, notify_email=email)
    token.update(queueinfo.snapshot(token["id"], path))
    token["notify_email"] = email
    token["notification_id"] = notifier.notify(token["id"], "issued", path=path, background=background)
    return token


def serve_token(token_id: int, path: str | None = None, background: bool = True) -> bool:
    """Start service for a token and send the "it's your turn" email."""
    if not db.serve_token(token_id, path=path):
        return False
    notifier.notify(token_id, "served", path=path, background=background)
    return True
