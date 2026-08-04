# Jobbot

Jobbot is a local, Discord-operated, human-in-the-loop assistant for finding,
scoring, reviewing, tracking, and applying to software engineering jobs.

The project is designed to run locally on a personal computer while Matthew
controls it remotely through Discord. It should make the application process
almost hands-free, but it is not an auto-apply bot: unknown questions,
consequential decisions, and every final submission require human review.

The durable product contract, target Discord workflow, answer-memory rules, and
approval gates are defined in
[docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md).

## Project Goals

- Find junior-friendly software engineering job postings.
- Score jobs based on how well they match Matthew Glassman's target roles and locations.
- Save job postings in a simple local database.
- Notify when a posting looks like a strong fit.
- Coordinate application progress, questions, and confirmations through Discord.
- Remember approved answers so Matthew is not repeatedly asked the same thing.
- Present every proposed answer for final review before submission.
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
- BeautifulSoup and requests for API response normalization
- pytest for tests
- macOS launchd for scheduled scans
- Discord incoming webhook for high-fit job notifications
- Discord bot for notifications, commands, questions, and approvals

Not planned for the early version:

- React
- Docker
- PostgreSQL
- AI-based scoring
- Browser automation
- Auto-apply behavior

## Current Status

Jobbot runs locally every 30 minutes. It ingests labeled LinkedIn and Indeed
alerts from Gmail, scans compliant job APIs and employer boards at provider-safe
intervals, preserves every source link, resolves high-confidence matches to
official application URLs, scores jobs, and sends new high-fit results to
Discord. Application records require separate approval to start and submit.

Full-posting enrichment, Scoring V2, the review dashboard, and approved ATS
assistance remain V1 work in progress. See the
[V1 definition of done](docs/V1_ROADMAP.md) and
[architecture](docs/ARCHITECTURE.md).

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

The Discord foundation is also available as an explicitly configured local
process. It currently exposes only `/status`, `/help`, and
`/test-notification`. None of these commands can begin, advance, or submit an
application.

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

### 3. Public job APIs

