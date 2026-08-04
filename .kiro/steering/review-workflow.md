# JobFindrBot Solo Pull Request Review Workflow

This workflow creates independent implementation, code-review, and QA gates for
a project with one human developer. A real pull request is the durable record
even when Matthew is the only person who merges it.

## Participants

- **Matthew:** Product owner and final decision-maker.
- **Implementation agent:** Plans, tests, implements, fixes, and maintains the
  branch and pull request.
- **Hybrid Reviewer:** Read-only independent reviewer defined in
  `.kiro/agents/hybrid-reviewer.md`.
- **QA agent:** Read-only behavioral verifier that returns reproducible evidence
  and does not repair its own findings.

The implementation agent must not impersonate the Hybrid Reviewer or QA agent.
Each gate should receive the same task and acceptance criteria but evaluate the
change from its own role.

## QA Agent Requirement

Independent adversarial QA is required for every major milestone. It is
acceptance-criteria-driven, read-only, unable to repair defects, and returns
`PASS`, `FAIL`, or `BLOCKED` with reproducible evidence.

## Branch and Pull Request Policy

Use:

- `main` for stable releases.
- `develop` as the integration branch.
- `feature/*`, `fix/*`, or `chore/*` for scoped work.

Meaningful changes should reach `develop` through a pull request. The pull
request supplies a stable base-to-head diff, records verification and review,
and makes later investigation possible.

Do not combine unrelated features in one PR. Large changes should be split when
each part can be reviewed and tested independently.

## Phase 1: Define and Implement

Before coding, the implementation agent records:

- problem and desired outcome;
- explicit acceptance criteria;
- relevant requirements or issue;
- in-scope and out-of-scope behavior;
- expected tests;
- safety or migration concerns.

Implementation follows a red-green-refactor approach for behavioral logic:

1. Add or update a failing test.
2. Implement the smallest correct change.
3. Refactor while tests remain green.

Before requesting review, the implementation agent must:

1. Inspect the complete branch diff.
2. Run focused tests and the full test suite.
3. Run any relevant diagnostics or safe build checks.
4. Confirm each acceptance criterion.
5. Commit and push the branch.
6. Open or update a draft pull request.
7. Wait for required CI checks to pass.
8. Complete the review packet below.

### Required Review Packet

```text
Base branch/commit:
Review branch/commit:
Pull request:
Task/spec:

Acceptance criteria:
- ...

Implementation summary:
- ...

Verification performed:
- command — result

Known limitations or deferred work:
- ...
```

If implementation is intentionally incomplete, label the PR as draft and say
which criteria remain. Do not request a merge-readiness verdict on unfinished
work.

## Phase 2: Independent Hybrid Review

Invoke the Hybrid Reviewer with the completed packet. The reviewer:

1. Verifies the comparison baseline.
2. Reads the complete diff and relevant surrounding code.
3. Checks requirements, human-in-the-loop safety, correctness, tests, security,
   persistence, and operational risks.
4. Runs safe read-only verification.
5. Produces evidence-backed findings with acceptance conditions.
6. Returns BLOCK, CONDITIONAL PASS, PASS WITH OBSERVATIONS, or PASS.
7. Supplies a QA handoff when the result is passing.

The reviewer never edits the branch.

### Clean Review

If the verdict is PASS, skip directly to Phase 4.

If the verdict is PASS WITH OBSERVATIONS, the implementation agent records
whether each observation is accepted now or intentionally deferred, then moves
to Phase 4.

## Phase 3: Implementation Response and Targeted Re-Review

The implementation agent responds to every finding using exactly one category:

- **Accepted:** The finding is valid. State the fix and verification.
- **Pushback:** The finding is incorrect or out of scope. Give concise
  file/test/spec evidence.
- **Needs Matt decision:** Product, safety, or architecture judgment is required.
- **Deferred:** A non-blocking observation will not be addressed in this PR.
  State why and where it will be tracked.

The implementation agent must not silently ignore findings or weaken a
non-negotiable product safety rule.

After accepted blockers are fixed:

1. Run focused and full verification again.
2. Commit and push the fixes.
3. Update the PR with the response and evidence.
4. Invoke the reviewer for one targeted re-review.

The re-review packet contains:

- the original blocker;
- the implementation response;
- fix commits;
- commands and results;
- any disputed items.

The reviewer checks resolved blockers, disputes, and newly introduced risks. It
then returns a final verdict.

Two reviewer rounds are the normal maximum. Unresolved disagreement after the
second round goes to Matthew; neither agent decides unilaterally.

## Phase 4: QA Gate

A code-review pass means ready for QA, not ready to merge.

The QA gate validates behavior from the user and system perspective using:

- task acceptance criteria;
- the reviewer’s QA handoff;
- supported user journeys;
- failure and recovery behavior;
- authorization and approval boundaries;
- idempotency and duplicate-event handling;
- relevant regression tests.

The QA result is:

- **PASS:** All required behavioral checks succeeded.
- **FAIL:** A reproducible defect blocks merge.
- **BLOCKED:** Required environment, credentials, fixture, or human decision is
  unavailable.

Every failure must include:

- environment and starting state;
- exact reproduction steps;
- expected result;
- actual result;
- relevant logs, screenshots, or response data with secrets removed;
- severity and affected acceptance criterion.

The QA agent does not repair defects. A FAIL returns to the implementation
agent, followed by focused code re-review when the fix materially changes the
implementation and then QA re-check.

## Phase 5: Merge Decision

A PR may merge into `develop` only when:

- CI and required checks pass;
- no Hybrid Reviewer Must Fix finding remains;
- all Matthew decisions are resolved;
- QA passes;
- the PR describes tests and known limitations;
- no unexpected uncommitted work is part of the result.

Matthew makes the final merge decision. After merge, delete the feature branch
unless it contains explicitly planned ongoing work.

Promotion from `develop` to `main` should use a separate release PR with focused
regression evidence.

## Phase 6: Production Verification

After merge to `develop`, deploy or reload the reviewed commit. Verify scheduler
health, structured metrics, logs, migrations, backups, and any milestone-specific
production behavior. A failed deployment or health gate requires a follow-up
branch and pull request; the milestone remains open until production passes.

Runtime work must not happen in the checkout used by the active scheduler. Until
an isolated release checkout exists, stop the LaunchAgent before editing runtime
files and restart it only after merge and successful CI.

## PR Summary Template

```text
## What changed
- ...

## Why
- ...

## Acceptance criteria
- [ ] ...

## Verification
- command — result

## Hybrid review
- Verdict:
- Blocking findings resolved:
- Deferred observations:
- Decisions from Matthew:

## QA
- Result:
- Scenarios exercised:
- Evidence:

## Known limitations
- ...
```

## Workflow Guardrails

- Do not let the implementation agent approve its own diff under the reviewer’s
  name.
- Do not treat passing unit tests as complete QA.
- Do not merge merely because a reviewer found no code issue.
- Do not allow one approval to authorize later application workflow gates.
- Do not expose secrets or applicant data in PRs, logs, screenshots, or review
  output.
- Do not turn advisory observations into blockers without a concrete failure
  mode.
- Keep review scope tied to the task and diff; track unrelated debt separately.
