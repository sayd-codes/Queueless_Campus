# Email notifications, user accounts and admin interface

Version 2 adds three things to QueueLess Campus:

1. **Accounts with two roles** – `user` (students/staff) and `admin` (office staff).
2. **Email notifications** – an email when a token is **issued** and another when it is **served** (the person's turn).
3. **Two interfaces** – after login, users see a simple token screen; admins see the analytics dashboard plus queue control, an email log and user management.

No new libraries are needed. Email uses Python's built-in `smtplib`; passwords use built-in PBKDF2.

---

## 1. How it works

```
User takes a token ──► services.issue_token() ──► db (tokens table)
                                 │
                                 └─► notifier.notify("issued") ──► notifications table ──► SMTP / preview

Admin presses "Serve" ──► services.serve_token() ──► db (served_at set)
                                 │
                                 └─► notifier.notify("served") ──► notifications table ──► SMTP / preview
```

* Every email is **saved first** in the `notifications` table, then delivered in a background thread, so the web page never waits for the mail server.
* A token gets **at most one email per event** (no duplicates on double clicks).
* If delivery fails (wrong password, no internet) the token still works; the email is marked `failed` with the reason, and an admin can press **Send this email again**.
* **Preview mode:** while SMTP is not configured nothing is sent. Emails are stored with status `logged` and can be read in *Admin → Notifications*. This is how the demo works with zero setup.

| Who | Sees | Can do |
|---|---|---|
| Not logged in | Login / Create account | Register (always as a normal user) |
| **user** | Get a token · My tokens · My emails | Take a token (max 1 waiting token per counter), watch queue position, read own emails |
| **admin** | Overview · Peak hours · Recommendations · Live queue & entry · Notifications · Users · Data | Everything users can't: serve/finish tokens, walk-in tokens with email, analytics, email log + retry + test email, create accounts, promote/demote users |

---

## 2. Files: what is new and what changed

| File | Status | Purpose |
|---|---|---|
| `app/config.py` | **replace** | Reads `.env`; new `email_settings()`; account limits |
| `app/db.py` | **replace** | New tables (`users`, `sessions`, `notifications`), new token columns `user_id`, `notify_email`; upgrades an old database automatically |
| `app/auth.py` | **new** | Register, login, roles, password hashing, login lockout, API sessions |
| `app/notifier.py` | **new** | Builds the emails, sends via SMTP, logs every email, retry, test email |
| `app/services.py` | **new** | `issue_token()` / `serve_token()` – the two functions that trigger emails |
| `app/queueinfo.py` | **new** | "People ahead" and estimated wait |
| `ui/__init__.py`, `ui/common.py` | **new** | Shared helpers |
| `ui/auth_ui.py` | **new** | Login / register screen, logout |
| `ui/user_view.py` | **new** | The user interface |
| `ui/admin_view.py` | **new** | The admin interface (your old dashboard + new tabs) |
| `dashboard.py` | **replace** | Now a small router: login → user or admin screen |
| `api.py` | **replace** | Login-protected API with roles |
| `create_admin.py` | **new** | Command-line tool to create the first admin |
| `.env.example`, `.gitignore` | **new / edit** | Email settings template; `.env` is never committed |
| `tests/` | **new files** | `conftest.py`, `test_notifications.py`, `test_api.py`, `test_ui.py` |

`app/analytics.py`, `app/recommender.py`, `app/seed.py`, `run_demo.py` and `requirements.txt` are **unchanged**.

---

## 3. Setup (Windows, VS Code)

### Step 1 – Put the new files in your project
Copy the new project over your old one (replace when asked). Keep your own `.venv` folder and `queueless.db`: your existing data is upgraded automatically on first start.

### Step 2 – Create your first admin
In the VS Code terminal (with `(.venv)` active):
```
python create_admin.py
```
Enter email, name and a password (min 8 characters).

### Step 3 – Run the app in preview mode (no email setup needed)
```
streamlit run dashboard.py
```
1. Log in as the admin → **Load sample data** if you want analytics.
2. Open a **private/incognito window** → *Create account* as a student → **Get token**.
3. Back in the admin window → **Live queue & entry** → press **Serve**.
4. Admin → **Notifications** shows both emails (status *Preview only*). The student's **My emails** tab shows them too.

### Step 4 – Send real emails (Gmail example)
1. In your Google account turn on **2-Step Verification** (Security page).
2. Open **myaccount.google.com/apppasswords**, create an app password (name it *QueueLess*). Google shows 16 characters – copy them.
3. In the project folder: `copy .env.example .env`, then open `.env` in VS Code and set:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=yourname@gmail.com
   SMTP_PASSWORD=the16characterapppassword
   ```
4. Stop the app (Ctrl+C) and start it again. Admin → Notifications now shows a green *"Sending real emails through smtp.gmail.com"* banner.
5. Use **Send a test email** (same tab) with your own address.

Other providers: Outlook/Microsoft 365 `smtp-mail.outlook.com` port 587 (an admin may need to allow SMTP AUTH); college mail – ask IT for the SMTP host, port and whether STARTTLS or SSL is used (set `SMTP_PORT=465` for SSL).

### Step 5 – (Optional) the API
```
uvicorn api:app --reload
```
Open `http://127.0.0.1:8000/docs` → `POST /auth/login` → copy `access_token` → click **Authorize** → paste it.

---

## 4. Deploying (Streamlit Community Cloud)

* Put the email settings in **App settings → Secrets** instead of `.env`:
  ```toml
  SMTP_HOST = "smtp.gmail.com"
  SMTP_PORT = "587"
  SMTP_USER = "yourname@gmail.com"
  SMTP_PASSWORD = "the16characterapppassword"
  APP_URL = "https://your-app.streamlit.app"
  ```
* Never commit `.env` or passwords to GitHub.
* Streamlit Cloud storage is temporary: accounts and tokens vanish when the app restarts. For real use, host it on a machine with a persistent disk or move to PostgreSQL.

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| "No admin account exists yet" | Run `python create_admin.py` |
| Notifications tab says *Preview mode* | `.env` missing, or `SMTP_USER`/`SMTP_PASSWORD` empty, or app not restarted after editing |
| Status **failed**: `SMTPAuthenticationError` | Wrong password. For Gmail use an **App password**, not your normal password |
| Status **failed**: `timed out` / `Connection refused` | Wrong host/port, or the network blocks SMTP. Try port 465 (SSL) |
| Email arrives in Spam | Normal for new senders; mark "Not spam" once. A college SMTP server is usually trusted more than Gmail |
| "You already have a waiting token" | Limit of 1 waiting token per counter per user. Change `MAX_ACTIVE_TOKENS_PER_COUNTER` in `app/config.py` |
| Locked out after wrong passwords | Wait 5 minutes (`LOGIN_LOCK_SECONDS`) |
| Forgot the only admin password | Run `python create_admin.py` with a new email, or promote another account |

## 6. Good to know (limits)

* **Refreshing the browser tab logs you out** (Streamlit keeps the login only while the page stays open). Persistent login would need a cookie component.
* The estimated wait is a rough guess: `people ahead × service time ÷ staff` from `app/config.py`.
* Gmail allows roughly 500 emails per day for a normal account.
* Wrong-password lockout is remembered in memory, so it resets when the app restarts.
* Emails are sent from a background thread; if the app is stopped in the middle of a send, that email stays *Sending* – use *Send this email again*.
* Anyone can create a `user` account with any email address (no email verification yet). That is fine for a campus pilot; add an email-confirmation step before wider use.