The first real source uses the official [Himalayas public Jobs API](https://himalayas.app/docs/remote-jobs-api). It makes one filtered request for recent entry-level software engineering jobs and sends parsed results through the existing ingestion pipeline.

Run it manually:

```bash
python scripts/run_himalayas_scan.py
```

Himalayas data is refreshed every 24 hours, so this scanner should not run more than once per day. Job links point back to [Himalayas](https://himalayas.app), and the source is stored as `himalayas`.

The app also supports these compliant job sources:

| Source | Authentication | Scheduled interval | Search scope |
| --- | --- | --- | --- |
| Remotive | None | 6 hours | Remote software development |
| Greenhouse employer boards | None | 6 hours | Target titles in US/remote locations |
| Lever employer boards | None | 6 hours | Target titles in US/remote locations |
| Adzuna | App ID and key | 6 hours | Recent Minnesota software roles |
| USAJOBS | API key and registration email | 6 hours | Recent Minnesota public/graduate roles |

Remotive results retain their Remotive links and source attribution. Employer
boards are configured in `config/employer_watchlist.json`; only public
Greenhouse and Lever job-board APIs are used. The adapters do not scrape
LinkedIn or Indeed pages.

Run the public sources manually:

```bash
python scripts/run_remotive_scan.py
python scripts/run_employer_watchlist_scan.py
```

Adzuna and USAJOBS require local credentials. Create
`credentials/source_api.json` from
`config/source_credentials.example.json`, fill in the values, and keep the file
local. Jobbot repairs the directory to mode `0700` and the file to `0600` before
reading it. This private JSON file is required for scheduled launchd scans.
Environment variables are supported only for manual terminal runs because
launchd does not inherit shell environment variables:

```text
ADZUNA_APP_ID
ADZUNA_APP_KEY
USAJOBS_API_KEY
USAJOBS_USER_AGENT
```

Then verify each credentialed source manually:

```bash
python scripts/run_adzuna_scan.py
python scripts/run_usajobs_scan.py
```

Any real scanner must respect site terms, avoid aggressive scraping, never perform auto-apply behavior, and still send parsed jobs through the existing ingestion pipeline.

### 4. Employer-site resolution

Every ingested URL is stored with its source and classified as discovery or
official provenance. After source scans, Jobbot resolves email-discovered jobs
when exactly one official Greenhouse, Lever, or USAJOBS posting matches the same
normalized company, title, and compatible location. Ambiguous matches require
manual review; unresolved jobs remain pending for later scans.

Run the deterministic resolver manually:

```bash
python scripts/run_employer_resolution.py
```

Provide a reviewed employer or ATS URL when no automatic match exists:

```bash
python scripts/run_employer_resolution.py \
  --job-id JOB_ID \
  --application-url https://careers.example.com/jobs/JOB_ID
```

Manual URLs must be public HTTPS destinations and cannot point back to LinkedIn,
Indeed, or another discovery aggregator. This stage does not visit provider pages
or launch Playwright.

### 5. Job alert email ingestion foundation

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

## Scheduled Scans

The macOS launchd scheduler starts on login and wakes every 30 minutes. LinkedIn
and Indeed alert emails are checked every cycle. The same process runs each API
only when its provider-specific interval is due: Himalayas every 24 hours and
Remotive, employer watchlists, Adzuna, and USAJOBS every 6 hours. Credentialed
sources report `not configured` without failing scheduler health until their
keys are present. Attempt timestamps enforce those intervals after failures as
well as successes, preventing a 30-minute retry loop from violating provider
request limits.

Generate the local LaunchAgent configuration with:

```bash
python scripts/install_launchd_scheduler.py
```

The installer reloads the LaunchAgent, starts it immediately, and waits for a
successful background preflight. This preflight also detects macOS privacy
restrictions that could block a background process from accessing the project
under `Desktop`.

The scheduled entry point is `scripts/run_scheduled_scan.py`. It uses the local
read-only Gmail token without opening an interactive browser, prevents overlapping
runs, stores source timing in `data/scheduler_state.json`, and writes local output
under `logs/`.

Every production scheduler run also creates at most one owner-only database backup
per day under `data/backups/`, retains the newest 14 daily backups, and appends a
sanitized structured health record to `data/metrics/scheduler_runs.jsonl`. TinyDB
schema metadata is versioned; a separate snapshot is created before migrations.

## Candidate Profile and Scoring Benchmark

Personal role, technology, location, penalty, weight, and threshold settings live
in the private `credentials/candidate_profile.yaml`, created from
`config/candidate_profile.example.yaml`. The current values preserve legacy scoring
behavior until Scoring V2 is implemented and calibrated.

```bash
cp config/candidate_profile.example.yaml credentials/candidate_profile.yaml
chmod 600 credentials/candidate_profile.yaml
```

Create a private label file from `config/scoring_labels.example.json`, assign real
jobs to `strong`, `review`, or `reject`, and keep calibration labels separate from
the held-out validation labels. Evaluate aggregate metrics with:

```bash
.venv/bin/python scripts/run_scoring_benchmark.py \
  --labels data/scoring_labels.json \
  --split validation
```

Jobbot will not claim a scoring accuracy rate until enough real validation labels
exist and the benchmark demonstrates it.

## Discord Notifications

Discord is Jobbot's outbound review inbox. After every scheduled scan, Jobbot
posts newly discovered jobs with a fit score of at least 40 to a private Discord
channel. Each message includes the role, company, location, source, fit reasons,
watch-outs, salary when available, and a public job link. Discord does not approve,
answer, or submit applications in V1.

Create and connect the webhook:

1. Create a private Discord channel named `jobbot-alerts`.
2. Open **Edit Channel**, then **Integrations** and **Webhooks**.
3. Select **New Webhook**, name it `Jobbot`, choose `jobbot-alerts`, and copy its
   webhook URL.
4. Create the private local file from the example:

   ```bash
   cp config/discord_credentials.example.json credentials/discord.json
   chmod 600 credentials/discord.json
   ```

5. Replace the example value in `credentials/discord.json` with the copied URL.
   Do not paste this URL into chat, logs, source control, or screenshots; it grants
   permission to post into the channel.
6. Send a connection test:

   ```bash
   .venv/bin/python scripts/run_discord_notifications.py --test
   ```

7. Initialize notification tracking:

   ```bash
   .venv/bin/python scripts/run_discord_notifications.py
   ```

The initialization command records existing qualifying jobs without posting them,
preventing an alert flood. Later 30-minute scans notify only newly qualifying jobs,
up to 10 per cycle. Delivered jobs are recorded so they are not posted twice.
Discord rate-limit cooldowns are honored before another delivery attempt. If a
request has an uncertain outcome, Jobbot does not retry it automatically; this
favors avoiding duplicate alerts over guaranteed delivery. If Discord reports a
deleted or unauthorized webhook, Jobbot disables delivery until the webhook URL
is replaced.

If scheduler health reports a notification requiring attention, inspect it with:

```bash
.venv/bin/python scripts/run_discord_notifications.py --list-attention
```

After checking the Discord channel, explicitly resolve each uncertain job as
delivered or retryable:

```bash
.venv/bin/python scripts/run_discord_notifications.py --mark-delivered JOB_ID
.venv/bin/python scripts/run_discord_notifications.py --retry JOB_ID
```

## What This Does Not Do Yet

- No direct LinkedIn or Indeed page scraping.
- No Discord approval commands or interactive application controls.
- Gmail OAuth requires one-time local browser authorization.
- No auto-apply behavior.
- No browser automation.
- No AI scoring.

## Discord Foundation

The Discord bot is restricted to one configured user, guild, and channel. Its
slash commands are synchronized only to that guild. Unauthorized commands are
rejected ephemerally, processed interaction IDs are persisted to prevent
duplicate work, and message context storage survives local restarts.

Create a Discord application and bot in the Discord Developer Portal, install
it in your private server with permission to use application commands and send
messages, and configure:

```text
DISCORD_BOT_TOKEN
DISCORD_APPLICATION_ID
DISCORD_GUILD_ID
DISCORD_CHANNEL_ID
DISCORD_ALLOWED_USER_ID
```

The values are documented in `.env.example`. Keep the real token in the
environment or a local `.env` file; `.env` is ignored by git. The application
does not automatically load `.env`, so export the variables or use a trusted
local process manager that loads them.

Start the bot:

```bash
python scripts/run_discord_bot.py
```

The process validates all required configuration before connecting. It uses no
privileged Discord gateway intents. Keep `discord.enabled` and
`discord.application_actions_enabled` set to `false` in `config.yaml` until a
later milestone explicitly wires runtime orchestration and approval gates.

## Project Structure

```text
app/
  __init__.py
  candidate_profile.py
  discord_bot.py
  discord_config.py
  discord_notifications.py
  discord_service.py
  email_ingestion.py
  employer_resolver.py
  job_links.py
  job_sources.py
  main.py
  models.py
  operational_metrics.py
  scoring_benchmark.py
  storage.py
  scoring.py
  dedupe.py
  ingestion.py
  scanner.py
config/
  candidate_profile.example.yaml
  employer_watchlist.json
docs/
  ARCHITECTURE.md
  PRODUCT_REQUIREMENTS.md
  V1_ROADMAP.md
data/
  .gitkeep
scripts/
  run_discord_bot.py
  run_employer_resolution.py
  run_scheduled_scan.py
  run_scoring_benchmark.py
tests/
  fixtures/
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
- Treat Discord as the primary remote control and status interface.
- Never submit a job application without explicit, application-specific final
  approval.
- Never guess legally sensitive answers.
- Pause and ask in Discord when it encounters an unknown, ambiguous, changed,
  or sensitive question.
- Reuse stored answers only according to their approval and sensitivity rules.
- Show every question and proposed answer in a final pre-submission review.
- Keep browser automation behind the approval gates defined in the
  [product requirements](docs/PRODUCT_REQUIREMENTS.md).

## Development Notes

The code should favor simple, readable modules over complex architecture. Each feature should be small enough to understand, test, and explain as part of a portfolio project.
