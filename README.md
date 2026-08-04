# Job Radar Assistant

Job Radar Assistant is a personal job-search helper for finding, scoring, saving, and reviewing software engineering job postings.

The project is designed to run locally on a personal computer. It is not an auto-apply bot, and it should never submit applications or answer sensitive application questions without explicit human approval.

## Project Goals

- Find junior-friendly software engineering job postings.
- Score jobs based on how well they match Matthew Glassman's target roles and locations.
- Save job postings in a simple local database.
- Notify when a posting looks like a strong fit.
- Keep the code readable, testable, and realistic for a growing developer.

## Target Roles

The assistant should prioritize roles like:

- Junior Software Engineer
- New Grad Software Engineer
- Entry-Level Software Developer
- Application Developer
- Frontend Developer
- Full-stack Developer
- Java Developer
- React / TypeScript Developer
- Junior AWS or cloud-related roles

Preferred locations:

- Minnesota
- Twin Cities
- Rochester
- Nearby hybrid roles
- Remote roles

## Roles to Avoid or Penalize

The assistant should avoid or heavily penalize postings that are clearly not a good fit, including:

- Senior roles
- Staff, principal, lead, or architect roles
- Manager roles
- Roles requiring 5+ years of experience
- Sketchy unpaid roles
- Contract-only roles
- Jobs that are clearly not entry-level or junior-friendly

## Planned Tech Stack

- Python
- FastAPI
- TinyDB for local JSON-like storage
- Pydantic for data validation
- BeautifulSoup and requests for simple scraping
- pytest for tests
- APScheduler or cron for scheduled scans later
- Telegram bot or Discord webhook for notifications later

Not planned for the early version:

- React
- Docker
- PostgreSQL
- Playwright
- AI-based scoring
- Browser automation
- Auto-apply behavior

## Current Status

This repository has a small local MVP in place. It can accept job postings, score them with simple rules, detect duplicates, save them in TinyDB, and list saved jobs through FastAPI.

## Current MVP

The current app supports a local job ingestion workflow:

1. A job comes in from an API route or the fake scanner.
2. Pydantic validates it as a `JobPosting`.
3. `app/ingestion.py` scores it, creates a `content_hash`, checks for duplicates, and saves new jobs.
4. TinyDB stores saved jobs locally in the `data/` folder.
5. `GET /jobs` returns the saved jobs.

Current routes:

```text
GET  /
GET  /health
GET  /jobs
GET  /jobs/top
GET  /applications
GET  /applications/pending
POST /jobs
POST /jobs/manual
POST /scan/fake
POST /applications/candidates
POST /applications/{application_id}/approve
POST /applications/{application_id}/reject
POST /applications/{application_id}/approve-submit
POST /applications/{application_id}/transitions
```

`POST /jobs` and `POST /jobs/manual` both use the same ingestion pipeline. `POST /scan/fake` runs two hardcoded sample jobs through that same pipeline so the workflow can be tested without real scraping.
`POST /applications/candidates` creates approval-ready application records for saved jobs whose fit score meets the application threshold.
Application actions enforce the supported status workflow and append every change to the application's event history. Starting and submitting each require a dedicated approval action, and the approval type and approver are recorded. Invalid status jumps return `409 Conflict` without changing the application.

Open the interactive API docs after starting the server:

```text
http://127.0.0.1:8000/docs
```

## Quick Demo

Activate the virtual environment:

```bash
source .venv/bin/activate
```

Run the tests:

```bash
pytest
```

Start the FastAPI server:

```bash
uvicorn app.main:app --reload
```

Open the API docs:

```text
http://127.0.0.1:8000/docs
```

Try these routes in order:

```text
GET  /health
POST /scan/fake
GET  /jobs
GET  /jobs/top
POST /applications/candidates
GET  /applications/pending
POST /applications/{application_id}/approve
```

The demo flow is:

1. Run the fake scan.
2. View saved jobs with `GET /jobs`.
3. View the best saved jobs first with `GET /jobs/top`.
4. Create application candidates for strong matches.
5. Review pending application candidates before taking any application action.
6. Approve or reject each candidate.
7. Record later workflow steps with `POST /applications/{application_id}/transitions`.
8. Explicitly approve submission with `POST /applications/{application_id}/approve-submit` before recording the final `submitted` transition.

