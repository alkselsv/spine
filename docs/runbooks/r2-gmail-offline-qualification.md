# R2 Gmail offline qualification

This qualification runs the Gmail connector through synthetic admission, initial
no-gap snapshot, incremental history, bounded stale-cursor reconciliation,
canonical message state, replay and safe diagnostics. It exercises deterministic
fakes only: no network, Gmail/Yandex credentials, real mailbox data or provider
payloads. The executable traceability gate is
`tests/qualification/test_issue841_gmail_offline_qualification.py`; the pinned
scenario report is
`docs/qualification/issue-841-gmail-offline-qualification.json`.

## Run

From the repository root, run the focused suites:

```bash
uv run pytest -q \
  tests/qualification/test_issue841_gmail_offline_qualification.py \
  tests/qualification/test_issue838_gmail_initial_snapshot.py \
  tests/qualification/test_issue839_gmail_history_cursor_recovery.py \
  tests/qualification/test_issue840_gmail_message_state.py \
  tests/application/test_gmail_initial_snapshot.py \
  tests/application/test_gmail_history_cursor_recovery.py \
  tests/application/test_gmail_message_state.py \
  tests/application/connections/test_email_read_admission.py \
  tests/connectors/test_gmail.py \
  tests/connectors/test_gmail_history.py \
  tests/connectors/test_gmail_incremental.py \
  tests/application/test_google_sync_diagnostics.py \
  tests/application/test_google_read_admission.py \
  tests/application/test_connector_ingestion.py \
  tests/integration/postgresql/test_email_sync_durability.py \
  tests/qualification/test_issue614_google_offline_qualification.py \
  tests/qualification/test_issue677_cross_provider_offline.py
```

Then run the repository verification gates in the root `AGENTS.md`.

## Accept

Accept the offline result only when the focused command and repository CI pass,
the traceability gate finds every evidence node, PostgreSQL durability cases run
in CI, and the Google Drive/Sheets plus Google v1/provider-v2 compatibility
regressions remain green. If a required PostgreSQL prerequisite is skipped, that
evidence is `NOT_VERIFIED`, not a pass.

The report pins fixture, adapter-contract and profile versions and records only
scenario names and safe test references. Offline `PASS` does not verify Gmail
service behavior. Live history retention, concurrency, quota and outage
semantics remain `NOT_VERIFIED` until separately authorized and exercised with
an isolated account and approved scope. Never add credentials, message subjects,
addresses, raw MIME, provider bodies or raw provider errors to the report.
