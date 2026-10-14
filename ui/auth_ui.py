"""Login / registration screen and the signed-in header."""
from __future__ import annotations

import streamlit as st

from app import auth


def current_user() -> dict | None:
    """The signed-in account, re-read from the database on every run.

    Re-reading means a user whose role was changed (or who was removed) is affected immediately.
    """
    uid = st.session_state.get("uid")
    if not uid:
        return None
    user = auth.get_user(uid)
    if user is None:
        st.session_state.pop("uid", None)
    return user


def _sign_in(user: dict) -> None:
    st.session_state["uid"] = user["id"]
    st.rerun()


def render_login() -> None:
    st.title("🎫 QueueLess Campus")
    st.caption("Take a token online and get an email when it is issued and when it is your turn.")
    if auth.count_admins() == 0:
        st.info("No admin account exists yet. Open a terminal in the project folder and run "
                "`python create_admin.py` to create one.")

    tab_in, tab_reg = st.tabs(["Log in", "Create account"])
    with tab_in:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Log in", type="primary")
        if submitted:
            user, error = None, None
            try:
                user = auth.authenticate(email, password)
                if user is None:
                    error = "Wrong email or password."
            except auth.AuthLocked as exc:
                error = str(exc)
            if user:
                _sign_in(user)
            else:
                st.error(error)

    with tab_reg:
        with st.form("register_form"):
            name = st.text_input("Full name", key="reg_name")
            email = st.text_input("Email (your notifications are sent here)", key="reg_email")
            password = st.text_input("Password", type="password", key="reg_password")
            again = st.text_input("Repeat password", type="password", key="reg_password2")
            submitted = st.form_submit_button("Create account")
        if submitted:
            if password != again:
                st.error("The two passwords do not match.")
            else:
                try:
                    _sign_in(auth.create_user(email, password, name))   # self-registration is always 'user'
                except ValueError as exc:
                    st.error(str(exc))


def render_sidebar_header(user: dict) -> None:
    """Title, who is signed in, and the Log out button (top of the sidebar)."""
    with st.sidebar:
        st.title("🎫 QueueLess Campus")
        label = user["name"] or user["email"]
        st.caption(f"Signed in as **{label}** · {'🛡️ Admin' if user['role'] == 'admin' else '🎓 User'}")
        if st.button("Log out", key="logout"):
            st.session_state.clear()
            st.rerun()
        st.divider()
