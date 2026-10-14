"""The interface an admin sees: analytics (the original dashboard) + queue control,
email notification management and user management."""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import plotly.express as px
import streamlit as st

from app import analytics, auth, config, db, notifier, recommender, seed, services
from ui.common import flash, show_flash

SEVERITY_ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}
NO_DATA = "No data yet. Use **Load sample data** or **Import CSV** in the sidebar."


def render(user: dict) -> None:
    _sidebar_data()
    raw = db.load_tokens()
    df_all = analytics.prepare(raw)
    filters = _sidebar_filters(df_all)
    show_flash()

    df = df_all
    if not df_all.empty:
        df = df_all[df_all["counter"].isin(filters["counters"])
                    & (df_all["date"] >= filters["d_from"]) & (df_all["date"] <= filters["d_to"])]
    hp = analytics.detect_peaks(analytics.hourly_stats(df), filters["sensitivity"])

    tabs = st.tabs(["📊 Overview", "⏰ Peak hours", "💡 Recommendations", "🎟️ Live queue & entry",
                    "📧 Notifications", "👥 Users", "🗂️ Data"])
    with tabs[0]:
        _overview(df, filters)
    with tabs[1]:
        _peak_hours(df, hp, filters)
    with tabs[2]:
        _recommendations(df, hp, filters)
    with tabs[3]:
        _live_queue(raw, df_all)
    with tabs[4]:
        _notifications()
    with tabs[5]:
        _users(user)
    with tabs[6]:
        _data(raw)


# --------------------------------------------------------------------------- sidebar
def _sidebar_data() -> None:
    with st.sidebar:
        st.subheader("Data")
        n_days = st.number_input("Sample days", 1, 30, 5)
        if st.button("Load sample data", width="stretch"):
            n = db.insert_dataframe(seed.generate_sample(days=int(n_days)), source="sample")
            flash(f"Loaded {n} sample tokens.")
        up = st.file_uploader("Import CSV (counter, issued_at, served_at)", type="csv")
        if up is not None and st.button("Import CSV", width="stretch"):
            try:
                flash(f"Imported {db.insert_dataframe(pd.read_csv(up))} tokens.")
            except ValueError as e:
                st.error(str(e))
        if st.button("Clear all tokens", width="stretch",
                     help="Deletes tokens and the email log. User accounts are kept."):
            db.clear_tokens()
            flash("All tokens and notifications cleared.")


def _sidebar_filters(df_all: pd.DataFrame) -> dict:
    out = {}
    with st.sidebar:
        st.subheader("Filters")
        all_counters = sorted(df_all["counter"].unique()) if not df_all.empty else []
        out["counters"] = st.multiselect("Counters", all_counters, default=all_counters)
        if not df_all.empty:
            dmin, dmax = df_all["date"].min(), df_all["date"].max()
            rng = st.date_input("Date range", value=(dmin, dmax), min_value=dmin, max_value=dmax)
            out["d_from"], out["d_to"] = rng if isinstance(rng, tuple) and len(rng) == 2 else (dmin, dmax)
        st.subheader("Analysis settings")
        out["target_wait"] = st.slider("Target average wait (min)", 2, 30, int(config.TARGET_WAIT_MIN))
        out["sensitivity"] = st.slider("Peak sensitivity (lower = more peaks)", 0.0, 2.0,
                                       float(config.PEAK_SENSITIVITY), 0.05)
        out["appt_share"] = st.slider("Appointment share of capacity", 0.0, 1.0,
                                      float(config.APPOINTMENT_SHARE), 0.05)
        with st.expander("Staff per counter"):
            out["staff"] = {c: int(st.number_input(
                c, 1, 20, int(config.COUNTERS.get(c, {}).get("staff", config.DEFAULT_STAFF)),
                key=f"staff_{c}")) for c in sorted(set(all_counters) | set(config.COUNTERS))}
    return out


