import pytest
from fastapi.testclient import TestClient

import api
from app import auth, db, notifier


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(notifier, "_spawn", lambda target, *args: target(*args))
    """API client on the (throwaway) test database, emptied before each test."""
    db.init_db()
    with db.connect() as c:
        for table in ("notifications", "tokens", "sessions", "users"):
            c.execute(f"DELETE FROM {table}")
    auth._FAILS.clear()
    return TestClient(api.app)


def register(client, email="asha@college.edu", name="Asha"):
    r = client.post("/auth/register", json={"email": email, "password": "password123", "name": name})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_admin(client, email="boss@college.edu"):
    u = auth.create_user(email, "password123", "Boss", role="admin")
    r = client.post("/auth/login", json={"email": email, "password": "password123"})
    return u, {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_required(client):
    assert client.get("/me").status_code == 401
    assert client.post("/tokens", json={"counter": "Fee Counter"}).status_code == 401
    assert client.get("/analytics/summary").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/health").status_code == 200           # health stays public


def test_register_login_logout(client):
    user = register(client)
    assert client.get("/me", headers=user).json()["role"] == "user"
    assert client.post("/auth/register", json={"email": "asha@college.edu", "password": "password123"}
                       ).status_code == 422
    assert client.post("/auth/login", json={"email": "asha@college.edu", "password": "nope-nope"}
                       ).status_code == 401
    assert client.post("/auth/logout", headers=user).status_code == 200
    assert client.get("/me", headers=user).status_code == 401


def test_register_cannot_choose_admin_role(client):
    r = client.post("/auth/register", json={"email": "x@college.edu", "password": "password123",
                                            "role": "admin"})
    assert r.json()["user"]["role"] == "user"


def test_user_cannot_use_admin_routes(client):
    user = register(client)
    assert client.get("/analytics/summary", headers=user).status_code == 403
    assert client.get("/admin/users", headers=user).status_code == 403
    assert client.get("/admin/notifications", headers=user).status_code == 403
    assert client.post("/seed", headers=user).status_code == 403
    assert client.delete("/tokens", headers=user).status_code == 403
    assert client.post("/tokens/1/serve", headers=user).status_code == 403


def test_full_flow_user_takes_token_admin_serves_both_emails_logged(client):
    user = register(client)
    _, admin = make_admin(client)

    t = client.post("/tokens", json={"counter": "Fee Counter", "service_type": "Fee payment"}, headers=user)
    assert t.status_code == 200 and t.json()["ahead"] == 0
    token_id = t.json()["id"]

    mine = client.get("/me/tokens", headers=user).json()
    assert mine[0]["id"] == token_id and mine[0]["ahead"] == 0
    assert client.post("/tokens", json={"counter": "Fee Counter"}, headers=user).status_code == 422  # limit

    assert client.post(f"/tokens/{token_id}/serve", headers=admin).status_code == 200
    assert client.post(f"/tokens/{token_id}/serve", headers=admin).status_code == 404   # no double serve

    mails = client.get("/me/notifications", headers=user).json()
    assert sorted(m["event"] for m in mails) == ["issued", "served"]
    assert {m["status"] for m in mails} == {"logged"}

    log = client.get("/admin/notifications", headers=admin).json()
    assert log["counts"]["logged"] == 2 and len(log["items"]) == 2


def test_walk_in_and_role_management(client):
    me_admin, admin = make_admin(client)
    r = client.post("/admin/tokens", json={"counter": "Admin Office", "notify_email": "walk@in.com"}, headers=admin)
    assert r.status_code == 200 and r.json()["notify_email"] == "walk@in.com"
    assert client.post("/admin/tokens", json={"counter": "Admin Office", "notify_email": "bad"},
                       headers=admin).status_code == 422

    register(client)
    users = client.get("/admin/users", headers=admin).json()
    asha = next(u for u in users if u["email"] == "asha@college.edu")
    assert client.patch(f"/admin/users/{asha['id']}/role", json={"role": "admin"}, headers=admin
                        ).json()["role"] == "admin"
    assert client.patch(f"/admin/users/{me_admin['id']}/role", json={"role": "user"}, headers=admin
                        ).status_code == 422                  # cannot change own role
    assert client.patch(f"/admin/users/{asha['id']}/role", json={"role": "boss"}, headers=admin
                        ).status_code == 422


def test_admin_test_email_and_retry(client):
    _, admin = make_admin(client)
    r = client.post("/admin/notifications/test", json={"to": "me@example.com"}, headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is False and "preview" in r.json()["message"].lower()
    assert client.post("/admin/notifications/test", json={"to": "nope"}, headers=admin).status_code == 422
    assert client.post("/admin/notifications/999/retry", headers=admin).status_code == 422


def test_login_lockout_returns_429(client):
    register(client)
    for _ in range(5):
        client.post("/auth/login", json={"email": "asha@college.edu", "password": "wrong-wrong"})
    assert client.post("/auth/login", json={"email": "asha@college.edu", "password": "password123"}
                       ).status_code == 429


def test_analytics_for_admin(client):
    _, admin = make_admin(client)
    assert client.post("/seed?days=2&reset=true", headers=admin).json()["inserted"] > 100
    assert client.get("/analytics/summary", headers=admin).json()
    assert client.get("/recommendations", headers=admin).json()
    assert client.get("/analytics/hourly", headers=admin).json()
