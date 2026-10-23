# R1 five-format ingestion qualification

This runbook exercises Issue #561 against an operator-approved isolated
deployment. It never substitutes local fakes or direct database edits for the
deployed administrator UI/API path.

## Preconditions

- Q16-01 is PASS for the exact commit, deployment fingerprint, provider profile,
  workspace/environment pair and authorization generation used by this run.
- The Data Owner has approved the synthetic corpus and upload scope.
- Runtime/Security owners have supplied current trusted upload authority and
  short-lived credentials out of band.
- `SPINE_INGESTION_QUALIFICATION_CORPUS_DIR` names an approved directory outside
  Git containing exactly one PDF, DOCX, XLSX, Markdown and TXT source.
- `SPINE_INGESTION_QUALIFICATION_REPORT_PATH` points at the metadata-only report.
- `SPINE_INGESTION_Q16_REPORT_PATH` points at the committed metadata-only #560
  PASS report for the exact target. Before installing browser credentials or
  performing any mutation, the runner verifies the report schema/issue, commit,
  deployment fingerprint, API/Web releases, provider profile, Workspace,
  Environment and authorization generation.

The runner also requires the existing `SPINE_E2E_*` deployment, identity,
workspace and environment bindings documented by the governed-Q&A runbook.
Never paste their values into GitHub, logs or committed files.

## Execute

From `web/`, run the issue-specific Playwright test against the already started
deployment:

```bash
corepack pnpm exec playwright test \
  --config playwright.production.config.ts \
  e2e/production/five-format-ingestion.spec.ts
```

The Playwright profile must retain `trace: 'off'`, `screenshot: 'off'` and
`video: 'off'`. Route fulfillment and API mocking are forbidden. User mutations
are made through the administrator UI/API; operator failure injection must use
only the approved disposable deployment procedure and must never edit canonical
PostgreSQL or original-storage records directly.

## Accept

Accept only a metadata-only report with `status = pass`, all five formats, all
required lifecycle/failure/isolation scenarios, current Q16-01 compatibility
and no protected filenames, content, credentials, endpoints, provider payloads
or raw exceptions. Missing prerequisites remain `blocked`; an unexercised edge
remains `not_verified`; an invariant violation is `fail`.
