import sqlite3

import pytest

from app import auth, config, db, notifier, queueinfo, services


# ----------------------------------------------------------------- fake SMTP server
class FakeSMTP:
    sent: list = []
    fail_with: Exception | None = None
    calls: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_with, FakeSMTP.calls = [], None, []
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-app-password")
    return FakeSMTP


@pytest.fixture()
def student(dbpath):
    return auth.create_user("Student@College.edu", "password123", "Asha", path=dbpath)


# ----------------------------------------------------------------- accounts
def test_register_login_and_roles(dbpath):
    u = auth.create_user(" Asha@College.edu ", "password123", "Asha", path=dbpath)
    assert u["email"] == "asha@college.edu" and u["role"] == "user" and "password_hash" not in u
    assert auth.authenticate("ASHA@college.edu", "password123", path=dbpath)["id"] == u["id"]
    assert auth.authenticate("asha@college.edu", "wrong-password", path=dbpath) is None
    assert auth.authenticate("nobody@college.edu", "password123", path=dbpath) is None
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user("asha@college.edu", "password123", path=dbpath)
    with pytest.raises(ValueError, match="valid email"):
        auth.create_user("not-an-email", "password123", path=dbpath)
    with pytest.raises(ValueError, match="at least"):
        auth.create_user("b@college.edu", "short", path=dbpath)


def test_password_is_hashed_not_stored(dbpath):
    auth.create_user("a@b.co", "password123", path=dbpath)
    with db.connect(dbpath) as c:
        stored = c.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "password123" not in stored and stored.startswith("pbkdf2_sha256$")
    assert auth.verify_password("password123", stored) and not auth.verify_password("x", stored)


def test_last_admin_cannot_be_demoted(dbpath):
    admin = auth.create_user("root@college.edu", "password123", role="admin", path=dbpath)
    other = auth.create_user("o@college.edu", "password123", path=dbpath)
    with pytest.raises(ValueError, match="at least one admin"):
        auth.set_role(admin["id"], "user", path=dbpath)
    auth.set_role(other["id"], "admin", path=dbpath)
    assert auth.set_role(admin["id"], "user", path=dbpath)["role"] == "user"


def test_login_lockout_after_repeated_failures(dbpath, monkeypatch):
    auth._FAILS.clear()
    auth.create_user("lock@college.edu", "password123", path=dbpath)
    for _ in range(config.LOGIN_MAX_FAILURES):
        assert auth.authenticate("lock@college.edu", "bad-password", path=dbpath) is None
    with pytest.raises(auth.AuthLocked):
        auth.authenticate("lock@college.edu", "password123", path=dbpath)   # even the right one
    t0 = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: t0 + config.LOGIN_LOCK_SECONDS + 1)
    assert auth.authenticate("lock@college.edu", "password123", path=dbpath) is not None


def test_sessions(dbpath):
    u = auth.create_user("s@college.edu", "password123", path=dbpath)
    raw = auth.create_session(u["id"], path=dbpath)
    assert auth.user_from_session(raw, path=dbpath)["id"] == u["id"]
    assert auth.user_from_session("garbage", path=dbpath) is None
    with db.connect(dbpath) as c:                    # the raw token is never stored
        assert raw not in str(c.execute("SELECT * FROM sessions").fetchall()[0][:])
    auth.end_session(raw, path=dbpath)
    assert auth.user_from_session(raw, path=dbpath) is None


