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


def test_login_locks_out_after_repeated_failures(data_dir, monkeypatch):
    from app import auth

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(auth.asyncio, "sleep", no_sleep)
    with TestClient(app) as c:
        h = {"cf-connecting-ip": "203.0.113.9"}
        for _ in range(auth.MAX_FAILURES):
            assert c.post("/login", data={"password": "nope"}, headers=h).status_code == 401
        # Locked out: even the right password is refused for this client...
        assert c.post("/login", data={"password": "test-pass"}, headers=h, follow_redirects=False).status_code == 429
        # ...but a different client is unaffected.
        r = c.post("/login", data={"password": "test-pass"}, headers={"cf-connecting-ip": "198.51.100.7"}, follow_redirects=False)
        assert r.status_code == 303


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
