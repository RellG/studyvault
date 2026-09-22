# studyvault

A self-hosted study notebook for a WGU degree, built to run on a Raspberry Pi on your home network. One notebook per course, organized by term and competency, with spaced-repetition flashcards, practice quizzes, OA/PA/cert prep workspaces, study-time tracking, and a dashboard that tells you whether you're on pace, including the SAP ratio financial aid depends on.

It was built for the B.S. Cloud & Network Engineering – AWS program, but the course plan is a YAML file, so it works for any WGU program.

- **Your notes are plain markdown files**, versioned with git. If the app ever breaks, they're still readable in any editor.
- **Small:** ~30 MB of RAM, ~0% CPU at idle. FastAPI + SQLite in one container, no Node build, all frontend assets vendored (works with the internet down).
- **LAN only, single user**, password login.

## Features

| | |
|---|---|
| **Dashboard** | Current term, CU passed vs. target with a pace projection, SAP ratio (green / amber / red), Term 1 minimum check, projected graduation, next course with readiness and cards due, exam countdowns, certs earned, study streak |
| **Terms & courses** | Term timeline, course status (not started → in progress → pre-assessed → scheduled → passed / revision needed), target vs. mentor due dates, move or add courses mid-term |
| **Course notebook** | Overview, Competencies, Notebook and Mistakes tabs. Markdown editor (EasyMDE) with 2 s autosave, conflict detection, code highlighting, image paste/upload, `[[D325]]` links between courses. Files are git-committed in batches |
| **Competencies & readiness** | Paste the competency list; rate confidence 1–5; weakest three shown on every course page. Readiness = 40% confidence + 30% flashcard recall + 30% latest quiz, with the formula shown |
| **Flashcards** | SM-2 spaced repetition; bulk import from text or a notes section (`Q: / A:`, `term :: definition`, `{{c1::cloze}}`); keyboard or tap review on a phone |
| **Practice quizzes** | Multiple choice, multi-select, self-graded short answer; timed or untimed; every miss is appended to the course's `mistakes.md` with a "why I missed it" line |
| **OA / PA / cert prep** | Pre-assessment log with coaching-report levels, a computed readiness checklist, exam scheduling; PA rubric tasks, versioned drafts with diffs, submission log, revision counter and a term-end warning; cert vouchers, exam dates and results |
| **Study time** | Server-side timer (survives reloads), optional Pomodoro, hours per week / course / CU |
| **Search** | SQLite FTS5 across notes, flashcards and questions, filterable by term and course |
| **AI assist (optional)** | Notes → flashcards, notes → practice questions, explain differently, gap check. Anthropic or Google via env config; everything comes back as a draft you accept or discard; only the text you select is sent; never used to write performance assessments |
| **Backup & export** | Nightly SQLite backup + notes/attachments archives with checksums, 14 kept, rsync off-device; tested restore script; course export to one markdown file or a print-to-PDF page |

## Run it

Requirements: a Raspberry Pi 4 (or any Linux box) with Docker and the Compose plugin.

```bash
git clone https://github.com/RellG/studyvault.git ~/studyvault
cd ~/studyvault
cp .env.example .env                 # set STUDYVAULT_LAN_IP, STUDYVAULT_PASSWORD, STUDYVAULT_SECRET (openssl rand -hex 32)
cp seed/courses.example.yaml seed/courses.yaml   # edit terms, dates and courses BEFORE first start
mkdir -p data

docker build --platform linux/arm/v7 -t studyvault:latest .    # see "Platform" below
docker compose up -d --no-build
docker run --rm --platform linux/arm/v7 studyvault:latest python -m pytest -q
```

Open `http://<STUDYVAULT_LAN_IP>:8420` and log in with your password. The seed runs once, on first boot; after that, edit courses and terms in the UI.

**Platform.** `docker-compose.yml` pins `linux/arm/v7` because the Pi it was built on runs a 32-bit userland on a 64-bit kernel (64-bit images crash there). On 64-bit Raspberry Pi OS or x86, change `platform:` in `docker-compose.yml` and the `--platform` flags to `linux/arm64` or `linux/amd64`. On newer Docker, `docker compose up -d --build` also works; the plain `docker build` step exists because Docker 20.10 is too old for Compose v5's builder.

**The seed.** `seed/courses.example.yaml` is the BSCNE-AWS course list with a sample term calendar. Startup checks that remaining CU sum to the expected total, so adjust `EXPECTED_REMAINING_CU` in `app/seed.py` if your program differs. `seed/courses.yaml` is git-ignored, so your own plan stays out of the repo.

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `STUDYVAULT_LAN_IP`, `STUDYVAULT_PORT` | The container is published on this address only (default port 8420) |
| `STUDYVAULT_PASSWORD`, `STUDYVAULT_SECRET` | Login password; cookie-signing secret |
| `STUDYVAULT_TZ` | Your timezone, e.g. `America/New_York` |
| `AI_PROVIDER`, `AI_MODEL`, `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | Leave empty to hide AI features. No model name is hardcoded |
| `AI_MAX_OUTPUT_TOKENS` | Output cap per AI request (default 2048) |
| `BACKUP_TARGET` | rsync destination for nightly backups (`user@host:path` or a mounted drive) |

## Backups

```bash
scripts/backup.sh                 # online DB backup + integrity check, notes (with git history), attachments → backups/<timestamp>/
scripts/install-backup-cron.sh    # 02:30 nightly via the user crontab
scripts/restore.sh [snapshot]     # into an empty data/ with the app stopped; verifies checksums
```

## Development

- Code: `app/` (FastAPI routers in `app/routers/`, Jinja2 templates, HTMX, vanilla JS), migrations in `app/migrations/`.
- Tests: `python -m pytest` inside the image (119 tests: SM-2, readiness, SAP/pace math, seed, notes filesystem, search, quizzes, assessments, AI client with mocked providers).
- Browser checks: `scripts/scratch-instance.sh up` starts a throwaway copy on `127.0.0.1:8421` with empty data (password `scratch`); `node tests/ui/ui_check.js` runs phone- and desktop-sized checks against it through a Chromium with remote debugging (`SV_CDP`, `SV_PLAYWRIGHT` to point at your Playwright install).
- Deploying from another machine: `STUDYVAULT_HOST=user@pi bash scripts/deploy.sh` (or put `STUDYVAULT_HOST=` in a git-ignored `.deploy.env`). It never touches `data/` or `.env`.

## Academic integrity

studyvault is for your own notes and self-testing. It doesn't scrape or store WGU assessment content, and the AI features only work from your own notes. The PA workspace records your drafts and evaluator feedback but never writes them. Check WGU's current AI-use policy for each course.

## License and third-party assets

studyvault is released under the [MIT License](LICENSE).

Vendored in `app/static/vendor/`: [htmx](https://htmx.org) 2.0.4, [EasyMDE](https://github.com/Ionaru/easy-markdown-editor) 2.18.0, [highlight.js](https://highlightjs.org) 11.9.0, [Font Awesome](https://fontawesome.com/v4/) 4.7.0. Each is under its own license.
