"""FastAPI backend for QueueLess Campus (with accounts, roles and email notifications).

Run:  uvicorn api:app --reload      then open http://127.0.0.1:8000/docs

Log in with POST /auth/login, copy the access_token, click "Authorize" on the /docs page and
paste it. Routes marked [user] need any account, routes marked [admin] need an admin account.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app import analytics, auth, config, db, notifier, queueinfo, recommender, seed, services

db.init_db()
app = FastAPI(title="QueueLess Campus API", version="2.0.0",
              description="Tokens, e-mail notifications, waiting-time analytics and scheduling suggestions.")
bearer = HTTPBearer(auto_error=False)


# ----------------------------------------------------------------------------- models
class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


class TokenIn(BaseModel):
    counter: str
    service_type: str = "general"
    notify: bool = True


class WalkInIn(BaseModel):
    counter: str
    service_type: str = "general"
    notify_email: Optional[str] = None


class ManualObservation(BaseModel):
    """A voluntary observation: when someone arrived and how long they waited."""
    counter: str
    arrived_at: datetime
    waited_min: float
    service_type: str = "general"


class RoleIn(BaseModel):
    role: str


class TestMailIn(BaseModel):
    to: str


# ----------------------------------------------------------------------------- auth helpers
def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    user = auth.user_from_session(creds.credentials) if creds else None
    if user is None:
        raise HTTPException(401, "Please log in.", headers={"WWW-Authenticate": "Bearer"})
    return user


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required.")
    return user


def _records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict(orient="records")


def _stringify_times(df: pd.DataFrame) -> pd.DataFrame:
    return df.astype({c: str for c in ("issued_at", "served_at", "finished_at") if c in df})


def _prepared() -> pd.DataFrame:
    return analytics.prepare(db.load_tokens())


# ----------------------------------------------------------------------------- public
@app.get("/health")
def health():
    return {"status": "ok", "email_mode": config.email_settings()["mode"]}


@app.post("/auth/register")
def register(body: RegisterIn):
    """Create a normal user account and log in."""
    try:
        user = auth.create_user(body.email, body.password, body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"user": user, "access_token": auth.create_session(user["id"]), "token_type": "bearer"}


@app.post("/auth/login")
def login(body: LoginIn):
    try:
        user = auth.authenticate(body.email, body.password)
    except auth.AuthLocked as exc:
        raise HTTPException(429, str(exc))
    if user is None:
        raise HTTPException(401, "Wrong email or password.")
    return {"user": user, "access_token": auth.create_session(user["id"]), "token_type": "bearer"}


@app.post("/auth/logout")
def logout(creds: HTTPAuthorizationCredentials | None = Depends(bearer), _: dict = Depends(current_user)):
    auth.end_session(creds.credentials)
    return {"status": "logged out"}


# ----------------------------------------------------------------------------- [user]
@app.get("/me")
def me(user: dict = Depends(current_user)):
    return user


@app.post("/tokens")
def take_token(body: TokenIn, user: dict = Depends(current_user)):
    """[user] Take a token. Sends the 'token issued' email to the account's address."""
    try:
        return services.issue_token(body.counter, body.service_type, user=user, notify=body.notify)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.get("/me/tokens")
def my_tokens(user: dict = Depends(current_user)):
    """[user] My tokens with live position in the queue."""
    df = db.user_tokens(user["id"])
    out = _records(_stringify_times(df))
    for row in out:
        row.update(queueinfo.snapshot(row["id"]))
    return out


@app.get("/me/notifications")
def my_notifications(user: dict = Depends(current_user)):
    """[user] Emails sent (or previewed) for my tokens."""
    return _records(db.list_notifications(user_id=user["id"], limit=100))


@app.get("/queue/status")
def queue_status(_: dict = Depends(current_user)):
    """[user] People waiting and estimated wait for a newcomer, per counter."""
    return [queueinfo.counter_status(c) for c in config.COUNTERS]


# ----------------------------------------------------------------------------- [admin] queue control
@app.post("/admin/tokens")
def issue_walk_in(body: WalkInIn, _: dict = Depends(admin_only)):
    """[admin] Issue a token for a walk-in; optional email gets the same notifications."""
    try:
        return services.issue_token(body.counter, body.service_type, notify_email=body.notify_email)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/tokens/{token_id}/serve")
def serve_token(token_id: int, _: dict = Depends(admin_only)):
    """[admin] Call a token. Sends the 'it's your turn' email."""
    if not services.serve_token(token_id):
        raise HTTPException(404, "Token not found or already served")
    return {"id": token_id, "status": "serving"}