# ----------------------------------------------------------------- migration
def test_old_database_is_upgraded_without_losing_data(tmp_path):
    p = str(tmp_path / "old.db")
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, counter TEXT NOT NULL,
            token_no INTEGER NOT NULL, service_type TEXT DEFAULT 'general', issued_at TEXT NOT NULL,
            served_at TEXT, finished_at TEXT, source TEXT DEFAULT 'token');
        INSERT INTO tokens (counter, token_no, issued_at, served_at)
            VALUES ('Fee Counter', 1, '2026-10-05 10:00:00', '2026-10-05 10:05:00');
    """)
    con.commit()
    con.close()
    db.init_db(p)
    db.init_db(p)                                   # running twice must be harmless
    df = db.load_tokens(p)
    assert len(df) == 1 and df.loc[0, "counter"] == "Fee Counter"
    assert "user_id" in df.columns and "notify_email" in df.columns
    t = db.issue_token("Fee Counter", path=p, user_id=1, notify_email="a@b.co")
    assert t["token_no"] == 1                        # new day -> numbering restarts
    u = auth.create_user("a@b.co", "password123", path=p)
    assert u["id"] == 1


# ----------------------------------------------------------------- queue info
def test_queue_position_and_estimate(dbpath):
    a = services.issue_token("Fee Counter", path=dbpath, background=False)
    b = services.issue_token("Fee Counter", path=dbpath, background=False)
    c = services.issue_token("Admin Office", path=dbpath, background=False)
    assert (a["ahead"], b["ahead"], c["ahead"]) == (0, 1, 0)
    assert b["est_wait_min"] == queueinfo.estimate_wait_min("Fee Counter", 1) > 0
    services.serve_token(a["id"], path=dbpath, background=False)
    assert db.queue_position(b["id"], path=dbpath) == 0
    assert db.queue_position(a["id"], path=dbpath) is None          # already served


# ----------------------------------------------------------------- notifications
def test_preview_mode_logs_both_emails(dbpath, student):
    t = services.issue_token("Fee Counter", "Fee payment", user=student, path=dbpath, background=False)
    assert services.serve_token(t["id"], path=dbpath, background=False)
    log = db.list_notifications(path=dbpath).sort_values("id")
    assert list(log["event"]) == ["issued", "served"] and set(log["status"]) == {"logged"}
    assert set(log["to_email"]) == {"student@college.edu"}
    issued = db.get_notification(t["notification_id"], dbpath)
    assert f"#{t['token_no']}" in issued["subject"] and "Hello Asha" in issued["body_text"]
    assert "People ahead of you : 0" in issued["body_text"] and "you are next" in issued["body_text"]


def test_smtp_mode_sends_real_message(dbpath, student, smtp):
    t = services.issue_token("Fee Counter", "Fee payment", user=student, path=dbpath, background=False)
    services.serve_token(t["id"], path=dbpath, background=False)
    assert len(smtp.sent) == 2
    first, second = smtp.sent
    assert first["To"] == "student@college.edu" and "bot@example.com" in first["From"]
    assert "issued" in first["Subject"] and "your turn" in second["Subject"]
    assert first.get_body(("html",)) is not None and first.get_body(("plain",)) is not None
    assert ("starttls",) in smtp.calls and ("login", "bot@example.com", "secret-app-password") in smtp.calls
    assert set(db.list_notifications(path=dbpath)["status"]) == {"sent"}


def test_smtp_failure_is_recorded_and_can_be_retried(dbpath, student, smtp):
    smtp.fail_with = ConnectionRefusedError("server down")
    t = services.issue_token("Fee Counter", user=student, path=dbpath, background=False)
    assert t["id"]                                    # the token itself is still issued
    n = db.get_notification(t["notification_id"], dbpath)
    assert n["status"] == "failed" and "server down" in n["error"]
    assert "secret-app-password" not in (n["error"] or "")
    smtp.fail_with = None
    assert notifier.retry(n["id"], dbpath) == "sent"
    assert len(smtp.sent) == 1
    with pytest.raises(ValueError):
        notifier.retry(n["id"], dbpath)               # already sent


def test_no_duplicate_emails_for_same_event(dbpath, student):
    t = services.issue_token("Fee Counter", user=student, path=dbpath, background=False)
    assert notifier.notify(t["id"], "issued", path=dbpath, background=False) is None
    services.serve_token(t["id"], path=dbpath, background=False)
    assert services.serve_token(t["id"], path=dbpath, background=False) is False
    assert len(db.list_notifications(path=dbpath)) == 2


def test_no_email_when_not_wanted(dbpath, student, monkeypatch):
    services.issue_token("Fee Counter", user=student, notify=False, path=dbpath, background=False)
    services.issue_token("Admin Office", path=dbpath, background=False)          # walk-in, no email
    assert db.list_notifications(path=dbpath).empty
    monkeypatch.setenv("EMAIL_MODE", "off")
    services.issue_token("Examination Cell", user=student, path=dbpath, background=False)
    assert db.list_notifications(path=dbpath).empty


def test_walk_in_with_email_and_validation(dbpath):
    t = services.issue_token("Admin Office", notify_email=" Walkin@Mail.com ", path=dbpath, background=False)
    assert t["notify_email"] == "walkin@mail.com"
    assert db.list_notifications(path=dbpath).iloc[0]["to_email"] == "walkin@mail.com"
    with pytest.raises(ValueError, match="valid email"):
        services.issue_token("Admin Office", notify_email="oops", path=dbpath, background=False)
    with pytest.raises(ValueError, match="counter"):
        services.issue_token("  ", path=dbpath, background=False)


def test_one_waiting_token_per_counter_per_user(dbpath, student):
    t = services.issue_token("Fee Counter", user=student, path=dbpath, background=False)
    with pytest.raises(ValueError, match="already have a waiting token"):
        services.issue_token("Fee Counter", user=student, path=dbpath, background=False)
    services.issue_token("Admin Office", user=student, path=dbpath, background=False)   # other counter ok
    services.serve_token(t["id"], path=dbpath, background=False)
    services.issue_token("Fee Counter", user=student, path=dbpath, background=False)    # allowed again


def test_user_input_is_escaped_in_html_email():
    token = {"token_no": 1, "counter": "Fee Counter", "service_type": "<script>alert(1)</script>",
             "user_name": "<b>Eve</b>", "issued_at": "2026-10-05 10:00:00"}
    _, text, page = notifier.build_message(token, "issued", 0, 0)
    assert "<script>" not in page and "&lt;script&gt;" in page and "<b>Eve</b>" not in page
    assert "<script>" in text                         # plain text is not HTML, so it stays literal


def test_served_email_reports_wait_time():
    token = {"token_no": 7, "counter": "Admin Office", "service_type": "ID card", "user_name": "",
             "issued_at": "2026-10-05 10:00:00", "served_at": "2026-10-05 10:12:30"}
    subject, text, _ = notifier.build_message(token, "served")
    assert "your turn" in subject.lower() and "You waited" in text and "12 min" in text


def test_send_test_email(smtp, monkeypatch):
    ok, msg = notifier.send_test("me@example.com")
    assert ok and smtp.sent[0]["To"] == "me@example.com"
    smtp.fail_with = OSError("bad credentials")
    ok, msg = notifier.send_test("me@example.com")
    assert not ok and "bad credentials" in msg
    monkeypatch.setenv("EMAIL_MODE", "console")
    assert notifier.send_test("me@example.com")[0] is False


def test_email_mode_auto_detection(monkeypatch):
    monkeypatch.delenv("EMAIL_MODE")
    assert config.email_settings()["mode"] == "console"
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    assert config.email_settings()["mode"] == "console"            # still no sender
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    s = config.email_settings()
    assert s["mode"] == "smtp" and s["use_tls"] and not s["use_ssl"] and s["port"] == 587
    monkeypatch.setenv("SMTP_PORT", "465")
    assert config.email_settings()["use_ssl"]


def test_clear_tokens_also_clears_notifications(dbpath, student):
    services.issue_token("Fee Counter", user=student, path=dbpath, background=False)
    db.clear_tokens(dbpath)
    assert db.list_notifications(path=dbpath).empty and len(auth.list_users(dbpath)) == 1


def test_email_shows_people_ahead_and_estimate(dbpath):
    services.issue_token("Fee Counter", path=dbpath, background=False)
    second = services.issue_token("Fee Counter", path=dbpath, background=False, notify_email="b@b.co")
    body = db.get_notification(second["notification_id"], dbpath)["body_text"]
    assert "People ahead of you : 1" in body and "about" in body and "you are next" not in body
