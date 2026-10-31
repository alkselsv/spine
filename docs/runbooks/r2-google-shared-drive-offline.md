# R2 Google Shared Drive offline conformance

Issue #756 adds fake-backed conformance checks around the existing Drive and
Sheets adapters. This runbook describes only deterministic offline behavior.
It does not require a Workspace account, credentials, a real Shared Drive or
Google network access.

## Verify

Run the focused matrix and its underlying adapter/coordinator tests:

```bash
uv run pytest -q \
  tests/connectors/test_google_api.py \
  tests/application/test_google_drive_snapshot.py \
  tests/application/test_google_drive_changes.py \
  tests/application/test_google_read_admission.py \
  tests/qualification/test_google_live_harness.py \
  tests/qualification/test_issue756_shared_drive_conformance.py
```

The fakes use synthetic values. Tests assert `driveId`, `corpora=drive`,
`supportsAllDrives=true`, `includeItemsFromAllDrives=true` and
`restrictToMyDrive=false` on the applicable requests. List and change responses
must carry the exact drive identity before file observations are admitted.
Unknown or foreign drive identity makes the page incomplete, so a terminal
provider token cannot be committed from it.

Configured spreadsheet IDs and numeric sheet IDs remain an independent exact
allowlist. A Shared Drive scope does not grant access to every spreadsheet in
that drive. Mismatched resource, workspace, environment or configuration
generation requests fail before credential resolution and transport calls.

Snapshot and change coordinators retain their durable cursor when a page is
incomplete, access is lost, or a worker is cancelled. Replaying a cancelled
command resumes from the last durable checkpoint and preserves idempotent
effects.

## Live preflight boundary

`shared_drive_approved` is only a configuration assertion. For a live Shared
Drive run, the harness also requires an external
`GoogleSharedDrivePreflightPort`. It resolves an approval decision and a
separate execution authorization, both bound to the exact workspace,
environment, opaque resource-scope reference, Shared Drive ID, connection,
authorization/configuration generations and file/spreadsheet allowlists. The
harness compares those bindings before invoking `GoogleReadAdmission`. Missing,
denied, mismatched or unavailable preflight evidence refuses the run before a
provider request, with zero provider calls.

The issue #756 fake-backed run report always records
`shared_drive_status: NOT_EXERCISED`. No offline test or report is a live
qualification PASS. The evidence index is
`docs/qualification/issue-756-google-shared-drive-offline-report.json`.

## Future live qualification inputs

Actual Shared Drive qualification remains in #615 and requires an approved
Workspace/environment, an accessible isolated synthetic Shared Drive, a
privately approved exact resource ID and allowlist, the corresponding owner
decision, a separate live execution authorization, real API evidence and
cleanup evidence. This offline task provisions none of those inputs and makes
no Google calls. Live Shared Drive behavior remains unverified until that
separate qualification is completed.
