# Jobbot Architecture

## Current and Target Flow

```mermaid
flowchart LR
    Gmail["LinkedIn and Indeed email alerts"] --> Ingest["Validate and ingest"]
    APIs["Public APIs and employer boards"] --> Ingest
    Ingest --> InitialScore["Initial scoring"]
    InitialScore --> Resolve["Employer-site resolver"]
    Resolve --> ProviderDynamic["Read-only provider rendering"]
    ProviderDynamic --> Enrich
    Resolve --> Enrich["Official posting enrichment"]
    Enrich --> EmployerDynamic["Allowlisted dynamic enrichment"]
    EmployerDynamic --> Rescore
    Enrich --> Rescore["Enriched rescoring"]
    InitialScore --> Discord["Discord review inbox"]
    Rescore --> Discord
    InitialScore --> Review["Human review"]
    Rescore --> Review
    Profile["Application profile and documents"] --> Apply
    Answers["Approved answer memory"] --> Apply
    Review --> Apply["Approved ATS assistance"]
    Apply --> Questions["Unknown or sensitive questions"]
    Questions --> Local["Owner-only sensitive input"]
    Questions --> Discord["Redacted review state"]
    Apply --> Submit["Explicit submission approval"]
```

The resolver is operational for captured official links, exact employer-board
matches, bounded Adzuna/Remotive/Himalayas destination extraction, and manual
handoff. Enrichment is operational for captured official payloads, Greenhouse
and Lever APIs, and matching employer-page `JobPosting` JSON-LD. Approved
JavaScript-dependent fallbacks use bounded read-only Playwright rendering.
Scoring V2 and its loopback-only labeling workflow are operational. The fixed
queue holds 25 calibration jobs followed by 50 held-out validation jobs. It
interleaves score strata without exposing predictions, binds each decision to an
immutable posting snapshot and fingerprint, and withholds validation results
until the holdout is complete. Completing the real labels, the broader
dashboard, and ATS assistance remain V1 work in progress.

The resolver retains every incoming URL as provenance. LinkedIn, Indeed, and
aggregator URLs remain discovery links; only validated public HTTPS employer/ATS
links become application URLs. Automatic matching requires one unambiguous exact
company/title match with a compatible location. Approved aggregator resolution
uses source-specific exact apply-link labels or validated redirects, round-robin
batching, public-DNS checks, pinned HTTPS, and cooldowns. LinkedIn and Indeed are
never requested.

Enrichment runs only after resolution. API and static-page responses have bounded
sizes, static requests pin TLS connections to a validated public address at each
redirect, and parsed posting identity must match the stored job. Successful
enrichment preserves the method and source URL, updates posting details, and
reruns deterministic scoring. Transient requests are deferred for retry;
terminal payload errors require manual review, and pages without unique static
posting data are routed to the approved dynamic-page stage.

Dynamic rendering uses a fresh non-persistent Chromium context and never clicks,
types, authenticates, or submits. Only same-host GET document/script/XHR/fetch
requests are allowed; the validated hostname is pinned to a public address and
all other resource classes and browser communication APIs are blocked. Provider
resolution is limited to the D016 sources. Employer enrichment requires an
explicit domain allowlist entry and a rendered posting identity match.

## Scoring

Scoring V2 is a deterministic, versioned model with normalized 0-to-100 role,
seniority, skills, location, and risk dimensions. The configured weights total
100. Phrase-boundary matching avoids partial-word hits, aliases are deduplicated,
and title-level seniority exclusions are evaluated in the title so ordinary
description language does not create a disqualification. Missing role evidence,
excluded seniority, explicit candidate risk, or an onsite conflict at a
remote-or-hybrid-required location caps the fit score below review. An
unconfirmed work arrangement at a constrained location caps the score below
Strong so it remains a manual-review candidate.

Structured workplace type is authoritative when enrichment provides it. Text is
a fallback with negation and technical-context filtering. Affirmative hybrid
language may include onsite days. Arrangement-level onsite language wins over a
Remote location label, while incidental onsite meetings, interviews, and duties
do not. A state-aware seven-county locality catalog resolves metro cities without
confusing explicit out-of-state namesakes. Pending application approval rechecks
the job's current scoring version and Strong threshold inside the same database
transaction as the transition so a historical candidate cannot outlive a
disqualifying rescore.

