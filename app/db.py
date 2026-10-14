
"""SQLite storage: tokens, users, sessions and notification logs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from . import analytics, config


SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    counter TEXT NOT NULL,
    token_no INTEGER NOT NULL,
    service_type TEXT DEFAULT 'general',
    issued_at TEXT NOT NULL,
    served_at TEXT,
    finished_at TEXT,
    source TEXT DEFAULT 'token',
    user_id INTEGER,
    notify_email TEXT
);

CREATE INDEX IF NOT EXISTS idx_tokens_counter_issued
ON tokens(counter, issued_at);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('user', 'admin')),
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id INTEGER NOT NULL,
    to_email TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('issued', 'served')),
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    body_html TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (token_id, event)
);
"""

NEW_TOKEN_COLUMNS = {
    "user_id": "INTEGER",
    "notify_email": "TEXT",
}

# Safe defaults are used when adding columns to an older database.
NEW_NOTIFICATION_COLUMNS = {
    "token_id": "INTEGER DEFAULT 0",
    "to_email": "TEXT DEFAULT ''",
    "event": "TEXT DEFAULT 'issued'",
    "subject": "TEXT DEFAULT ''",
    "body_text": "TEXT DEFAULT ''",
    "body_html": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "error": "TEXT",
    "created_at": "TEXT DEFAULT ''",
    "sent_at": "TEXT",
}


def _path(path: str | None) -> str:
    return path or config.DB_PATH


