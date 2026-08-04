# Product Requirements: Human-in-the-Loop Job Application Assistant

## Product Vision

Jobbot is a Discord-operated job discovery and application
assistant. Its purpose is to help Matthew find and apply to newly posted jobs
quickly while he is away from his computer.

The desired experience is almost hands-free, but never fully autonomous.
Discord is the primary control surface because it is available on Matthew's
phone and other mobile devices. The assistant should keep working when it can,
provide concise progress updates, and pause whenever Matthew's judgment,
confirmation, or missing information is required.

## Non-Negotiable Principles

1. **Human control is mandatory.** The assistant may prepare and fill an
   application, but it must not submit one without Matthew's explicit approval.
2. **Unknown answers are never guessed.** If an application asks a question
   that does not have an approved answer, the assistant must ask Matthew in
   Discord and wait.
3. **Sensitive answers require stronger review.** Legal attestations,
   demographic information, disability or veteran status, sponsorship and work
   authorization, salary expectations, background disclosures, and any
   statement requiring a signature must be surfaced explicitly. Stored answers
   do not remove a confirmation requirement when the wording or context changes
   the meaning.
4. **Matthew can intervene at any time.** Every active application must be
   pausable, resumable, and cancellable from Discord.
5. **Every answer remains auditable.** Before submission, Matthew receives a
   complete review of the information and answers the assistant intends to
   submit.

## Target Workflow

1. Scan approved job sources frequently enough to prioritize fresh postings.
2. Validate, deduplicate, and score each posting.
3. Notify Matthew in Discord when a promising new job is found, including the
   company, role, location, source, posting age, fit score, important reasons,
   red flags, and link.
4. Ask for explicit approval before beginning application assistance.
5. Open and progress through the application while posting meaningful Discord
   updates, such as starting, signing in, entering a new section, waiting for
   input, recovering from an error, or becoming ready for review.
6. Reuse approved profile data and prior answers when the new question is a
   confident semantic match and the context has not changed.
7. For an unknown, ambiguous, changed, or sensitive question:
   - pause the application;
   - send the exact question and redacted context to Discord;
   - for non-sensitive questions, propose an answer only when useful, label it
     as a proposal, and wait for Matthew to answer or approve;
   - for sensitive questions, route raw input to an owner-only local surface;
   - record only answers permitted by the approved sensitivity and reuse policy.
8. When all fields are complete, prepare an owner-only local review package and
   send a redacted Discord summary containing:
   - the job and company;
   - the resume and other documents selected;
   - every application question and proposed answer, with sensitive values
     visible only in the local package;
   - which answers were reused, newly supplied, or generated and approved;
   - any unresolved warnings, unusual terms, or assistant uncertainty.
9. Wait for explicit final approval. Submission is a separate action from
   approving individual answers.
10. Submit only after final approval, then report the result and preserve an
    audit trail.

## Discord Interaction Requirements

- Messages should identify the job and current application state clearly.
- Routine progress should be concise and should not require a response.
- Requests for input should state exactly what is blocked and what response is
  needed.
- Confirmation prompts should offer unambiguous actions such as approve, edit,
  skip, pause, cancel, or resume.
- The assistant should acknowledge commands and report failures instead of
  silently retrying indefinitely.
- Duplicate notifications and repeated questions should be avoided.
- Discord must not request, receive, or echo a raw sensitive answer. It may link
  the owner to an owner-only local input surface and report only redacted state.
- If Discord is unavailable, the assistant must pause at the next decision or
  submission boundary rather than make decisions on Matthew's behalf.

## Answer Memory

The system should maintain a structured answer library rather than relying only
on chat history. Each saved answer should include:

- a normalized question or intent;
- the exact question originally asked;
- the approved answer;
- category and sensitivity;
- who approved it and when;
- the job/company context in which it was approved;
- whether it may be reused automatically, reused with confirmation, or never
  reused;
- revision history and the date it was last confirmed.

Matching must account for meaning and context, not just identical wording.
When confidence is insufficient, the assistant asks again. Matthew must be able
to view, correct, retire, or delete stored answers.

## Required Approval Gates

At minimum, the workflow must stop for:

- approval to begin application assistance;
- any unrecognized or ambiguous application question;
- sensitive or legally consequential questions as described above;
- material changes to resumes, cover letters, or claims about experience;
- unexpected application terms or requested commitments;
- the complete pre-submission review;
- final submission.

Approval for one gate must never be interpreted as approval for later gates.

## Audit and Recovery

For each application, store a timestamped event history containing progress,
questions, non-sensitive answers, approvals, edits, errors, retries, and
submission outcome. Sensitive-answer events retain the question, category,
approval outcome, approver, and time but redact the raw value. A sensitive value
needed to resume after restart must be requested again rather than recovered from
routine audit history.
The workflow should resume safely after a restart without duplicating an
application or losing a pending question. Secrets, credentials, and unnecessary
sensitive data must not appear in Discord messages or logs. Raw sensitive
answers must never pass through Discord.

## Explicit Non-Goals

- Unattended bulk auto-apply.
- Guessing answers to maximize application completion.
- Bypassing CAPTCHAs, anti-bot controls, site rules, or access restrictions.
- Fabricating qualifications, experience, education, or personal information.
- Treating a stored answer as permanently correct.
- Submitting an application without Matthew's final, application-specific
  approval.

## Definition of Success

Matthew can be away from his computer, receive a timely Discord notification
about a fresh job, authorize and guide the application from his phone, answer
only genuinely new questions, review every proposed answer, and explicitly
approve submission. The assistant reduces repetitive work without taking final
decision-making away from him.

## Development Quality Gate

Every major milestone requires focused tests, the full automated suite, and a
safe local preflight before review. The scoped branch is pushed to a draft pull
request and must pass GitHub CI before independent review and adversarial QA.
After approval and merge, production deployment and health verification close
the milestone. The detailed sequence is defined in
`.kiro/steering/review-workflow.md`.
