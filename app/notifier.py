"""Email notifications: "token issued" and "your turn" messages.

Every email is saved in the `notifications` table first, then delivered:
  * EMAIL_MODE=smtp    -> sent through your SMTP server (Gmail, Outlook, college mail...)
  * EMAIL_MODE=console -> NOT sent; stored with status 'logged' so admins can preview it
A token gets at most one email per event, so double clicks never send duplicates.
Delivery runs in a background thread so the web page never waits for the mail server.
"""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
import threading
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from . import config, db, queueinfo

log = logging.getLogger("queueless.notifier")
EVENTS = ("issued", "served")


# --------------------------------------------------------------------------- message building
def _fmt(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y, %I:%M %p")
    except ValueError:
        return ts


def _wait_minutes(token: dict) -> int | None:
    try:
        a = datetime.strptime(token["issued_at"], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(token["served_at"], "%Y-%m-%d %H:%M:%S")
        return max(int((b - a).total_seconds() // 60), 0)
    except (TypeError, ValueError, KeyError):
        return None


def build_message(token: dict, event: str, ahead: int | None = None,
                  est_wait_min: int | None = None) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for a token event. User-supplied text is HTML-escaped."""
    if event not in EVENTS:
        raise ValueError(f"Unknown event: {event}")
    name = (token.get("user_name") or "").strip()
    greeting = f"Hello {name}," if name else "Hello,"
    n, counter = token["token_no"], token["counter"]
    service = token.get("service_type") or "general"

    if event == "issued":
        subject = f"Token #{n} issued - {counter}"
        rows = [("Token number", f"#{n}"), ("Counter", counter), ("Service", service),
                ("Issued at", _fmt(token.get("issued_at")))]
        if ahead is not None:
            rows.append(("People ahead of you", str(ahead)))
        if ahead == 0:
            rows.append(("Estimated wait", "none - you are next"))
        elif est_wait_min is not None:
            rows.append(("Estimated wait", f"about {est_wait_min} min"))
        headline = "Your token has been issued."
        footer = "We will email you again when it is your turn. Please stay nearby."
    else:
        subject = f"It's your turn! Token #{n} - {counter}"
        rows = [("Token number", f"#{n}"), ("Counter", counter), ("Service", service),
                ("Called at", _fmt(token.get("served_at")))]
        waited = _wait_minutes(token)
        if waited is not None:
            rows.append(("You waited", f"{waited} min"))
        headline = "It is your turn now."
        footer = f"Please go to the {counter} right away."

    app_url = config.email_settings()["app_url"]
    width = max(len(k) for k, _ in rows)
    text = "\n".join([greeting, "", headline, ""] +
                     [f"  {k.ljust(width)} : {v}" for k, v in rows] +
                     ["", footer] + ([f"Track your token: {app_url}"] if app_url else []) +
                     ["", "- QueueLess Campus"])

    esc = html.escape
    table = "".join(
        f"<tr><td style='padding:4px 14px 4px 0;color:#555'>{esc(k)}</td>"
        f"<td style='padding:4px 0'><b>{esc(v)}</b></td></tr>" for k, v in rows)
    link = (f"<p><a href='{esc(app_url, quote=True)}'>Track your token</a></p>" if app_url else "")
    page = (f"<div style='font-family:Arial,sans-serif;font-size:15px;color:#222'>"
            f"<p>{esc(greeting)}</p><p style='font-size:17px'><b>{esc(headline)}</b></p>"
            f"<table>{table}</table><p>{esc(footer)}</p>{link}"
            f"<p style='color:#888'>- QueueLess Campus</p></div>")
    return subject, text, page


# --------------------------------------------------------------------------- delivery
def _build_email(settings: dict, to_email: str, subject: str, text: str, page: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings["from_name"], settings["sender"]))
    msg["To"] = to_email
    msg.set_content(text)
    if page:
        msg.add_alternative(page, subtype="html")
    return msg


def _smtp_send(settings: dict, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if settings["use_ssl"]:
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=15)
    with server:
        if settings["use_tls"]:
            server.starttls(context=ctx)
        if settings["user"]:
            server.login(settings["user"], settings["password"])
        server.send_message(msg)


def _deliver(notification_id: int, path: str | None = None) -> str:
    """Send (or log) one stored notification and record the outcome. Returns the final status."""
    row = db.get_notification(notification_id, path)
    if row is None:
        return "missing"
    settings = config.email_settings()
    if settings["mode"] != "smtp":
        db.update_notification(notification_id, "logged", None, path)
        log.info("EMAIL (preview) to %s: %s", row["to_email"], row["subject"])
        return "logged"
    try:
        msg = _build_email(settings, row["to_email"], row["subject"], row["body_text"], row["body_html"])
        _smtp_send(settings, msg)
    except Exception as exc:                       # network, auth, bad address ... never crash the app
        err = f"{type(exc).__name__}: {exc}"[:300]
        db.update_notification(notification_id, "failed", err, path)
        log.warning("Email to %s failed: %s", row["to_email"], err)
        return "failed"
    db.update_notification(notification_id, "sent", None, path)
    return "sent"


def _spawn(target, *args) -> None:
    """Run `target` in a background thread (tests replace this to run it immediately)."""
    threading.Thread(target=target, args=args, daemon=True).start()


def notify(token_id: int, event: str, path: str | None = None, background: bool = True) -> int | None:
    """Create and deliver the notification for a token event.

    Returns the notification id, or None when nothing was sent (notifications off, token has no
    email, or this event was already notified). With background=False delivery finishes before
    returning (used by scripts and tests).
    """
    if config.email_settings()["mode"] == "off":
        return None
    token = db.get_token(token_id, path)
    if not token or not token.get("notify_email"):
        return None
    snap = queueinfo.snapshot(token_id, path) if event == "issued" else {"ahead": None, "est_wait_min": None}
    subject, text, page = build_message(token, event, snap["ahead"], snap["est_wait_min"])
    nid = db.create_notification(token_id, token["notify_email"], event, subject, text, page, path=path)
    if nid is None:
        return None
    if background:
        _spawn(_deliver, nid, path)
    else:
        _deliver(nid, path)
    return nid


def retry(notification_id: int, path: str | None = None) -> str:
    """Admin action: send a failed (or previewed) notification again. Returns the new status."""
    row = db.get_notification(notification_id, path)
    if row is None:
        raise ValueError("Notification not found.")
    if row["status"] not in ("failed", "logged"):
        raise ValueError("Only failed or previewed emails can be re-sent.")
    return _deliver(notification_id, path)


def send_test(to_email: str) -> tuple[bool, str]:
    """Send a test email using the current settings. Returns (ok, human-readable message)."""
    settings = config.email_settings()
    if settings["mode"] != "smtp":
        return False, ("Email is in preview mode (SMTP not configured), so nothing was sent. "
                       "Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD in .env and restart.")
    subject = "QueueLess Campus - test email"
    text = "This is a test email from QueueLess Campus. Your email settings work."
    try:
        _smtp_send(settings, _build_email(settings, to_email, subject, text, None))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    return True, f"Test email sent to {to_email}."