The private profile also requires preferred-location evidence for Strong. This
caps out-of-region onsite namesakes and other unsupported locations below Strong,
while retaining remote US, Rochester, and state-aware Twin Cities eligibility.

Confidence is calculated independently from fit using posting completeness and
evidence coverage. Each score stores its dimensions, matching, exclusion, or
uncertainty evidence, confidence band, scoring version, and the Review and
Strong thresholds used for caps. The storage boundary binds those values to the
active profile and recomputes the expected normalized or capped result before
accepting any score update. Initial ingestion and successful
enrichment use the same scorer. After enrichment, the scheduler upgrades stale
scores before Discord selects notifications. Schema v1 candidate profiles are
migrated in memory; schema v1 persisted scores are structurally backfilled by
schema v6 and then deterministically rescored. Historical jobs made reviewable
only by that upgrade are atomically recorded as Discord baselines, preventing a
scoring release from creating a notification backlog.

## Scoring Labels

The scoring-review API and HTML surface accept only loopback clients with a
local Host and Origin. The session queue and labels are separate private JSON
files guarded by one cross-process lock and atomic owner-only replacement. The
queue is deterministic and idempotent: restarting the API cannot silently
resample the holdout. Each entry stores its split, complete posting snapshot,
review URL, and posting fingerprint. Only snapshots with at least 500
description characters qualify. A session-level fingerprint binds every queue
entry and its review metadata. Persisted labels must be the exact ordered prefix
of that queue. Recording fails if the snapshot or session integrity is
invalid, the job was already labeled or not selected, or the decision was
submitted out of order. Predicted labels are queue metadata but are never
returned by the blind review endpoints. Calibration comparisons and validation
benchmarks score the frozen snapshots, so later scheduler enrichment cannot
change the sample. Calibration comparisons are available immediately;
validation metrics unlock only when all 50 held-out decisions exist.

## Application Memory

Application memory is planned but not yet operational. It will be separate from
job-scoring preferences and will contain a validated personal application
profile, approved document catalog, and structured answer library. Answers retain
their original wording, normalized intent, sensitivity, context, approval,
reuse policy, and revision history. Unknown or sensitive questions pause the
workflow. Discord carries review state, but raw sensitive values are entered
only through an owner-only local surface; chat history alone is never treated as
an approved answer source.

## Trust Boundaries

- LinkedIn and Indeed are discovery and email-notification providers only.
- Jobbot does not automate or scrape LinkedIn or Indeed pages.
- Official APIs and employer ATS endpoints are preferred over browser rendering.
- Read-only Playwright is permitted only for D016 provider resolution or after an
  official employer destination is known and explicitly allowlisted.
- Application automation remains behind start and submit approvals.
- Credentials, job history, labels, backups, and metrics remain local and private.

## Persistence

TinyDB is the V1 single-user store. Local Jobbot entry points serialize access
through cross-process locking and atomic file replacement. Every database carries
a schema version. Migrations create an owner-only snapshot before writing, and
the scheduler creates one retained daily backup. SQLite is required before
multi-user, hosted, or materially higher-concurrency operation. A local dashboard
must use the existing application/storage boundary rather than become an
independent uncoordinated database writer.

Schema v3 retains each source's posting snapshot alongside canonical jobs and URL
provenance. This allows richer official-board data to update a job first found in
an email without losing where either record came from. Schema v4 adds provider
resolution attempt counts, timestamps, cooldowns, and sanitized outcome types.
Schema v5 adds separate dynamic-resolution attempts and cooldowns.
Schema v6 adds scoring version, confidence, dimensions, and evidence. The
migration does not reinterpret legacy fit values; the scheduler's scoring stage
performs that separate deterministic upgrade before notifications.

Backup SHA-256 sidecars detect accidental corruption. They are not authenticated
and do not defend against a malicious local user who can rewrite both files; V1's
security boundary is the operating-system account and owner-only file permissions.

## Quality Gates

Before review, every major milestone requires focused tests, the full automated
suite, and a safe local preflight. The change is committed to a scoped branch,
pushed to a draft pull request, and must pass GitHub CI before independent code
review and adversarial QA. After approval and merge to `develop`, production
deployment and health verification close the milestone. Until the scheduler has
an isolated release checkout, runtime development requires stopping the
LaunchAgent before edits and restarting it only after merge and passing CI.

Material decisions and changes to these boundaries are retained in the
[decision register](DECISIONS.md).
