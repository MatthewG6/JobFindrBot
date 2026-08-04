# Jobbot V1 Definition of Done

V1 is a reliable, local, single-user job-search assistant. It discovers jobs,
resolves them to official employer postings where possible, produces evidence-
based fit scores, supports human review, and assists with applications without
submitting anything without explicit approval.

## Definition of Done

### Discovery and operations

- [x] Gmail ingestion for labeled LinkedIn and Indeed alerts
- [x] Compliant public and employer-board source adapters
- [x] Thirty-minute launchd schedule with provider-specific intervals
- [x] Deduplication, source attribution, and persistent local storage
- [x] Discord notifications with recovery and attention states
- [x] Structured scheduler metrics and automatic private backups
- [x] CI test workflow

### Resolution and enrichment

- [x] Distinguish discovery URLs from official application URLs
- [x] Match email jobs to existing official employer-board records
- [ ] Resolve official employer or ATS postings without automating LinkedIn or Indeed
- [x] Validate redirects, public destinations, provenance, and confidence
- [x] Enrich jobs through ATS APIs or static employer pages
- [ ] Add read-only Playwright rendering for approved dynamic employer pages
- [x] Provide a manual URL handoff when automatic resolution is not possible

### Scoring quality

- [x] Store candidate preferences in a validated profile
- [x] Provide calibration and validation benchmark tooling
- [ ] Replace keyword accumulation with normalized Scoring V2 dimensions
- [ ] Store fit score, confidence score, evidence, and scoring version
- [ ] Label at least 50 real jobs, including a held-out validation set
- [ ] Demonstrate at least 60% validation accuracy before making that claim
- [ ] Target at least 70% precision for jobs sent to the review inbox

### Review and application assistance

- [x] Application state machine with separate start and submit approvals
- [ ] Add a focused local review and labeling dashboard
- [ ] Support approved Playwright assistance on selected employer ATS platforms
- [ ] Reuse only reviewed, non-sensitive known answers
- [ ] Pause on unknown, sensitive, authentication, CAPTCHA, and submission steps
- [ ] Verify that no workflow can submit without explicit approval

### Release readiness

- [x] Owner-only OAuth, source, and Discord credential storage
- [x] Schema version metadata and pre-migration backups
- [x] Independent reviewer and adversarial QA gates for major milestones
- [ ] Add restore tooling and rehearse backup recovery
- [ ] Reconcile README, screenshots, architecture, and setup instructions
- [ ] Produce a redacted demo using fixture data
- [ ] Tag a reproducible V1 release

## Ordered Backlog

1. Employer-site resolver contracts, persistence, matching, and manual handoff
2. Employer posting enrichment and read-only dynamic-page rendering
3. Scoring V2 dimensions, confidence, evidence, and score-version migration
4. Labeling workflow and held-out scoring benchmark
5. Local review dashboard
6. Approved ATS form-assistance adapters
7. Restore drill, end-to-end release QA, and V1 documentation

## Later Versions

SQLite replaces TinyDB before multiple users, multiple worker processes, or a
hosted dashboard. Multi-user accounts, cloud execution, and broad ATS coverage
are explicitly outside V1.
