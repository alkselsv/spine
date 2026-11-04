# R2 Google Drive/Sheets API adapters

Ticket #652 adds one concrete REST implementation of the existing
`GoogleDriveService`, `GoogleDriveSnapshotService`, `GoogleDriveChangeService`
and `GoogleSheetsService` ports. It remains behind `GoogleReadAdmission` and the
existing production readers; canonical admission, ingestion, acceptance and
outbox processing are unchanged.

## Selected client and provider versions

- HTTP/OAuth client: declared application dependency `httpx>=0.27,<1`; the
  current `uv.lock` resolves `0.28.1`.
- Google Drive REST API: v3.
- Google Sheets REST API: v4.
- OAuth mode: server-side OAuth 2.0 refresh grant. Production provider reads are
  registration-type neutral and resolve secrets through separate opaque
  credential and grant references. The isolated #615 Phase 1 qualification uses
  a Web application client under its narrower owner-approved policy.

No Google SDK is added. The REST endpoints and the provider/configuration
version evidence are required runtime inputs rather than hardcoded policy.

## Configuration contract

`GoogleApiService` requires all of the following:

- `GoogleApiEndpoints`: HTTPS Drive, Sheets and OAuth token endpoints, the exact
  approved OAuth scopes, and the provider API/configuration version labels;
- `GoogleApiRequestPolicy`: timeout, maximum attempts, every backoff delay,
  Drive page size and Sheets page size; it has no numeric policy defaults;
- one or more `GoogleResourceScope` records bound to the exact workspace,
  environment, connection, authorization generation, configuration generation
  and opaque scope reference;
- an exact file/spreadsheet allowlist and, where enumeration is approved, the
  exact folder or Shared Drive ID;
- configured `GoogleSheetsReadProfile` values for every spreadsheet/numeric
  sheet ID, business-ID column, selected representations and optional Developer
  Metadata key;
- a `ReferenceGoogleCredentialResolver` backed by an approved secret store;
- an injected HTTP transport, clock and sleep function;
- a `GoogleApiRetryObserver` that persists sanitized, tenant-scoped retry
  evidence for each low-level OAuth/provider retry. The existing sync
  coordinators remain responsible for durable business-level retry outcomes.

The adapter checks scope and generation before resolving credentials or making
an HTTP request. Refresh-token rotation or reauthorization uses a new immutable
connection generation/reference pair and therefore a different token-cache key.
Provider authorization revocation maps to the existing suspension path. Raw
credential/provider values and exception messages do not cross the adapter.

## Offline verification

Ordinary tests use synthetic responses and make no network calls:

```bash
uv run pytest -q \
  tests/connectors/test_google_api.py \
  tests/qualification/test_google_live_harness.py \
  tests/application/test_google_read_admission.py \
  tests/application/test_google_drive_reader.py \
  tests/application/test_google_drive_snapshot.py \
  tests/application/test_google_drive_changes.py \
  tests/application/test_google_sheets_reader.py \
  tests/application/test_google_sheets_reconciliation.py
```

The suite covers request mapping, download/export, Drive snapshot and Changes
pagination, Sheets representations and pagination, Developer Metadata, exact
scope/generation denial, OAuth resolution/revocation, bounded retry/backoff and
sanitized live evidence.

