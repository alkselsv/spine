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
- OAuth mode: OAuth 2.0 Desktop refresh grant resolved through separate opaque
  credential and grant references.

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

## Manual live qualification

Live access is forbidden unless both gates are present:

1. the JSON config contains `"opt_in": true` and a complete exact allowlist;
2. the process has `SPINE_GOOGLE_LIVE_OPT_IN=1`.

The config also names (but does not contain) the approved Desktop credential
inputs. The connection contains only opaque references. The CLI cannot build a
provider client from this JSON snapshot: `--composition module:function` must
name a deployment-owned factory returning `GoogleLiveQualificationRunner`.
That factory resolves secrets through the approved store, supplies the trusted
persistence context and current connection repository, and wires the provider
through `compose_google_live_qualification_runner`. The admitted current
connection must exactly equal the approved live manifest before any provider
request. Missing/mismatched references, scope, generations, current version,
opt-in or composition refuse the run before a provider request.

Run only against the separately approved isolated synthetic Google resources:

```bash
SPINE_GOOGLE_LIVE_OPT_IN=1 \
  uv run spine-google-live-qualification \
  --config /approved/path/google-live-config.json \
  --composition approved_google_runtime:build_live_runner \
  --report /approved/path/google-live-report.json
```

The report contains only the provider/API/configuration versions, connection
generations, bounded operation counts, status and a closed safe failure code.
It never contains credentials, provider bodies, source bytes or raw exceptions.
Do not commit the caller's live config or report if its resource identifiers are
not approved for repository disclosure.

Shared Drive remains `NOT_EXERCISED` unless a separately approved accessible
synthetic Shared Drive is supplied, exactly allowlisted and
`shared_drive_approved` is true.

## Not verified by offline evidence

- Google endpoint availability and real Desktop OAuth consent/rotation;
- real quota allocation, rate-limit timing and provider backoff headers;
- real Drive change-feed delivery/token expiry and provider removal semantics;
- real Sheets concurrent edits and response-size behavior;
- Shared Drive access and scope semantics;
- any corporate, customer or production data path.

These are `NOT_VERIFIED`, not passes. Live execution and production rollout are
outside #652.
