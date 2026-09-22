from fastapi.testclient import TestClient

from app.main import app


def test_health_is_public(data_dir):
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_pages_require_login(data_dir):
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.get("/", headers={"HX-Request": "true"}).status_code == 401
        assert c.post("/logout", follow_redirects=False).status_code in (303, 401)


def test_wrong_password_rejected(data_dir):
    with TestClient(app) as c:
        r = c.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert r.status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_and_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Term 1" in r.text


def test_open_redirect_blocked(data_dir):
    with TestClient(app) as c:
        r = c.post("/login", data={"password": "test-pass", "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/"


def test_notes_repo_initialised(client, data_dir):
    assert (data_dir / "notes" / ".git").is_dir()
