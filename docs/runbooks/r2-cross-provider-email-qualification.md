# R2 cross-provider email qualification

This matrix qualifies the synthetic Gmail and Yandex Mail paths through
provider-specific identity, canonical raw-message acceptance, bounded MIME
parsing, attachment lineage, citations, durable sync checkpoints and current
authorization. Provider identities remain distinct while the shared
`message/rfc822`, digest, parser profile and locator contracts converge.

The report is `docs/qualification/issue-863-cross-provider-email.json`, and its
synthetic fixture manifest is
`docs/qualification/issue-863-email-fixture-manifest.json`. The traceability gate is
`tests/qualification/test_issue863_cross_provider_email_qualification.py`.
Fixtures are synthetic. The suites use no network or Gmail/Yandex credentials
and do not access production data. PostgreSQL cases use the disposable
integration database configured by the repository test harness.

## Run

From the repository root, run the focused matrix and compatibility suites:

```bash
uv run pytest -q \
  tests/qualification/test_issue863_cross_provider_email_qualification.py \
  tests/qualification/test_issue827_email_canonical_ingestion.py \
  tests/qualification/test_issue835_gmail_canonical_observation.py \
  tests/qualification/test_issue840_gmail_message_state.py \
  tests/qualification/test_issue841_gmail_offline_qualification.py \
  tests/qualification/test_issue848_imap_canonical_observation.py \
  tests/qualification/test_issue853_yandex_mail_end_to_end.py \
  tests/application/test_email_sync.py \
  tests/application/test_gmail_initial_snapshot.py \
  tests/application/test_gmail_history_cursor_recovery.py \
  tests/application/test_gmail_message_state.py \
  tests/application/test_yandex_mail_uidvalidity_reset.py \
  tests/application/connections/test_email_read_admission.py \
  tests/application/test_email_artifacts.py \
  tests/application/test_email_attachments.py \
  tests/application/test_email_message_citations.py \
  tests/application/test_email_attachment_citations.py \
  tests/connectors/test_gmail.py \
  tests/connectors/test_gmail_history.py \
  tests/connectors/test_gmail_incremental.py \
  tests/connectors/test_yandex_mail.py \
  tests/connectors/test_yandex_mail_incremental.py \
  tests/connectors/test_yandex_mail_message_fetch.py \
  tests/ingestion/test_email_mime_parser.py \
  tests/integration/postgresql/test_email_sync_durability.py \
  tests/qualification/test_issue614_google_offline_qualification.py \
  tests/qualification/test_issue677_cross_provider_offline.py \
  tests/application/test_connector_ingestion.py \
  tests/test_architecture_scaffold.py
```

Check the generated API contract with `pnpm web:api:check`, then run the
repository verification gates in the root `AGENTS.md`.

## Accept

Accept offline `PASS` only when the focused command, PostgreSQL durability cases,
generated API check and repository gates pass, every traceability node exists,
and the report contains no credentials, mailbox identifiers, subjects, filenames,
message content or provider error text. Keep `live_provider_semantics` at
`NOT_VERIFIED` until separately authorized live Gmail and Yandex qualifications
are completed. A missing PostgreSQL prerequisite or skipped required test is
`NOT_VERIFIED`, not `PASS`.

The checked-in report records local and CI gate outcomes separately. The local
full pytest attempt was `NOT_VERIFIED` (7087 passed, 7 skipped, 6 failed) because
`ffprobe` was absent. Pull request CI installs `ffmpeg`; its full repository and
PostgreSQL gate passed, as did Python 3.10–3.14 and web checks.
