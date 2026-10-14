"""Central configuration: counters, working hours, analysis defaults, accounts and email."""
import os


def _load_dotenv(path: str) -> None:
    """Tiny .env reader (KEY=VALUE per line). Real environment variables always win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


# Read <project>/.env (SMTP passwords etc.) before anything else uses the environment
_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# SQLite database file (override with the QUEUELESS_DB environment variable)
DB_PATH = os.environ.get("QUEUELESS_DB", "queueless.db")

# Working hours: counters are open OPEN_HOUR:00 to CLOSE_HOUR:00
OPEN_HOUR = 9
CLOSE_HOUR = 17

# Analysis defaults (all adjustable from the dashboard)
TARGET_WAIT_MIN = 10.0       # acceptable average wait in minutes
PEAK_SENSITIVITY = 0.75      # an hour is a peak if arrivals > mean + k * std
APPOINTMENT_SHARE = 0.6      # share of capacity reserved for booked appointments
TARGET_UTILIZATION = 0.85    # never plan a counter above 85% busy

# Sample counters used by the data generator and as dashboard defaults.
# "profile" = expected tokens per hour from OPEN_HOUR to CLOSE_HOUR - 1.
COUNTERS = {
    "Fee Counter": {
        "staff": 3,
        "service_min": 4.0,
        "service_types": ["Fee payment", "Fee receipt", "Scholarship query"],
        "profile": [20, 45, 60, 35, 15, 25, 50, 30],
    },
    "Examination Cell": {
        "staff": 2,
        "service_min": 4.0,
        "service_types": ["Admit card", "Result query", "Re-evaluation"],
        "profile": [10, 20, 30, 40, 20, 15, 25, 12],
    },
    "Admin Office": {
        "staff": 3,
        "service_min": 6.0,
        "service_types": ["Bonafide certificate", "Transfer certificate", "ID card"],
        "profile": [15, 25, 28, 30, 22, 20, 26, 18],
    },
}

# Busier at the start of the week (0 = Monday)
WEEKDAY_FACTOR = {0: 1.15, 1: 1.0, 2: 0.95, 3: 1.0, 4: 0.9}

# Fallbacks for counters that appear in uploaded data but not in COUNTERS
DEFAULT_STAFF = 2
DEFAULT_SERVICE_MIN = 5.0

# ----------------------------------------------------------------- accounts
MAX_ACTIVE_TOKENS_PER_COUNTER = 1   # unserved tokens one user may hold per counter today
MIN_PASSWORD_LENGTH = 8
SESSION_HOURS = 12                  # how long an API login stays valid
LOGIN_MAX_FAILURES = 5              # wrong passwords allowed ...
LOGIN_LOCK_SECONDS = 300            # ... before that email is locked for this long


# ----------------------------------------------------------------- email
def email_settings() -> dict:
    """Email settings, read from environment variables / .env on every call.

    EMAIL_MODE = smtp     -> really send through SMTP_HOST
                 console  -> don't send; save every email in the database (preview mode)
                 off      -> no notifications at all
    If EMAIL_MODE is empty: 'smtp' when SMTP_HOST and a sender are set, otherwise 'console'.
    """
    env = os.environ.get
    host = env("SMTP_HOST", "").strip()
    user = env("SMTP_USER", "").strip()
    sender = env("SMTP_FROM", "").strip() or user
    try:
        port = int(env("SMTP_PORT", "587") or 587)
    except ValueError:
        port = 587
    mode = env("EMAIL_MODE", "").strip().lower()
    if mode not in ("smtp", "console", "off"):
        mode = "smtp" if (host and sender) else "console"
    if mode == "smtp" and not (host and sender):
        mode = "console"
    use_ssl = port == 465 or env("SMTP_SSL", "").strip().lower() in ("1", "true", "yes")
    use_tls = (not use_ssl) and env("SMTP_STARTTLS", "true").strip().lower() in ("1", "true", "yes")
    return {
        "mode": mode, "host": host, "port": port, "user": user,
        "password": env("SMTP_PASSWORD", ""), "sender": sender,
        "from_name": env("EMAIL_FROM_NAME", "QueueLess Campus"),
        "use_ssl": use_ssl, "use_tls": use_tls,
        "app_url": env("APP_URL", "").strip(),
    }
