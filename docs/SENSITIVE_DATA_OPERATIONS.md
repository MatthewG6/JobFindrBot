# Sensitive Data Operations

Jobbot encrypts retained application-profile and answer values with AES-256-GCM.
The authenticated encryption context binds ciphertext to its record, scope, and
field. The 256-bit owner key lives in macOS Keychain under service
`com.jobfindrbot.sensitive-data`; it is not stored in TinyDB or database backups.

## Initialize the Key

Run this once from the project virtual environment:

```bash
.venv/bin/python scripts/manage_sensitive_data.py initialize-key
```

The command creates a random key only when the Keychain item is genuinely
missing. A malformed existing item fails closed and is not replaced.

## Create and Verify Recovery

Export recovery to an encrypted external drive or password-manager attachment,
not inside the Jobbot project, `credentials/`, `data/`, or a synced plaintext
folder:

```bash
.venv/bin/python scripts/manage_sensitive_data.py export-recovery /secure/offline/jobbot-recovery.json
.venv/bin/python scripts/manage_sensitive_data.py verify-recovery /secure/offline/jobbot-recovery.json data/jobs.json
```

The recovery package contains the raw encryption key and is written mode `0600`.
Anyone with that package and a database backup can decrypt retained sensitive
values. The verification command authenticates every encrypted record without
printing plaintext.

To restore the key after replacing or repairing the Mac:

```bash
.venv/bin/python scripts/manage_sensitive_data.py restore-recovery /secure/offline/jobbot-recovery.json
```

Run `verify-recovery` against the restored database before restarting Jobbot.

## Retention and Deletion

Demographic, disability, veteran, background, signature, and legal-attestation
values are not retained unless the owner explicitly confirms retention. Any
retained sensitive value is `confirm_first` or `never_reuse`; automatic reuse is
not a valid sensitive-value policy.

Deleting an active sensitive value removes it immediately and reports whether
backup purging is still recommended. Because Jobbot backups are complete
database snapshots, purging removes every retained snapshot for that database:

```bash
.venv/bin/python scripts/manage_sensitive_data.py purge-backups \
  --database data/jobs.json \
  --confirm DELETE-JOBBOT-BACKUPS
```

The next scheduled run creates a new backup containing the post-deletion state.
Application deletion also removes its redacted audit history and encrypted
application-scoped values.

## Audit and Messaging Boundary

Sensitive application events retain only the exact question, category, approval
outcome, approver, timestamp, and `[REDACTED]` marker. Raw values are not accepted
by that audit API. Discord may announce that local sensitive input is required,
but raw sensitive input must never be sent through Discord or routine logs.
