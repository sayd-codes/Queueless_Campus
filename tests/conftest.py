"""Test setup: use a throwaway database and never touch real SMTP settings."""
import os
import sys
import tempfile

# must happen before any `app` module is imported
os.environ["QUEUELESS_DB"] = os.path.join(tempfile.mkdtemp(), "tests.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

EMAIL_VARS = ["EMAIL_MODE", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
              "SMTP_SSL", "SMTP_STARTTLS", "APP_URL", "EMAIL_FROM_NAME"]


@pytest.fixture(autouse=True)
def clean_email_env(monkeypatch):
    """Every test starts in preview ('console') mode, whatever is in the developer's .env."""
    for v in EMAIL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("EMAIL_MODE", "console")


@pytest.fixture()
def dbpath(tmp_path):
    from app import db
    p = str(tmp_path / "t.db")
    db.init_db(p)
    return p
