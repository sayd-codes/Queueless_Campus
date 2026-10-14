"""Drive the real Streamlit screens (login, user interface, admin interface) with AppTest."""
import os

import pytest
from streamlit.testing.v1 import AppTest

from app import auth, db, notifier

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setattr(notifier, "_spawn", lambda target, *args: target(*args))
    db.init_db()
    with db.connect() as c:
        for table in ("notifications", "tokens", "sessions", "users"):
            c.execute(f"DELETE FROM {table}")
    auth._FAILS.clear()


def app_for(user=None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    if user:
        at.session_state["uid"] = user["id"]
    return at.run()


def button(at, label):
    return next(b for b in at.button if b.label == label)


def tab_labels(at):
    return [t.label for t in at.tabs]


def test_login_screen_when_signed_out():
    at = app_for()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Log in", "Create account"]
    assert any("create_admin.py" in i.value for i in at.info)       # no admin yet -> hint


def test_register_then_see_user_interface_not_admin():
    at = app_for()
    at.text_input(key="reg_name").set_value("Asha")
    at.text_input(key="reg_email").set_value("asha@college.edu")
    at.text_input(key="reg_password").set_value("password123")
    at.text_input(key="reg_password2").set_value("password123")
    next(b for b in at.button if b.label == "Create account").click().run()
    assert not at.exception
    assert tab_labels(at) == ["🎟️ Get a token", "📍 My tokens", "📧 My emails"]
    assert auth.list_users()[0]["role"] == "user"


def test_wrong_password_and_mismatch_show_errors():
    auth.create_user("asha@college.edu", "password123", "Asha")
    at = app_for()
    at.text_input(key="login_email").set_value("asha@college.edu")
    at.text_input(key="login_password").set_value("totally-wrong")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert any("Wrong email or password" in e.value for e in at.error)
    assert tab_labels(at) == ["Log in", "Create account"]

    at.text_input(key="reg_email").set_value("new@college.edu")
    at.text_input(key="reg_password").set_value("password123")
    at.text_input(key="reg_password2").set_value("different123")
    next(b for b in at.button if b.label == "Create account").click().run()
    assert any("do not match" in e.value for e in at.error)


def test_login_success_for_admin_shows_admin_tabs():
    auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for()
    at.text_input(key="login_email").set_value("boss@college.edu")
    at.text_input(key="login_password").set_value("password123")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception
    assert "📧 Notifications" in tab_labels(at) and "👥 Users" in tab_labels(at)


def test_user_takes_token_and_email_is_logged():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    at = app_for(user)
    assert not at.exception
    button(at, "Get token").click().run()
    assert not at.exception
    assert any("Token #1 issued" in s.value for s in at.success)
    log = db.list_notifications(user_id=user["id"])
    assert list(log["event"]) == ["issued"] and log.iloc[0]["to_email"] == "asha@college.edu"

    button(at, "Get token").click().run()                        # second token at same counter
    assert any("already have a waiting token" in e.value for e in at.error)
    assert len(db.list_notifications(user_id=user["id"])) == 1


def test_user_can_opt_out_of_email():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    at = app_for(user)
    at.checkbox[0].uncheck()
    button(at, "Get token").click().run()
    assert any("Token #1 issued" in s.value for s in at.success)
    assert db.list_notifications(user_id=user["id"]).empty


def test_user_cannot_reach_admin_functions_by_session_trick():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    at = app_for(user)
    assert "👥 Users" not in tab_labels(at)
    assert all(b.label != "Load sample data" for b in at.sidebar.button)


def test_admin_serves_token_and_user_gets_second_email():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    admin = auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for(user)
    button(at, "Get token").click().run()

    at = app_for(admin)
    assert not at.exception
    serve = [b for b in at.button if b.label == "Serve"]
    assert len(serve) == 1
    serve[0].click().run()
    assert not at.exception
    assert any("called" in s.value and "asha@college.edu" in s.value for s in at.success)
    events = sorted(db.list_notifications(user_id=user["id"])["event"])
    assert events == ["issued", "served"]

    at = app_for(user)                                           # user now sees "your turn"
    assert any("your turn" in m.value for m in at.markdown)


def test_admin_notifications_and_users_tabs_render_and_work():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    admin = auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for(user)
    button(at, "Get token").click().run()

    at = app_for(admin)
    assert any("Preview mode" in w.value for w in at.warning)
    assert any(m.label == "Preview only" and m.value == "1" for m in at.metric)

    # promote Asha through the Users tab
    asha = next(u for u in auth.list_users() if u["email"] == "asha@college.edu")
    [s for s in at.selectbox if s.label == "Account"][0].select(asha)
    [s for s in at.selectbox if s.label == "New role"][0].select("admin")
    next(b for b in at.button if b.label == "Apply").click().run()
    assert not at.exception
    assert auth.get_user(user["id"])["role"] == "admin"


def test_admin_cannot_demote_self_in_ui():
    admin = auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for(admin)
    sel = [s for s in at.selectbox if s.label == "Account"][0]
    sel.select(auth.list_users()[0])
    [s for s in at.selectbox if s.label == "New role"][0].select("user")
    next(b for b in at.button if b.label == "Apply").click().run()
    assert any("cannot change your own role" in e.value for e in at.error)
    assert auth.get_user(admin["id"])["role"] == "admin"


def test_admin_walk_in_with_email():
    admin = auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for(admin)
    at.text_input(key="walkin_email").set_value("walk@in.com")
    next(b for b in at.button if b.label == "Issue token").click().run()
    assert not at.exception
    assert any("Email queued to walk@in.com" in s.value for s in at.success)
    assert db.list_notifications()["to_email"].tolist() == ["walk@in.com"]


def test_logout_returns_to_login():
    user = auth.create_user("asha@college.edu", "password123", "Asha")
    at = app_for(user)
    button(at, "Log out").click().run()
    assert [t.label for t in at.tabs] == ["Log in", "Create account"]


def test_admin_analytics_tabs_work_with_data_after_loading_samples():
    admin = auth.create_user("boss@college.edu", "password123", "Boss", role="admin")
    at = app_for(admin)
    button(at, "Load sample data").click().run()
    assert not at.exception
    labels = {m.label for m in at.metric}
    assert {"Tokens analysed", "Average wait", "Staff now → needed"} <= labels
    assert any("Loaded" in s.value for s in at.success)
    assert len(at.dataframe) >= 4                                     # summary, plan, log/users, data
    # filters and sliders still work after the refactor
    at.slider[0].set_value(5)
    at.run()
    assert not at.exception
