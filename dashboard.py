"""Streamlit app for QueueLess Campus: login, then the user or admin interface.

Run:  streamlit run dashboard.py
"""
import streamlit as st

st.set_page_config(page_title="QueueLess Campus", page_icon="🎫", layout="wide")

from app import db  # noqa: E402
from ui import admin_view, auth_ui, user_view  # noqa: E402

db.init_db()   # creates tables and upgrades an older database automatically

user = auth_ui.current_user()
if user is None:
    auth_ui.render_login()
    st.stop()

auth_ui.render_sidebar_header(user)
if user["role"] == "admin":
    admin_view.render(user)
else:
    user_view.render(user)