# --------------------------------------------------------------------------- analytics tabs
def _overview(df: pd.DataFrame, f: dict) -> None:
    if df.empty:
        st.info(NO_DATA)
        return
    target = f["target_wait"]
    within = (df["wait_min"] <= target).mean() * 100
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Tokens analysed", f"{len(df):,}")
    k2.metric("Average wait", f"{df['wait_min'].mean():.1f} min")
    k3.metric("90% of people wait under", f"{df['wait_min'].quantile(0.9):.1f} min")
    k4.metric(f"Served within {target} min", f"{within:.0f}%")

    summary = analytics.waiting_summary(df)
    st.subheader("Waiting time by counter")
    st.dataframe(summary, hide_index=True, width="stretch")
    per = summary[summary["counter"] != "All counters"]
    fig = px.bar(per, x="counter", y=["avg_wait", "p90_wait"], barmode="group",
                 labels={"value": "Minutes", "variable": "Metric", "counter": ""})
    fig.add_hline(y=target, line_dash="dash", annotation_text="target")
    st.plotly_chart(fig, width="stretch")


def _peak_hours(df: pd.DataFrame, hp: pd.DataFrame, f: dict) -> None:
    if df.empty:
        st.info(NO_DATA)
        return
    st.subheader("Tokens per hour (average working day)")
    plot = hp.copy()
    plot["status"] = plot["is_peak"].map({True: "Peak", False: "Normal"})
    fig = px.bar(plot, x="hour", y="avg_arrivals", color="status", facet_col="counter",
                 facet_col_wrap=3, color_discrete_map={"Peak": "#d62728", "Normal": "#1f77b4"},
                 labels={"avg_arrivals": "Avg tokens", "hour": "Hour of day"})
    st.plotly_chart(fig, width="stretch")

    st.subheader("Average wait by hour")
    fig = px.line(hp, x="hour", y="avg_wait", color="counter", markers=True,
                  labels={"avg_wait": "Avg wait (min)", "hour": "Hour of day"})
    fig.add_hline(y=f["target_wait"], line_dash="dash", annotation_text="target")
    st.plotly_chart(fig, width="stretch")

    st.subheader("Busy periods")
    win = analytics.peak_windows(hp)
    if win.empty:
        st.write("No peak hours detected with the current sensitivity.")
    else:
        win["window"] = win.apply(lambda r: f"{r.start_hour:02d}:00-{r.end_hour:02d}:00", axis=1)
        st.dataframe(win[["counter", "window"]], hide_index=True)

    st.subheader("Weekday x hour heatmap")
    sel = st.selectbox("Counter", ["All counters"] + sorted(df["counter"].unique()))
    m = analytics.weekday_hour_matrix(df, sel)
    if not m.empty:
        fig = px.imshow(m, aspect="auto", color_continuous_scale="OrRd",
                        labels=dict(x="Hour", y="Weekday", color="Avg tokens"))
        st.plotly_chart(fig, width="stretch")