Drive v3 `Change` does not expose the v2-style `changeId` field. The adapter's
partial-response selector therefore requests only documented v3 fields and
derives the replay key from the file ID, requested file version and removal
state. A removal without a returned file resource uses the existing conservative
`unknown` version component; it does not invent provider identity or establish
deletion authority. See the official
[`Change` resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes).
The v3 `ChangeList` response likewise has no `incompleteSearch` field: its
top-level selector is limited to `nextPageToken`, `newStartPageToken` and
`changes`. Spine's `DriveChangePage.incomplete_search` remains an internal
conservative signal and is set when exact folder membership cannot be proved;
it is not read from an undocumented provider field. See the official
[`changes.list` response](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list#response-body).
The exact post-#934 live Changes response remains unverified until the fix is
merged, post-merge preflight passes and #615 records a new one-shot owner
authorization.

## Manual live qualification

Live access is forbidden unless both gates are present:

1. the JSON config contains `"opt_in": true` and a complete exact allowlist;
2. the process has `SPINE_GOOGLE_LIVE_OPT_IN=1`.

The config names (but does not contain) the approved credential inputs. The
connection contains only opaque references. The private manifest pins the exact
canonical workspace, environment, connection/version, approval, service
principal, credential/grant/resource-scope references and authority generations;
it never derives authority from provider IDs. `--composition module:function`
must name a deployment-owned factory returning `GoogleLiveQualificationRunner`.
That factory resolves secrets through the approved store, supplies the trusted
persistence context and current connection repository, and wires the provider
through `compose_google_live_qualification_runner`. The admitted current
connection must exactly equal the approved live manifest before any provider
request. The runner also verifies a SHA-256 fingerprint of the complete
non-secret effective endpoints, OAuth scopes, request policy, resource scope and
Sheets profiles, and the CLI requires the factory's manifest to equal the input
manifest. Missing/mismatched references, scope, generations, current version,
configuration fingerprint, opt-in or composition refuse the run before a
provider request.

For #615 Phase 1, inspect the safe fingerprints from the mode-0600 private env:

```bash
uv run spine-google-live-qualification \
  --env-file .env.qualification \
  --inspect-fingerprints
```

After the manifest and effective fingerprint are separately approved, verify
the entire private composition without a Google request:

```bash
SPINE_GOOGLE_PREFLIGHT_OPT_IN=1 \
  uv run spine-google-live-qualification \
  --env-file .env.qualification \
  --composition spine.infrastructure.google_live_runtime:build_live_runner \
  --preflight-only
```

This preflight validates the direct manifest digest, exact file roles,
spreadsheet/sheet allowlist, PostgreSQL-canonical current connection and trusted
workspace/environment context, approved limits, effective fingerprint and
reference-bound OAuth values after admission. Missing, stale, revoked or
mismatched current authority refuses before OAuth or provider transport. It does
not write a report or invoke OAuth/Drive/Sheets endpoints.

`GOOGLE_NUMERIC_SHEET_TITLES_JSON` is an ordered JSON array whose entries bind
one-to-one to `GOOGLE_NUMERIC_SHEET_IDS`. Titles are part of the approved
manifest and effective fingerprint. The runtime always A1-quotes each title
(including doubled apostrophes) and composes only bounded `A2:E5000` ranges;
missing, duplicate or count-mismatched titles fail closed before provider
transport. During an authorized live run, the Sheets adapter first performs a
metadata-only `spreadsheets.get` restricted to `spreadsheetId` and
`sheets.properties(sheetId,title)` for the approved range. The returned title
and numeric sheet ID must match the approved pair before any Drive-version or
grid-data request; stale or swapped titles therefore stop with no sheet page
completed and no provider identity in the report.

Phase 1 requests only `drive.readonly` and `spreadsheets.readonly`. The consent
command reads the exact mode-0600 Web client JSON named by
`GOOGLE_OAUTH_CLIENT_FILE`; the JSON must contain the configured client ID and
secret and the exact registered loopback redirect URI. Installed/Desktop client
JSON, an absent redirect registration or mismatched client material is refused
before the browser opens.

Only a later, separately authorized one-run command may contact the provider:

```bash
SPINE_GOOGLE_LIVE_OPT_IN=1 \
  uv run spine-google-live-qualification \
  --env-file .env.qualification \
  --composition spine.infrastructure.google_live_runtime:build_live_runner \
  --report /approved/path/google-live-report.json
```

The `google-live-qualification-report:v4` report separates completed logical
operations from low-level transport attempts. It records bounded attempt counts
for OAuth refresh, Drive and Sheets, plus a closed failure stage of
`oauth_refresh`, `drive`, `sheets` or `harness`; a successful run has no failure
stage. An attempt is counted immediately before the bounded transport delegates
the request, so a transport exception still leaves independently checkable
evidence. Completed operation counters keep their previous meaning and advance
only after the logical operation succeeds.

Version 3 added the configured maximum attempts, a closed failure class
and a closed retry disposition. The failure class distinguishes disclosure-safe
categories such as timeout, transport connectivity and HTTP 5xx without
retaining the URL, response or raw exception. The retry disposition says whether
the failure exhausted its attempts, was non-retryable, stopped before another
retry, stopped at the run deadline or had no applicable provider retry.
Every failed v4 report contains the failure stage, code, class and retry
disposition together; every passed report omits all four. A second provider 401
after the single permitted refresh is `not_retryable`, even when the general
request-attempt ceiling is higher, because the authorization policy deliberately
stops before exhausting that ceiling.

Version 4 preserves the v3 fields and distinguishes a closed allowlist of safe
Drive 403 reasons: insufficient OAuth scope, a non-downloadable file, missing
file permission and a revision download restriction. Unknown, missing or
malformed provider reasons remain `provider_policy_denied`. The report never
copies the provider message, resource ID or response body.

Migration note: v1 reports contain completed-operation counters only. Version 2
adds transport attempts and `failure_stage`, but has no failure class, retry
disposition or configured maximum. Consumers must never infer missing v2, v3 or v4
evidence retrospectively; require the exact schema version before reading its
fields.

The report otherwise contains only provider/API/configuration versions,
connection generations, status and a closed safe failure code. It never contains
credentials, provider bodies, source bytes, resource IDs, endpoint URLs or raw
exceptions. Do not commit the caller's live config or report if any future report
schema includes identifiers that are not approved for repository disclosure.

Shared Drive remains `NOT_EXERCISED` unless a separately approved accessible
synthetic Shared Drive is supplied, exactly allowlisted and
`shared_drive_approved` is true. That configuration boolean is not owner
approval: a live Shared Drive run also requires the external approval and
separate execution authorization described in
[`r2-google-shared-drive-offline.md`](r2-google-shared-drive-offline.md).

## Not verified by offline evidence

- Google endpoint availability and real Web OAuth consent/rotation;
- real quota allocation, rate-limit timing and provider backoff headers;
- real Drive change-feed delivery/token expiry and provider removal semantics;
- real Sheets concurrent edits and response-size behavior;
- Shared Drive access and scope semantics;
- any corporate, customer or production data path.

These are `NOT_VERIFIED`, not passes. Live execution and production rollout are
outside #652.
