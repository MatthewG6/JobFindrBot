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

This repository has the initial Python project structure in place. The first version should stay small and focus on a working scanner, scoring model, storage layer, and tests.

## Project Structure

```text
app/
  __init__.py
  main.py
  models.py
  storage.py
  scoring.py
  dedupe.py
  scanner.py
data/
  .gitkeep
tests/
  test_scoring.py
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