def _recommendations(df: pd.DataFrame, hp: pd.DataFrame, f: dict) -> None:
    if df.empty:
        st.info(NO_DATA)
        return
    staff, appt_share = f["staff"], f["appt_share"]
    recs = recommender.build_recommendations(df, hp, staff, f["target_wait"], appt_share)
    st.subheader("Suggested actions for peak windows")
    if not recs:
        st.success("No peak windows detected: workload is evenly spread.")
    for r in recs:
        with st.container(border=True):
            st.markdown(f"### {SEVERITY_ICON[r['severity']]} {r['counter']} · {r['window']} "
                        f"· {r['severity']} priority")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Avg wait", f"{r['avg_wait_min']} min")
            c2.metric("Tokens / hour", r["arrivals_per_hr"])
            c3.metric("Staff now → needed", f"{r['current_staff']} → {r['recommended_staff']}")
            c4.metric("Appointment slots / hr", r["appointment_slots_per_hr"])
            for a in r["actions"]:
                st.markdown(f"- {a}")
    if recs:
        st.download_button("Download recommendations (CSV)",
                           recommender.recommendations_frame(recs).to_csv(index=False),
                           "recommendations.csv", "text/csv")

    st.subheader("Appointment & token plan")
    c = st.selectbox("Counter", sorted(df["counter"].unique()), key="plan_counter")
    svc = analytics.estimate_service_min(df, c)
    plan = recommender.slot_plan(hp, c, staff[c], svc, appt_share)
    moves, plan = recommender.rebalance(plan)
    st.caption(f"Capacity at {int(config.TARGET_UTILIZATION * 100)}% utilisation with "
               f"{staff[c]} staff and {svc:.1f} min per service.")
    st.dataframe(plan, hide_index=True, width="stretch")
    if moves.empty:
        st.write("Demand fits within capacity in every hour.")
    else:
        st.markdown("**Suggested shifts of demand (via appointment booking):**")
        st.dataframe(moves, hide_index=True)


# --------------------------------------------------------------------------- live queue
def _live_queue(raw: pd.DataFrame, df_all: pd.DataFrame) -> None:
    counters = sorted(set(config.COUNTERS) | set(df_all["counter"].unique() if not df_all.empty else []))
    left, right = st.columns(2)
    with left:
        st.subheader("Issue a token (walk-in)")
        st.caption("Optional email: the person gets the same 'issued' and 'your turn' emails.")
        with st.form("issue"):
            counter = st.selectbox("Counter", counters)
            stype = st.text_input("Service type", "general")
            email = st.text_input("Notify email (optional)", key="walkin_email")
            if st.form_submit_button("Issue token"):
                try:
                    t = services.issue_token(counter, stype, notify_email=email or None)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    tail = f" Email queued to {t['notify_email']}." if t["notify_email"] else ""
                    flash(f"Token #{t['token_no']} issued at {counter}.{tail}")
    with right:
        st.subheader("Add a manual observation")
        st.caption("Voluntary observation: when someone arrived and how long they waited.")
        with st.form("manual"):
            mc = st.selectbox("Counter", counters, key="manual_counter")
            md = st.date_input("Date", date.today())
            mt = st.time_input("Arrival time", time(11, 0))
            mw = st.number_input("Minutes waited", 0.0, 600.0, 10.0, 1.0)
            if st.form_submit_button("Save observation"):
                arrived = datetime.combine(md, mt)
                t = db.issue_token(mc, "observed", arrived, source="manual")
                db.serve_token(t["id"], arrived + pd.Timedelta(minutes=float(mw)))
                flash("Observation saved.")

    st.subheader("Live queue (today's tokens)")
    live = raw[(raw["source"] == "token") & (raw["issued_at"].dt.date == date.today())]
    waiting = live[live["served_at"].isna()]
    serving = live[live["served_at"].notna() & live["finished_at"].isna()]
    w_col, s_col = st.columns(2)
    with w_col:
        st.markdown(f"**Waiting ({len(waiting)})**")
        for r in waiting.itertuples():
            a, b = st.columns([3, 1])
            mail = " 📧" if r.notify_email else ""
            a.write(f"{r.counter} · #{r.token_no} · since {r.issued_at:%H:%M}{mail}")
            if b.button("Serve", key=f"serve_{r.id}", help="Call this token (sends the 'your turn' email)"):
                services.serve_token(int(r.id))
                note = f" Email queued to {r.notify_email}." if r.notify_email else ""
                flash(f"Token #{r.token_no} called at {r.counter}.{note}")
    with s_col:
        st.markdown(f"**Being served ({len(serving)})**")
        for r in serving.itertuples():
            a, b = st.columns([3, 1])
            a.write(f"{r.counter} · #{r.token_no} · since {r.served_at:%H:%M}")
            if b.button("Finish", key=f"finish_{r.id}"):
                db.finish_token(int(r.id))
                st.rerun()