@contextmanager
def connect(path: str | None = None):
    conn = sqlite3.connect(_path(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _upgrade_tokens_table(conn):
    """Upgrade tokens created by older versions."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tokens)").fetchall()
    }

    for name, sql_type in NEW_TOKEN_COLUMNS.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE tokens ADD COLUMN {name} {sql_type}"
            )


def _upgrade_notifications_table(conn):
    """Add missing notification columns without deleting existing records."""
    existing = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(notifications)"
        ).fetchall()
    }

    for name, sql_type in NEW_NOTIFICATION_COLUMNS.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE notifications ADD COLUMN {name} {sql_type}"
            )


def init_db(path: str | None = None) -> None:
    """Create tables and upgrade existing databases safely."""
    with connect(path) as c:
        # Create missing tables first.
        c.executescript(SCHEMA)

        # Upgrade old tables before using their newer columns.
        _upgrade_tokens_table(c)
        _upgrade_notifications_table(c)

        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)"
        )


def _ts(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------
# TOKENS
# ------------------------------------------------------------------

def issue_token(
    counter: str,
    service_type: str = "general",
    issued_at=None,
    source: str = "token",
    path: str | None = None,
    user_id: int | None = None,
    notify_email: str | None = None,
) -> dict:
    """Issue the next token number for a counter each day."""
    issued = _ts(issued_at or datetime.now())

    with connect(path) as c:
        c.execute("BEGIN IMMEDIATE")

        n = c.execute(
            "SELECT COALESCE(MAX(token_no), 0) + 1 FROM tokens "
            "WHERE counter = ? AND date(issued_at) = date(?)",
            (counter, issued),
        ).fetchone()[0]

        cur = c.execute(
            "INSERT INTO tokens "
            "(counter, token_no, service_type, issued_at, source, "
            "user_id, notify_email) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                counter, n, service_type, issued, source,
                user_id, notify_email,
            ),
        )

        return {
            "id": cur.lastrowid,
            "counter": counter,
            "token_no": n,
            "issued_at": issued,
        }


def serve_token(
    token_id: int,
    served_at=None,
    path: str | None = None,
) -> bool:
    """Mark a token as served."""
    with connect(path) as c:
        cur = c.execute(
            "UPDATE tokens SET served_at = ? "
            "WHERE id = ? AND served_at IS NULL",
            (_ts(served_at or datetime.now()), token_id),
        )
        return cur.rowcount > 0


def finish_token(
    token_id: int,
    finished_at=None,
    path: str | None = None,
) -> bool:
    """Finish service after it has started."""
    with connect(path) as c:
        cur = c.execute(
            "UPDATE tokens SET finished_at = ? "
            "WHERE id = ? AND served_at IS NOT NULL "
            "AND finished_at IS NULL",
            (_ts(finished_at or datetime.now()), token_id),
        )
        return cur.rowcount > 0


def get_token(token_id: int, path: str | None = None) -> dict | None:
    """Get a token with its owner's name."""
    with connect(path) as c:
        r = c.execute(
            "SELECT t.*, u.name AS user_name FROM tokens t "
            "LEFT JOIN users u ON u.id = t.user_id "
            "WHERE t.id = ?",
            (token_id,),
        ).fetchone()

    return dict(r) if r else None


def queue_position(
    token_id: int,
    path: str | None = None,
) -> int | None:
    """Return the number of waiting tokens ahead of this token."""
    with connect(path) as c:
        t = c.execute(
            "SELECT counter, issued_at, served_at FROM tokens WHERE id = ?",
            (token_id,),
        ).fetchone()

        if t is None or t["served_at"] is not None:
            return None

        return c.execute(
            "SELECT COUNT(*) FROM tokens "
            "WHERE counter = ? AND date(issued_at) = date(?) "
            "AND served_at IS NULL "
            "AND (issued_at < ? OR (issued_at = ? AND id < ?))",
            (
                t["counter"], t["issued_at"],
                t["issued_at"], t["issued_at"], token_id,
            ),
        ).fetchone()[0]


def waiting_count(
    counter: str,
    path: str | None = None,
) -> int:
    """Count today's waiting tokens at a counter."""
    with connect(path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM tokens "
            "WHERE counter = ? AND served_at IS NULL "
            "AND source = 'token' "
            "AND date(issued_at) = date('now', 'localtime')",
            (counter,),
        ).fetchone()[0]


def active_token_count(
    user_id: int,
    counter: str,
    path: str | None = None,
) -> int:
    """Count a user's unserved tokens at a counter today."""
    with connect(path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM tokens "
            "WHERE user_id = ? AND counter = ? "
            "AND served_at IS NULL "
            "AND date(issued_at) = date('now', 'localtime')",
            (user_id, counter),
        ).fetchone()[0]


def load_tokens(path: str | None = None) -> pd.DataFrame:
    """Load all tokens with parsed timestamps."""
    with connect(path) as c:
        df = pd.read_sql_query(
            "SELECT * FROM tokens ORDER BY issued_at", c
        )

    for col in ("issued_at", "served_at", "finished_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    return df


def user_tokens(
    user_id: int,
    limit: int = 30,
    path: str | None = None,
) -> pd.DataFrame:
    """Load a user's tokens, newest first."""
    with connect(path) as c:
        df = pd.read_sql_query(
            "SELECT * FROM tokens WHERE user_id = ? "
            "ORDER BY issued_at DESC, id DESC LIMIT ?",
            c,
            params=(user_id, limit),
        )

    for col in ("issued_at", "served_at", "finished_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    return df


def insert_dataframe(
    df: pd.DataFrame,
    source: str = "upload",
    path: str | None = None,
) -> int:
    """Bulk insert tokens from a DataFrame."""
    analytics.require_columns(df)
    d = df.copy()

    for col in ("issued_at", "served_at", "finished_at"):
        if col in d:
            d[col] = pd.to_datetime(d[col], errors="coerce")
        else:
            d[col] = pd.NaT

    d = d.dropna(subset=["counter", "issued_at"])

    if "token_no" not in d:
        d = d.sort_values("issued_at")
        d["token_no"] = (
            d.groupby(["counter", d["issued_at"].dt.date]).cumcount() + 1
        )

    if "service_type" not in d:
        d["service_type"] = "general"

    if "source" not in d:
        d["source"] = source

    rows = [
        (
            str(r.counter),
            int(r.token_no),
            str(r.service_type),
            _ts(r.issued_at),
            _ts(r.served_at),
            _ts(r.finished_at),
            str(r.source),
        )
        for r in d.itertuples()
    ]

    with connect(path) as c:
        c.executemany(
            "INSERT INTO tokens "
            "(counter, token_no, service_type, issued_at, served_at, "
            "finished_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    return len(rows)


def clear_tokens(path: str | None = None) -> None:
    """Delete tokens and their notification log; keep user accounts."""
    with connect(path) as c:
        c.execute("DELETE FROM tokens")
        c.execute("DELETE FROM notifications")


# ------------------------------------------------------------------
# NOTIFICATIONS
# ------------------------------------------------------------------

def create_notification(
    token_id: int,
    to_email: str,
    event: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    status: str = "pending",
    path: str | None = None,
) -> int | None:
    """Record a notification, avoiding duplicates where possible."""
    try:
        with connect(path) as c:
            cur = c.execute(
                "INSERT INTO notifications "
                "(token_id, to_email, event, subject, body_text, "
                "body_html, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_id, to_email, event, subject, body_text,
                    body_html, status, _ts(datetime.now()),
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_notification(
    notification_id: int,
    status: str,
    error: str | None = None,
    path: str | None = None,
) -> None:
    """Update notification delivery status."""
    sent_at = (
        _ts(datetime.now())
        if status in ("sent", "logged")
        else None
    )

    with connect(path) as c:
        c.execute(
            "UPDATE notifications SET status = ?, error = ?, sent_at = ? "
            "WHERE id = ?",
            (status, error, sent_at, notification_id),
        )


def get_notification(
    notification_id: int,
    path: str | None = None,
) -> dict | None:
    """Get one notification."""
    with connect(path) as c:
        r = c.execute(
            "SELECT * FROM notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()

    return dict(r) if r else None


def list_notifications(
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 300,
    path: str | None = None,
) -> pd.DataFrame:
    """List notifications with token number and counter."""
    sql = (
        "SELECT n.id, n.created_at, n.sent_at, n.event, n.status, "
        "n.to_email, n.subject, n.error, t.token_no, t.counter "
        "FROM notifications n "
        "LEFT JOIN tokens t ON t.id = n.token_id "
        "WHERE 1 = 1"
    )

    params = []

    if user_id is not None:
        sql += " AND t.user_id = ?"
        params.append(user_id)

    if status:
        sql += " AND n.status = ?"
        params.append(status)

    sql += " ORDER BY n.id DESC LIMIT ?"
    params.append(limit)

    with connect(path) as c:
        return pd.read_sql_query(sql, c, params=params)


def notification_counts(path: str | None = None) -> dict:
    """Count notifications by status."""
    with connect(path) as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n "
            "FROM notifications GROUP BY status"
        ).fetchall()

    return {r["status"]: r["n"] for r in rows}
