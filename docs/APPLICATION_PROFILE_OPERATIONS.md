# Application Profile Operations

Jobbot's application profile is separate from candidate scoring preferences.
The private YAML file contains owner metadata, a profile ID, and the approved
document catalog only. The profile ID scopes reusable field values encrypted in
TinyDB with the macOS Keychain owner key, keeping field replacement in one data
store. Document files remain owner-only under `credentials/`.

## Initialize

Initialize the sensitive-data key first, then create the private profile:

```bash
.venv/bin/python scripts/manage_sensitive_data.py initialize-key
.venv/bin/python scripts/manage_application_profile.py initialize \
  --profile-name Primary \
  --approval-name Owner
```

The default path comes from `application_profile` in `config.yaml`. The profile
directory must be dedicated and owner-only (`0700`); Jobbot refuses to change a
shared directory's permissions. The profile is mode `0600`. Initialization
generates an immutable random profile ID; separate profiles therefore receive
separate encrypted-field scopes and document-approval signatures.

## Add Encrypted Fields

Add fields one at a time. The command prompts without echoing the value, so the
value is not placed in shell history or command output:

```bash
.venv/bin/python scripts/manage_application_profile.py set-field legal_name \
  --category personal_identifier
.venv/bin/python scripts/manage_application_profile.py set-field email \
  --category contact
.venv/bin/python scripts/manage_application_profile.py set-field phone \
  --category contact
```

Replacing a field re-encrypts the stable scoped record in one database update.
Because retained database backups may still contain the old ciphertext, the
command reports when the documented backup purge should be considered.
High-sensitivity categories still require `--confirm-retention` and can never
use automatic reuse.

## Approve Documents

Catalog an approved resume by copying it into the private profile directory:

```bash
.venv/bin/python scripts/manage_application_profile.py add-document \
  /path/to/resume.pdf \
  --document-id software_engineering_resume \
  --kind resume \
  --label "Software engineering resume" \
  --revision 2026-08-21 \
  --approved-for software_engineering \
  --default
```

Approved formats are PDF, DOCX, RTF, and plain text, with a 20 MB maximum. The
catalog stores a SHA-256 fingerprint, purpose tags, revision, approver, approval
time, active state, and default-selection state. A Keychain-backed HMAC
authenticates that metadata, including the hash. Jobbot rejects changed files,
substituted metadata, symlinks, path traversal, duplicate IDs, and conflicting
defaults. Retiring a document keeps its audit metadata and file but prevents
future selection:

```bash
.venv/bin/python scripts/manage_application_profile.py \
  retire-document software_engineering_resume
```

A durable transaction marker lets the next document-add operation roll back an
orphaned copy or recognize a completed catalog commit after interruption.

## Validate and Inspect

Validation checks encrypted fields and decryptability, approval signatures,
file fingerprints, permissions, and readiness. A ready profile requires
encrypted `legal_name`, `email`, and `phone` fields plus at least one active
approved resume:

```bash
.venv/bin/python scripts/manage_application_profile.py validate
.venv/bin/python scripts/manage_application_profile.py validate --require-ready
.venv/bin/python scripts/manage_application_profile.py list
```

`list` verifies that encrypted fields can be decrypted, then returns only field
names and redacted document metadata. It never prints application-profile values.