@app.post("/tokens/{token_id}/finish")
def finish_token(token_id: int, _: dict = Depends(admin_only)):
    if not db.finish_token(token_id):
        raise HTTPException(404, "Token not found, not served yet, or already finished")
    return {"id": token_id, "status": "finished"}


@app.post("/observations")
def add_observation(body: ManualObservation, _: dict = Depends(admin_only)):
    """[admin] Add a voluntary manual observation (stored with source='manual', no emails)."""
    if body.waited_min < 0:
        raise HTTPException(422, "waited_min must be >= 0")
    t = db.issue_token(body.counter, body.service_type, body.arrived_at, source="manual")
    db.serve_token(t["id"], pd.Timestamp(body.arrived_at) + pd.Timedelta(minutes=body.waited_min))
    return t


@app.get("/tokens")
def list_tokens(limit: int = 100, _: dict = Depends(admin_only)):
    return _records(_stringify_times(db.load_tokens().tail(limit)))


@app.get("/queue")
def queue(_: dict = Depends(admin_only)):
    """[admin] Tokens currently waiting for service."""
    df = db.load_tokens()
    return _records(_stringify_times(df[df["served_at"].isna()]))


# ----------------------------------------------------------------------------- [admin] analytics
@app.get("/analytics/summary")
def summary(_: dict = Depends(admin_only)):
    return _records(analytics.waiting_summary(_prepared()))


@app.get("/analytics/hourly")
def hourly(sensitivity: float = config.PEAK_SENSITIVITY, _: dict = Depends(admin_only)):
    hp = analytics.detect_peaks(analytics.hourly_stats(_prepared()), sensitivity)
    return _records(hp.round(2))


@app.get("/recommendations")
def recommendations(target_wait: float = config.TARGET_WAIT_MIN,
                    appointment_share: float = config.APPOINTMENT_SHARE,
                    sensitivity: float = config.PEAK_SENSITIVITY, _: dict = Depends(admin_only)):
    df = _prepared()
    hp = analytics.detect_peaks(analytics.hourly_stats(df), sensitivity)
    return recommender.build_recommendations(df, hp, None, target_wait, appointment_share)


# ----------------------------------------------------------------------------- [admin] notifications & users
@app.get("/admin/notifications")
def list_notifications(status: Optional[str] = None, limit: int = 200, _: dict = Depends(admin_only)):
    return {"counts": db.notification_counts(),
            "items": _records(db.list_notifications(status=status, limit=limit))}


@app.post("/admin/notifications/{notification_id}/retry")
def retry_notification(notification_id: int, _: dict = Depends(admin_only)):
    try:
        return {"id": notification_id, "status": notifier.retry(notification_id)}
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/admin/notifications/test")
def test_email(body: TestMailIn, _: dict = Depends(admin_only)):
    if not auth.EMAIL_RE.match(auth.normalize_email(body.to)):
        raise HTTPException(422, "Please enter a valid email address.")
    ok, message = notifier.send_test(auth.normalize_email(body.to))
    return {"ok": ok, "message": message}


@app.get("/admin/users")
def users(_: dict = Depends(admin_only)):
    return auth.list_users()


@app.patch("/admin/users/{user_id}/role")
def change_role(user_id: int, body: RoleIn, admin: dict = Depends(admin_only)):
    if user_id == admin["id"]:
        raise HTTPException(422, "You cannot change your own role.")
    try:
        return auth.set_role(user_id, body.role)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ----------------------------------------------------------------------------- [admin] data
@app.post("/seed")
def seed_data(days: int = 5, reset: bool = False, _: dict = Depends(admin_only)):
    """[admin] Load generated sample data (optionally wiping existing tokens first)."""
    if reset:
        db.clear_tokens()
    return {"inserted": db.insert_dataframe(seed.generate_sample(days=days), source="sample")}


@app.post("/upload")
async def upload_csv(file: UploadFile = File(...), _: dict = Depends(admin_only)):
    """[admin] Import a CSV with columns counter, issued_at, served_at (optional: token_no, finished_at)."""
    try:
        df = pd.read_csv(io.BytesIO(await file.read()))
        n = db.insert_dataframe(df, source="upload")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"inserted": n}


@app.delete("/tokens")
def clear(_: dict = Depends(admin_only)):
    db.clear_tokens()
    return {"status": "cleared"}
