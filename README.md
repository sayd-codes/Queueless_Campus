# QueueLess Campus – Admin Office Waiting-Time Analyzer

Reduces unnecessary waiting at the **fee counter, examination cell and administration office**.
It records token timestamps (or voluntary manual observations), calculates waiting times,
finds **peak-hour patterns**, and suggests **staffing, token allocation and appointment scheduling** changes.

**New in v2:** user and admin accounts, and **email notifications** when a token is issued and when it is
served. See [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md) for setup.

```
token issued ──► service starts ──► (service ends)
issued_at         served_at          finished_at        wait = served_at − issued_at
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run_demo.py            # 1) CLI demo: average waits + peak-hour chart in outputs/
python create_admin.py        # 2) create the first admin account (once)
streamlit run dashboard.py    # 3) login + user/admin interface (http://localhost:8501)
uvicorn api:app --reload      # 4) REST API + docs        (http://127.0.0.1:8000/docs)
python -m pytest              # 5) run the tests
```

Log in as the admin, then click **Load sample data** in the sidebar to get started.
Students create their own account on the login screen. Emails run in *preview mode* until you add SMTP settings (`.env`).
The dashboard and the API share one SQLite file (`queueless.db`, change with the `QUEUELESS_DB` env variable).

## Project structure

```
queueless_campus/
├── app/
│   ├── config.py        counters, working hours, defaults, email settings (.env)
│   ├── db.py            SQLite storage: tokens, users, sessions, notification log
│   ├── auth.py          register / login / roles / password hashing
│   ├── notifier.py      builds and sends the emails, logs every email
│   ├── services.py      issue_token() / serve_token() -> these trigger the emails
│   ├── queueinfo.py     people ahead + estimated wait
│   ├── analytics.py     cleaning, summaries, hourly stats, peak detection, Erlang-C staffing
│   ├── recommender.py   peak-window actions, appointment/token plan, demand re-balancing
│   └── seed.py          realistic sample-data simulator
├── ui/                  Streamlit screens: auth_ui, user_view, admin_view
├── dashboard.py         login + router to the user or admin interface
├── api.py               FastAPI backend (login-protected, role based)
├── create_admin.py      create the first admin
├── run_demo.py          command-line demo, saves chart
├── .env.example         email (SMTP) settings template
├── docs/NOTIFICATIONS.md  setup guide for accounts and email
├── tests/               unit, API and UI tests
└── requirements.txt
```

## Using your own data

Import a CSV (dashboard sidebar, `POST /upload`, or `python run_demo.py --csv file.csv`) with:

| column | required | example |
|---|---|---|
| `counter` | yes | `Fee Counter` |
| `issued_at` | yes | `2026-10-09 11:05:00` |
| `served_at` (service start) | yes | `2026-10-09 11:21:00` |
| `finished_at` | no | `2026-10-09 11:25:00` |
| `token_no`, `service_type` | no | `42`, `Fee receipt` |

Without a token machine, use the **Live queue & entry** tab: staff press *Serve* when they call the next person,
or anyone can add a **manual observation** (arrival time + minutes waited). Manual rows are stored with
`source = manual` so they can be told apart from system tokens.

## How the analysis works

1. **Waiting time** = `served_at − issued_at`. Unserved tokens and negative waits are dropped.
2. **Summary**: average, median, 90th percentile and maximum wait per counter. (Averages hide long waits, so the 90th percentile matters.)
3. **Peak hours**: average tokens per hour on a working day. An hour is a *peak* when it exceeds `mean + k × std` of that counter's hours (`k` = sensitivity slider). Consecutive peaks are merged into windows such as 11:00–13:00.
4. **Staffing**: Erlang-C queueing model finds the fewest staff that keep the expected wait under the target.
5. **Recommendations** per peak window: add temporary staff, cap walk-in tokens, release appointment slots, move flexible requests to quiet hours, and open a fast lane when service times vary a lot.
6. **Appointment plan**: hourly capacity (at 85 % utilisation) split into appointment slots and walk-in tokens. Overflow demand is moved to the nearest hours with spare capacity.

## REST API

Log in with `POST /auth/login`, then send `Authorization: Bearer <access_token>`.

| method | path | who | purpose |
|---|---|---|---|
| POST | `/auth/register`, `/auth/login`, `/auth/logout` | public / user | accounts and sessions |
| GET | `/me`, `/me/tokens`, `/me/notifications`, `/queue/status` | user | own profile, tokens with queue position, own emails, queue status |
| POST | `/tokens` | user | take a token (sends the *issued* email) |
| POST | `/admin/tokens` | admin | walk-in token, optional `notify_email` |
| POST | `/tokens/{id}/serve` | admin | call a token (sends the *your turn* email) |
| POST | `/tokens/{id}/finish`, `/observations` | admin | finish service / manual observation |
| GET | `/tokens`, `/queue`, `/analytics/summary`, `/analytics/hourly`, `/recommendations` | admin | data and analytics |
| GET/POST | `/admin/notifications`, `/admin/notifications/{id}/retry`, `/admin/notifications/test` | admin | email log, retry, test email |
| GET/PATCH | `/admin/users`, `/admin/users/{id}/role` | admin | list users, promote / demote |
| POST/DELETE | `/seed`, `/upload`, `/tokens` | admin | load sample data, import CSV, clear |

## Roadmap ideas

- QR code to take a token; SMS/WhatsApp alerts and an "almost your turn" email.
- Demand forecasting per hour (Prophet / gradient boosting) with an exam and fee-deadline calendar.
- Email verification for new accounts, persistent login (cookie), PostgreSQL for multi-user deployment, Docker.
- Before/after pilot report to measure improvement at one counter.

## Privacy

Accounts store a name, email and a salted password hash (never the password). Tokens store counter, service and timestamps, plus the email used for notifications. Tell users that wait times and notification emails are recorded.
