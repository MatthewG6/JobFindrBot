# Jobbot Decision Register

This is the durable record of product, architecture, safety, and sequencing
decisions. Chat history is not a source of truth. A material decision should be
added or updated here in the same change that updates the roadmap or code.

Statuses are `accepted`, `planned`, `proposed`, or `superseded`. Proposed
decisions require owner confirmation before implementation. A superseded entry
remains in this file and links to the decision that replaced it.

## D001 - Local, single-user V1

- Status: accepted
- Decision: V1 runs on one owner's local machine and stores private data locally.
- Consequence: Multi-user accounts and hosted execution are outside V1.

## D002 - LinkedIn and Indeed are discovery providers

- Status: accepted
- Decision: Jobbot ingests authenticated, labeled LinkedIn and Indeed alert
  emails. It does not scrape or automate either provider's website.
- Consequence: Provider emails supply discovery leads; Jobbot resolves and
  enriches jobs from official employer or ATS destinations.

## D003 - Thirty-minute orchestration with provider-aware intervals

- Status: accepted
- Decision: launchd starts Jobbot every 30 minutes while individual sources use
  longer intervals that respect their refresh rates and usage policies.
- Consequence: Fresh email alerts are handled quickly without polling every API
  every 30 minutes.

## D004 - Resolve an official destination before employer-page automation

- Status: accepted
- Decision: Discovery links remain provenance. Browser or employer-page work
  begins only after Jobbot has a validated public HTTPS employer or ATS URL.
- Consequence: Expanding official-destination resolution is the immediate V1
  bottleneck for unresolved email jobs.

## D005 - Prefer structured posting data over browser rendering

- Status: accepted
- Decision: Enrichment prefers captured official payloads, official ATS APIs,
  and static `JobPosting` JSON-LD. Read-only Playwright is reserved for approved
  JavaScript-rendered employer pages.
- Consequence: Dynamic rendering is a fallback and never operates on LinkedIn or
  Indeed pages.

## D006 - Discord first, local dashboard second

- Status: accepted
- Decision: Discord is the mobile notification and approval surface. A focused
  local dashboard will support review, labeling, answer maintenance, and audit
  work that benefits from a larger screen.
- Consequence: The dashboard is an operational V1 tool, not a marketing site.

## D007 - Separate start and submission approval

- Status: accepted
- Decision: Beginning application assistance and submitting an application are
  separate, application-specific approvals. Approval never carries forward to a
  later gate.
- Consequence: Jobbot may prepare and fill approved fields but cannot submit
  without final approval.

## D008 - Structured application profile and document catalog

- Status: planned
- Decision: Application data will live in a validated private profile, separate
  from job-search scoring preferences. Resume variants and other documents will
  be cataloged with purpose, revision, and approval metadata.
- Consequence: Browser assistance must not infer personal facts from chat history
  or choose documents without an auditable rule.

## D009 - Structured answer memory, not chat-only memory

- Status: planned
- Decision: Each approved answer will retain normalized intent, original
  question, answer, category, sensitivity, approver, job/company context, reuse
  policy, revision history, and last-confirmed date.
- Consequence: Users can view, edit, retire, and delete answers. An answer can be
  reused automatically, reused only after confirmation, or never reused.

## D010 - Unknown and sensitive answers stop the workflow

- Status: planned
- Decision: Answer matching must consider meaning and context with explicit
  confidence. Unknown, ambiguous, changed, or sensitive questions pause the
  application. Discord may carry the question and review state, but a raw
  sensitive value is entered only through an owner-only local surface.
- Consequence: Legal attestations, work authorization, salary, demographic,
  disability, veteran, background, and signature questions are never silently
  reused merely because a prior answer exists.

## D011 - Scoring claims require held-out evidence

- Status: accepted
- Decision: Scoring V2 will store dimensions, confidence, evidence, and version.
  Accuracy claims require at least 50 labeled jobs and a held-out validation set.
- Consequence: Jobbot will not claim 60% accuracy until the benchmark proves it.

## D012 - TinyDB for guarded local V1, SQLite before expanded concurrency

- Status: accepted
- Decision: TinyDB remains the local single-user V1 store. Existing local entry
  points serialize access with cross-process locking and atomic replacement. A
  local dashboard uses the existing application/storage boundary. Jobbot moves
  to SQLite before multi-user, hosted, or materially higher-concurrency use.
- Consequence: Schema migrations and backups remain mandatory during V1.

## D013 - Major milestones require independent gates

- Status: accepted
- Decision: Before review, run focused tests, the full suite, and a safe local
  preflight. Commit the change to a scoped branch, push a draft pull request, and
  pass GitHub CI. Independent code review and adversarial QA then gate merge to
  `develop`; production deployment and health verification close the milestone.
- Decision: Runtime work must happen outside the active scheduler checkout or the
  LaunchAgent must be stopped before runtime files are edited. It is restarted
  only after the reviewed commit is approved, merged, and passes CI.
- Consequence: A local pass alone is not sufficient. Findings are fixed on the
  review branch and gates rerun. A post-merge production failure requires a
  follow-up branch and pull request. Until Jobbot has an isolated release
  checkout, stopping and restarting launchd is a required manual gate.

## D014 - Documentation changes with decisions

- Status: accepted
- Decision: This register, the V1 roadmap, architecture, and product requirements
  are updated when a decision changes scope, order, safety, or data contracts.
- Consequence: Reminders from chat should reveal a documentation defect, not act
  as the long-term project record.

## D015 - Sensitive application-memory lifecycle

- Status: proposed; blocking D008-D010 implementation
- Decision: Reusable application-profile and answer values must be encrypted at
  rest with a key held outside the data store, preferably macOS Keychain.
  Demographic, disability, veteran, background, signature, and legal-attestation
  values default to no reusable retention. Stored sensitive values are always
  confirm-first or never-reuse, never automatic.
- Decision: Database backups may contain only encrypted sensitive values and use
  the existing limited retention. Deleting a sensitive value removes the active
  record immediately and must offer a documented purge of retained local backups.
  Document files remain owner-only and their catalog stores only necessary
  metadata.
- Decision: Routine logs and Discord interactions redact sensitive values.
  Discord may notify the owner that sensitive input is required, but raw
  sensitive values are entered only through an owner-only local surface and
  never pass through Discord. Entering a value does not authorize later reuse or
  submission.
- Decision: Application audit events never retain a raw sensitive answer. They
  retain the question, category, approval outcome, approver, time, and a redacted
  value marker. Sensitive input needed after a restart is requested again.
  Redacted audit metadata follows the application record's retention and is
  deleted when the owner deletes that application.
- Consequence: Answer-bank and application-profile persistence cannot begin until
  encryption, key recovery, backup purge, and redaction behavior have tests and
  the sensitive-data restore drill passes.
