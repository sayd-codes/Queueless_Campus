"""The interface a normal user (student / staff member) sees."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app import analytics, config, db, queueinfo, services
from ui.common import flash, show_flash

STATUS_LABEL = {"sent": "✅ Delivered", "logged": "👁️ Preview only", "failed": "⚠️ Not delivered",
                "pending": "⏳ Sending"}


def render(user: dict) -> None:
    st.title("Get your token")
    show_flash()
    tab_get, tab_mine, tab_mail = st.tabs(["🎟️ Get a token", "📍 My tokens", "📧 My emails"])
    with tab_get:
        _get_token(user)
    with tab_mine:
        _my_tokens(user)
    with tab_mail:
        _my_emails(user)


# --------------------------------------------------------------------------- get a token
def _quiet_hours(counter: str) -> str | None:
    """Usually-quietest hours at a counter, from stored history."""
    df = analytics.prepare(db.load_tokens())
    df = df[df["counter"] == counter]
    if df.empty:
        return None
    h = analytics.hourly_stats(df).nsmallest(2, "avg_arrivals")["hour"].tolist()
    return ", ".join(f"{x:02d}:00" for x in sorted(h))


def _get_token(user: dict) -> None:
    counters = list(config.COUNTERS)
    st.subheader("Right now")
    cols = st.columns(len(counters))
    for col, c in zip(cols, counters):
        s = queueinfo.counter_status(c)
        col.metric(c, f"~{s['est_wait_min']} min", help=f"{s['waiting']} people are waiting")
        col.caption(f"{s['waiting']} waiting")

    st.subheader("Take a token")
    counter = st.selectbox("Counter", counters, key="user_counter")
    quiet = _quiet_hours(counter)
    if quiet:
        st.caption(f"💡 Usually quietest at {quiet}.")
    with st.form("user_token_form"):
        service = st.selectbox("What do you need?", config.COUNTERS[counter]["service_types"])
        email_me = st.checkbox(f"Email me updates at {user['email']}", value=True)
        submitted = st.form_submit_button("Get token", type="primary")
    if submitted:
        try:
            t = services.issue_token(counter, service, user=user, notify=email_me)
        except ValueError as exc:
            st.error(str(exc))
        else:
            tail = f" A confirmation email is on its way to {t['notify_email']}." if t["notify_email"] else ""
            flash(f"Token #{t['token_no']} issued at {counter}. People ahead of you: {t['ahead']}.{tail}")


# --------------------------------------------------------------------------- my tokens
@st.fragment(run_every=10)
def _my_tokens_live(user: dict) -> None:
    """Re-runs every 10 seconds on its own, so status changes appear without a page reload."""
    df = db.user_tokens(user["id"], limit=15)
    if df.empty:
        st.info("You have not taken a token yet.")
        return
    for r in df.itertuples():
        with st.container(border=True):
            st.markdown(f"**#{r.token_no} · {r.counter}** — {r.service_type}")
            if pd.isna(r.served_at):
                ahead = db.queue_position(int(r.id))
                est = queueinfo.estimate_wait_min(r.counter, ahead or 0)
                st.markdown(f"⏳ **Waiting** · {ahead} ahead of you · about {est} min")
            elif pd.isna(r.finished_at):
                st.markdown(f"🔔 **It's your turn!** Please go to the **{r.counter}** now.")
            else:
                st.markdown("✅ **Completed**")
            st.caption(f"Issued {r.issued_at:%d %b, %H:%M}")


def _my_tokens(user: dict) -> None:
    st.caption("This list refreshes by itself every few seconds.")
    _my_tokens_live(user)


# --------------------------------------------------------------------------- my emails
def _my_emails(user: dict) -> None:
    if config.email_settings()["mode"] != "smtp":
        st.warning("Email delivery is not switched on for this server yet, so your notifications "
                   "appear only in this list.")
    log = db.list_notifications(user_id=user["id"], limit=50)
    if log.empty:
        st.info("No notifications yet. They appear here when you take a token.")
        return
    log["status"] = log["status"].map(lambda s: STATUS_LABEL.get(s, s))
    log["when"] = pd.to_datetime(log["created_at"]).dt.strftime("%d %b, %H:%M")
    st.dataframe(log[["when", "subject", "to_email", "status"]], hide_index=True, width="stretch")
