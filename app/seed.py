"""First-boot seed: seed/courses.yaml → DB. Runs once; later edits happen in the UI."""
from pathlib import Path

import yaml

from . import clock
from .db import get_meta, set_meta

EXPECTED_REMAINING_CU = 109


class SeedError(RuntimeError):
    pass


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check(data: dict) -> int:
    """Return remaining CU, or raise if it isn't 109 or doesn't reconcile with the program total."""
    remaining = sum(c["cu"] for c in data["courses"])
    transferred = sum(t["cu"] for t in data["program"].get("transferred", []))
    if remaining != EXPECTED_REMAINING_CU:
        raise SeedError(f"Seed courses sum to {remaining} CU; expected {EXPECTED_REMAINING_CU}")
    if remaining + transferred != data["program"]["total_cu"]:
        raise SeedError(f"{remaining} remaining + {transferred} transferred != program total {data['program']['total_cu']}")
    codes = [c["code"] for c in data["courses"]]
    if len(codes) != len(set(codes)):
        raise SeedError("Duplicate course codes in seed")
    return remaining


def _s(v):
    return None if v is None else str(v)


def seed(conn, path: Path) -> bool:
    """Seed the DB if it hasn't been seeded. Returns True if it seeded now."""
    if get_meta(conn, "seeded_at"):
        return False
    data = load(path)
    check(data)
    with conn:
        prog = data["program"]
        conn.execute("INSERT INTO program(id, name, total_cu) VALUES (1, ?, ?)", (prog["name"], prog["total_cu"]))
        for t in prog.get("transferred", []):
            conn.execute("INSERT INTO transfers(code, title, cu, via) VALUES (?, ?, ?, ?)",
                         (t["code"], t["title"], t["cu"], t.get("via")))
        term_ids = {}
        for t in data["terms"]:
            cur = conn.execute("INSERT INTO terms(n, start, end, target_cu) VALUES (?, ?, ?, ?)",
                               (t["n"], _s(t["start"]), _s(t["end"]), t["target_cu"]))
            term_ids[t["n"]] = cur.lastrowid
        for i, c in enumerate(data["courses"]):
            cur = conn.execute(
                """INSERT INTO courses(code, title, cu, term_id, ord, assessment_type, start, due, target,
                                       approved, cert_name, after_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (c["code"], c["title"], c["cu"], term_ids.get(c.get("term")), i, c.get("assessment"),
                 _s(c.get("start")), _s(c.get("due")), _s(c.get("target")),
                 1 if c.get("approved") else 0, c.get("cert"), c.get("after")))
            if c.get("cert"):
                conn.execute("INSERT INTO certs(name, course_id) VALUES (?, ?)", (c["cert"], cur.lastrowid))
        for name in data.get("self_directed_certs") or []:  # spec F7: certs outside any course
            conn.execute("INSERT INTO certs(name) VALUES (?)", (name,))
        set_meta(conn, "seeded_at", clock.now().isoformat())
    return True


def remaining_cu(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(cu), 0) FROM courses").fetchone()[0]