# --------------------------------------------------------------------------- notifications
def _notifications() -> None:
    s = config.email_settings()
    if s["mode"] == "smtp":
        st.success(f"📤 Sending real emails through **{s['host']}:{s['port']}** as **{s['sender']}**.")
    elif s["mode"] == "console":
        st.warning("👁️ **Preview mode:** emails are NOT sent. They are saved below so you can read them. "
                   "To send real emails, set SMTP_HOST, SMTP_USER and SMTP_PASSWORD in the `.env` file "
                   "and restart the app (see docs/NOTIFICATIONS.md).")
    else:
        st.info("Notifications are switched off (EMAIL_MODE=off).")

    counts = db.notification_counts()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sent", counts.get("sent", 0))
    m2.metric("Preview only", counts.get("logged", 0))
    m3.metric("Failed", counts.get("failed", 0))
    m4.metric("Sending", counts.get("pending", 0))

    with st.expander("Send a test email"):
        with st.form("test_mail"):
            to = st.text_input("Send to", key="test_mail_to")
            if st.form_submit_button("Send test"):
                if not auth.EMAIL_RE.match(auth.normalize_email(to)):
                    st.error("Please enter a valid email address.")
                else:
                    ok, msg = notifier.send_test(auth.normalize_email(to))
                    (st.success if ok else st.error)(msg)

    st.subheader("Email log")
    choice = st.selectbox("Show", ["all", "sent", "logged", "failed", "pending"], key="mail_filter")
    log = db.list_notifications(status=None if choice == "all" else choice, limit=300)
    if log.empty:
        st.info("No emails yet. They appear here when a token is issued or served.")
        return
    st.dataframe(log, hide_index=True, width="stretch")

    st.subheader("Open an email")
    nid = st.selectbox("Email #", log["id"].tolist(), key="mail_open")
    row = db.get_notification(int(nid))
    st.markdown(f"**To:** {row['to_email']}  \n**Subject:** {row['subject']}  \n**Status:** {row['status']}")
    if row["error"]:
        st.error(row["error"])
    st.code(row["body_text"], language=None)
    if row["status"] in ("failed", "logged") and st.button("Send this email again", key="mail_retry"):
        status = notifier.retry(int(nid))
        flash(f"Email #{nid}: {status}.")


# --------------------------------------------------------------------------- users
def _users(me: dict) -> None:
    users = auth.list_users()
    st.subheader("Accounts")
    st.dataframe(pd.DataFrame(users)[["id", "name", "email", "role", "created_at"]],
                 hide_index=True, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Change a role")
        with st.form("role_form"):
            target = st.selectbox("Account", users, format_func=lambda u: f"{u['email']} ({u['role']})")
            role = st.selectbox("New role", list(auth.ROLES))
            if st.form_submit_button("Apply"):
                if target["id"] == me["id"]:
                    st.error("You cannot change your own role.")
                else:
                    try:
                        auth.set_role(target["id"], role)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        flash(f"{target['email']} is now {role}.")
    with c2:
        st.subheader("Create an account")
        with st.form("new_user_form"):
            name = st.text_input("Name", key="nu_name")
            email = st.text_input("Email", key="nu_email")
            password = st.text_input("Temporary password", type="password", key="nu_password")
            role = st.selectbox("Role", list(auth.ROLES), key="nu_role")
            if st.form_submit_button("Create"):
                try:
                    u = auth.create_user(email, password, name, role)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    flash(f"Account created for {u['email']}.")


# --------------------------------------------------------------------------- raw data
def _data(raw: pd.DataFrame) -> None:
    st.subheader("All stored tokens")
    st.dataframe(raw, hide_index=True, width="stretch")
    if not raw.empty:
        st.download_button("Download CSV", raw.to_csv(index=False), "tokens.csv", "text/csv")
