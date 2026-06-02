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
POST /jobs
POST /jobs/manual
POST /scan/fake
```

`POST /jobs` and `POST /jobs/manual` both use the same ingestion pipeline. `POST /scan/fake` runs two hardcoded sample jobs through that same pipeline so the workflow can be tested without real scraping.

Open the interactive API docs after starting the server:

```text
http://127.0.0.1:8000/docs
```

Intentionally not built yet:

- Real web scraping
- Notifications
- Scheduling
- Playwright or browser automation
- React frontend
- Docker setup
- AI scoring
- Auto-apply behavior

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

## Run Fake Scan From Terminal

After activating the virtual environment, run the fake scanner without opening the FastAPI docs:

```bash
python scripts/run_fake_scan.py
```

The script uses the existing fake scanner and the shared ingestion pipeline. It prints how many jobs were scanned, how many were created, how many duplicates were skipped, each saved job title, and each job's fit score.

## Suggested First Milestones

1. Create a Pydantic model for a job posting.
2. Add TinyDB storage for saving and listing jobs.
3. Add a simple FastAPI app with health and jobs routes.
4. Add basic scoring rules for target roles and disqualifying signals.
5. Add pytest coverage for storage and scoring.
6. Add simple scraping for one job source.
7. Add local notifications after the core workflow works.

## Safety Boundaries

This project should:

- Run only on a personal machine and personal network.
- Never submit job applications automatically.
- Never guess legally sensitive answers.
- Never answer custom application questions without human review.
- Keep any future browser automation behind explicit approval steps.

## Development Notes

The code should favor simple, readable modules over complex architecture. Each feature should be small enough to understand, test, and explain as part of a portfolio project.
