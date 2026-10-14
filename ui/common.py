"""Small helpers shared by all screens."""
import streamlit as st


def flash(msg: str) -> None:
    """Show `msg` as a green message after the page reloads."""
    st.session_state["flash"] = msg
    st.rerun()


def show_flash() -> None:
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
