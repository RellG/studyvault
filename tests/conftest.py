import os
import tempfile

# Must run before `app` is imported: settings are read from the environment at import time.
os.environ.setdefault("STUDYVAULT_DATA", tempfile.mkdtemp(prefix="sv-test-"))
# Tests always use the public example seed, so they pass the same with or without a personal seed/courses.yaml.
os.environ["STUDYVAULT_SEED"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "courses.example.yaml")
os.environ["STUDYVAULT_PASSWORD"] = "test-pass"
os.environ["STUDYVAULT_SECRET"] = "test-secret"
for k in ("AI_PROVIDER", "AI_MODEL", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(k, None)

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture
def conn(data_dir):
    c = db.connect(settings.db_path)
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    seed.seed(conn, settings.seed_file)
    return conn


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/login", data={"password": "test-pass"}, follow_redirects=False)
        assert r.status_code == 303
        yield c
