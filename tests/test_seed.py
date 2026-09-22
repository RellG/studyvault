import copy

import pytest

from app import seed
from app.config import settings


def test_seed_file_sums_to_109():
    data = seed.load(settings.seed_file)
    assert seed.check(data) == 109


def test_seed_loads_everything(seeded):
    conn = seeded
    assert seed.remaining_cu(conn) == 109
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 34
    assert conn.execute("SELECT cu FROM transfers WHERE code = 'E004'").fetchone()[0] == 3
    d413 = conn.execute("SELECT c.*, t.n FROM courses c JOIN terms t ON t.id = c.term_id WHERE code = 'D413'").fetchone()
    assert (d413["n"], d413["start"], d413["due"], d413["target"], d413["approved"]) == (1, "2026-10-02", "2026-10-30", "2026-10-16", 0)
    assert d413["assessment_type"] is None  # deliberately unknown
    assert conn.execute("SELECT assessment_type FROM courses WHERE code = 'E030'").fetchone()[0] == "PA"
    assert conn.execute("SELECT after_code FROM courses WHERE code = 'C957'").fetchone()[0] == "C955"
    assert conn.execute("SELECT COUNT(*) FROM competencies").fetchone()[0] == 0  # never invented
    # 7 in-program certs; the example seed has no self-directed ones
    assert conn.execute("SELECT COUNT(*) FROM certs").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM certs WHERE course_id IS NULL").fetchone()[0] == 0
    term_cu = dict(conn.execute("SELECT t.n, SUM(c.cu) FROM courses c JOIN terms t ON t.id = c.term_id GROUP BY t.n").fetchall())
    assert term_cu == {1: 27, 2: 30, 3: 26, 4: 26}


def test_seed_is_idempotent(seeded):
    assert seed.seed(seeded, settings.seed_file) is False
    assert seeded.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 34
    assert seeded.execute("SELECT COUNT(*) FROM certs").fetchone()[0] == 7


def test_seed_rejects_wrong_total():
    data = copy.deepcopy(seed.load(settings.seed_file))
    data["courses"][0]["cu"] += 1
    with pytest.raises(seed.SeedError):
        seed.check(data)


def test_seed_rejects_duplicate_codes():
    data = copy.deepcopy(seed.load(settings.seed_file))
    data["courses"][1]["code"] = data["courses"][0]["code"]
    with pytest.raises(seed.SeedError):
        seed.check(data)


def test_self_directed_certs_from_yaml(conn, tmp_path):
    text = settings.seed_file.read_text(encoding="utf-8").replace("self_directed_certs: []",
                                                                  "self_directed_certs:\n  - Cisco CCNA\n  - Some Other Cert")
    p = tmp_path / "mine.yaml"
    p.write_text(text, encoding="utf-8")
    seed.seed(conn, p)
    names = {r[0] for r in conn.execute("SELECT name FROM certs WHERE course_id IS NULL")}
    assert names == {"Cisco CCNA", "Some Other Cert"}
