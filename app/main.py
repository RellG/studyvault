"""studyvault: FastAPI app, middleware, router registration, first-boot setup."""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, notes_fs, seed
from .config import settings
from .routers import ai, assessments, cards, competencies, courses, dashboard, export, notes, quizzes, search, sessions, terms

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("studyvault")


def startup() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    notes_fs.ensure_repo()
    conn = db.connect()
    try:
        applied = db.migrate(conn)
        if applied:
            log.info("applied migrations: %s", applied)
        if seed.seed(conn, settings.seed_file):
            log.info("seeded from %s", settings.seed_file)
        remaining = seed.remaining_cu(conn)
        log.info("remaining CU in plan: %s", remaining)
        migrated = notes_fs.migrate_scratch_to_notebook()
        if migrated:
            log.info("renamed scratch.md -> notebook.md in %s course(s)", migrated)
        log.info("reindexed %s note files", notes_fs.reindex_all(conn))
    finally:
        conn.close()


COMMIT_EVERY_S = 60


async def committer():
    """Commit dirty notes at most once a minute (see notes_fs docstring)."""
    while True:
        await asyncio.sleep(COMMIT_EVERY_S)
        await asyncio.to_thread(notes_fs.commit_pending)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    task = asyncio.create_task(committer())
    try:
        yield
    finally:
        task.cancel()
        notes_fs.commit_pending()


if not settings.password:
    raise RuntimeError("STUDYVAULT_PASSWORD is not set")

app = FastAPI(title="studyvault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.require_login)
app.add_middleware(SessionMiddleware, secret_key=settings.secret or secrets.token_hex(32),
                   session_cookie="studyvault", max_age=60 * 60 * 24 * 30, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/health")
def health():
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(terms.router)
app.include_router(courses.router)
app.include_router(notes.router)
app.include_router(search.router)
app.include_router(competencies.router)
app.include_router(cards.router)
app.include_router(quizzes.router)
app.include_router(assessments.router)
app.include_router(sessions.router)
app.include_router(ai.router)
app.include_router(export.router)