You can also run the fake scan from the terminal:

```bash
python scripts/run_fake_scan.py
```

The fake scan uses hardcoded sample jobs. It is only meant to prove the local ingestion, scoring, dedupe, and storage workflow.

## Run HTML Fixture Scan From Terminal

Run the local HTML fixture scanner without making network requests:

```bash
python scripts/run_html_fixture_scan.py
```

The script reads `tests/fixtures/sample_jobs.html`, parses the fake job cards, sends them through the shared ingestion pipeline, saves new jobs, and skips duplicates on repeated runs.

## Scanner Development Stages

### 1. Fake scanner

The fake scanner uses hardcoded sample jobs. It proves the core pipeline:

`job input -> ingestion -> scoring -> content_hash -> dedupe -> storage`

It does not parse HTML or make network requests.

### 2. HTML fixture scanner

The HTML fixture scanner parses local sample HTML files from `tests/fixtures/` using BeautifulSoup. It does not make network requests.

This stage proves the app can parse job-card-style HTML safely. Malformed or incomplete cards are skipped instead of crashing the parser.

### 3. First real scanner

The first real source uses the official [Himalayas public Jobs API](https://himalayas.app/docs/remote-jobs-api). It makes one filtered request for recent entry-level software engineering jobs and sends parsed results through the existing ingestion pipeline.

Run it manually:

```bash
python scripts/run_himalayas_scan.py
```

Himalayas data is refreshed every 24 hours, so this scanner should not run more than once per day. Job links point back to [Himalayas](https://himalayas.app), and the source is stored as `himalayas`.

Any real scanner must respect site terms, avoid aggressive scraping, never perform auto-apply behavior, and still send parsed jobs through the existing ingestion pipeline.

### 4. Job alert email ingestion foundation

The app includes parsers for the plain-text MIME parts of LinkedIn and Indeed
job-alert emails. Parsed cards are normalized into `JobPosting` records and use
the shared scoring and deduplication pipeline. A `processed_emails` TinyDB table
tracks Gmail message IDs so repeated scans do not import the same message again.
Messages must carry the expected Gmail label and a passing Google-recorded DMARC
result. Provider links are restricted to HTTPS URLs on the expected domain;
opaque Indeed email redirects are replaced with token-free Indeed search URLs.

The app includes a local Google OAuth adapter that uses the read-only Gmail
scope to fetch messages from the `Jobbot-LinkedIn` and `Jobbot-Indeed` labels.
OAuth credentials and tokens are excluded from Git.

The local Gmail adapter can be run with:

```bash
python scripts/run_gmail_scan.py
```

On its first run, Google opens a browser consent flow for the read-only Gmail
scope. The resulting refresh token is stored under `credentials/` with
owner-only permissions. The adapter resolves the two Jobbot labels, reads only
matching non-spam messages from a seven-day catch-up window, verifies Google's
authentication result, and sends new messages through the shared email-ingestion
pipeline without changing Gmail.

## What This Does Not Do Yet

- No real scraping.
- No notifications.
- No scheduling.
- Gmail OAuth requires one-time local browser authorization.
- No auto-apply behavior.
- No browser automation.
- No AI scoring.

## Project Structure

```text
app/
  __init__.py
  main.py
  models.py
  storage.py
  scoring.py
  dedupe.py
  ingestion.py
  scanner.py
data/
  .gitkeep
tests/
  test_main.py
  test_scoring.py
  test_storage.py
config.yaml
requirements.txt
README.md
.gitignore
```

## Running Locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the FastAPI app:

```bash
uvicorn app.main:app --reload
```

Run tests:

```bash
pytest
```

## Safety Boundaries

This project should:

- Run only on a personal machine and personal network.
- Never submit job applications automatically.
- Never guess legally sensitive answers.
- Never answer custom application questions without human review.
- Keep any future browser automation behind explicit approval steps.

## Development Notes

The code should favor simple, readable modules over complex architecture. Each feature should be small enough to understand, test, and explain as part of a portfolio project.
