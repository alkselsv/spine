# Issue 585: Google Sheets and Drive provider contract research

Status: provider research for the R2 Google source contract. This note records
Google-owned facts, then makes explicitly labelled inferences for a future Spine
specification. It does not select a production authentication mode or make a
pricing-authority decision.

## Evidence notation

- **Verified provider fact** means the linked Google documentation states the
  behavior.
- **Proposed contract** is a design inference from those facts. It is not a
  Google guarantee and is not evidence that Spine already implements it.
- **Unresolved evidence** means the reviewed Google documentation does not
  establish the stronger guarantee that would be needed to make the claim.

## Google Sheets identity

### Spreadsheet and sheet

**Verified provider fact.** `Spreadsheet.spreadsheetId` is the read-only ID of a
spreadsheet. A sheet (tab) has both a numeric `sheetId` and a `title`; Google says
the sheet ID is stable when the sheet name changes, and `sheetId` cannot be
changed once set. A cell has no unique ID: row and column coordinates identify
it. A1 notation embeds a sheet name plus cell coordinates.

Sources:

- [Sheets API concepts](https://developers.google.com/workspace/sheets/api/guides/concepts)
- [Spreadsheet resource](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets)
- [Sheet and SheetProperties](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/sheets)

**Proposed contract.** Identify a register tab by
`(connection_id, spreadsheet_id, sheet_id)`. Preserve the current title only as
observed metadata/a human locator. Never use the title as identity. A copied
sheet is a newly created sheet and therefore must not inherit the source sheet's
Spine identity merely because its content or title matches; `copyTo` returns the
new sheet's properties.

Source: [spreadsheets.sheets.copyTo](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.sheets/copyTo)

### Row identity alternatives

#### A. Coordinate or A1 range

**Verified provider fact.** Cells have no unique IDs. Coordinates identify them.
Inserting a row above a coordinate shifts the prior row. The Sheets request model
also supports moving, sorting, randomizing, inserting, and deleting rows/ranges.

Sources:

- [Sheets API concepts](https://developers.google.com/workspace/sheets/api/guides/concepts)
- [Spreadsheet requests](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request)

**Proposed contract.** A row number, grid index, or A1/R1C1 range is a locator at
one observation, never the business identity of an estimate-register row.

#### B. Developer Metadata

**Verified provider fact.** Developer Metadata can attach a spreadsheet-scoped
unique `metadataId`, key, and optional value to exactly one row or column. Google
states that attached metadata remains associated as locations move during edits:
for example, inserting above row 5 moves its row metadata to row 6. Deleting the
associated object deletes its metadata. Metadata can be found with a
`developerMetadataLookup`, and row values can be read with
`spreadsheets.values.batchGetByDataFilter`. `PROJECT` visibility is visible only
to the Cloud project that created it; `DOCUMENT` is visible to any project with
document access. Google explicitly warns not to use project-visible metadata as
a security mechanism or secret store. Metadata storage is limited.

Sources:

- [DeveloperMetadata resource](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata)
- [Read, write, and search metadata](https://developers.google.com/workspace/sheets/api/guides/metadata)

**Unresolved evidence.** The reviewed reference gives a general “move around”
guarantee and an insertion example, but does not separately specify every UI/API
sort, copy, paste, or partial-range operation. It also does not describe
Developer Metadata as a globally immutable row identifier: `metadataId` is only
spreadsheet-scoped, metadata can be updated/deleted, and deleting the associated
row deletes it.

#### C. Explicit immutable business-ID column

**Verified provider fact.** This is application data, not a Google-provided
identity primitive. Sheets accepts strings, booleans, and doubles as values; no
Sheets v4 field supplies an immutable per-row business identity or per-row
revision.

Sources:

- [ValueRange resource](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values)
- [Sheets REST resources](https://developers.google.com/workspace/sheets/api/reference/rest)

**Proposed contract.** Require an explicit, non-empty, immutable business-ID
column controlled by the register's Data Owner. Scope it by connection,
spreadsheet, and sheet; reject duplicate IDs within that scope. It remains an
observed external claim until canonical acceptance validates it.

#### D. Combined recommendation

**Proposed contract.** Use the explicit business-ID column as primary row
identity. Optionally attach project-visible Developer Metadata containing the
same non-secret ID as a resilience/indexing aid where the approved connection
has write access. Record Developer Metadata ID and current coordinate only as
provider locators. On every observation, require the business-ID value and
metadata value (when configured) to agree; mismatch, missing ID, or duplicates
are quarantine/diagnostic states, not automatic reassociation. This combination
survives ordinary row movement without treating mutable coordinates or mutable
metadata as the business key.

## Google Sheets revisions and extraction

**Verified provider facts.**

- The Sheets v4 API exposes spreadsheet, sheet, grid, value, and Developer
  Metadata resources, but no immutable per-row revision field.
- The Drive `File.version` field is a monotonically increasing file version that
  reflects every server-side file change, including changes not visible to the
  user. `headRevisionId` is currently available only for binary content, not a
  native Google Sheet.
- Google Workspace editor content changes produce revision-history entries, but
  editor revisions may be merged. `revisions.list` may be incomplete for large or
  frequently edited Docs/Sheets/Slides; older revisions can be omitted.
- `spreadsheets.values.get` and `batchGet` return values. Their defaults are
  `majorDimension=ROWS`, `valueRenderOption=FORMATTED_VALUE`, and
  `dateTimeRenderOption=SERIAL_NUMBER`. `FORMATTED_VALUE` is calculated and
  locale-formatted; `UNFORMATTED_VALUE` is calculated but not formatted;
  `FORMULA` returns the formula rather than its result.
- `CellData` distinguishes `userEnteredValue`, read-only `effectiveValue`
  (calculated for formulas), `formattedValue` (what the user sees), and number
  format. Dates/times are represented as doubles in serial-number form. The
  spreadsheet resource also carries locale and time zone.
- Value reads have no explicit response-size pagination. Google recommends
  specific ranges/fields for large sheets and a maximum payload around 2 MB;
  requests can time out after 180 seconds. Quota exhaustion produces `429` and
  Google recommends truncated exponential backoff.
- Sheets batch writes are atomic, but Google warns that collaborator changes can
  alter the spreadsheet after a batch completes. This is not a read-snapshot or
  row-version guarantee.

Sources:

- [Drive File resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Changes and revisions overview](https://developers.google.com/workspace/drive/api/guides/change-overview)
- [Manage revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions)
- [Read and write cell values](https://developers.google.com/workspace/sheets/api/guides/values)
- [ValueRenderOption](https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueRenderOption)
- [CellData](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/cells)
- [Sheets usage limits](https://developers.google.com/workspace/sheets/api/limits)
- [Sheets batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)

**Proposed contract.** Each successful read creates a Spine-owned immutable
observation identity and a digest over a deterministic serialization that
includes at least the scoped business ID, sheet ID, present coordinate, selected
cell `userEnteredValue`, `effectiveValue`, `formattedValue`, relevant number
format, spreadsheet locale/time zone, and the exact extraction profile version.
Record Drive `version`, revision ID if actually returned, and change token as
provider signals, not as the immutable `SourceRevision` identity.

For money/date fields, retain all relevant representations. Parse exact numeric
values without binary-float financial arithmetic; retain displayed currency and
format as evidence but do not infer pricing authority from formatting or a
historical row. Formula text, calculated result, and formatted display are three
different observations.

**Proposed contract.** Prefer a bounded `spreadsheets.getByDataFilter` or explicit
ranges with field masks when cell detail and Developer Metadata must be captured
together. Chunk large configured ranges explicitly; “pagination” is a
connector-owned range plan because Sheets value methods do not provide page
tokens. A Drive `version` read before and after Sheets extraction can be used as a
best-effort concurrent-change guard; if it changes, discard/retry the candidate
observation. This does not create a Google transaction guarantee.

## Google Drive identity, content, and revisions

**Verified provider facts.**

- Every Drive file has a unique opaque `fileId`, stable for the life of the file
  even if its name changes. `name` is not necessarily unique. A move updates
  parent folder IDs through `files.update`; it does not create a documented new
  file identity.
- Blob files are raw binary/text content. Google Workspace documents use
  `application/vnd.google-apps.*` MIME types; Sheets is
  `application/vnd.google-apps.spreadsheet`. Shortcuts are separate metadata-only
  files pointing to another file.
- Blob content is downloaded with `files.get?alt=media`; Workspace-native content
  is exported with `files.export` to a supported MIME type. Exported content is
  limited to 10 MB. `capabilities.canDownload` and download restrictions must be
  checked. Blob checksums may be present; `sha256Checksum` is not populated for
  Docs Editors or shortcut files.
- A Drive revision represents file content, not metadata. `headRevisionId` is
  only available for binary files. Older unpinned blob revisions can be purged;
  Workspace editor revision listings can be merged/incomplete. Therefore even a
  provider revision reference is not, by itself, a durable archive.
- `File.version` advances for every server-side change, including metadata-only
  changes. `modifiedTime` is the last modification time, not an immutable content
  revision identity.

Sources:

- [Files and folders overview](https://developers.google.com/workspace/drive/api/guides/about-files)
- [Drive File resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Create and populate folders](https://developers.google.com/workspace/drive/api/guides/folder)
- [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Manage revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions)

**Proposed contract.** Identify a Drive source by
`(connection_id, file_id)`. Record `driveId`, current parents, name, MIME type,
`version`, `modifiedTime`, available head/revision/checksum signals, export
profile, and content digest as observed metadata. Rename/move alone updates an
observation/locator but does not create a new `SourceObject`. Create a new
immutable source revision only when the normalized source observation covered by
the Spine contract changes; preserve an original byte/export artifact because
Google revision history is not a permanent immutable store.

## Change feed, deletion, access, and watches

**Verified provider facts.**

- `changes.getStartPageToken` returns a start token. Google documents start,
  next-page, and new-start tokens as non-expiring.
- `changes.list` entries are chronological, oldest first. A response with
  `nextPageToken` is incomplete and must be paged; `newStartPageToken` appears
  only at the end and is the token for future changes.
- A `Change.removed=true` means the item was removed from this change collection,
  for example because it was deleted **or because access was lost**. It does not
  distinguish those causes.
- `File.trashed` means the file is in trash; other users may retain access until
  permanent deletion. A `404 notFound` likewise means either no read access or
  nonexistence. These signals cannot independently prove permanent deletion.
- Change watches are wake-up hints. Notification bodies are empty and callers
  must query `changes.list`/`files.get`. Message numbers increase but are not
  sequential. Channels expire (maximum one week for changes, one day for an
  individual file), cannot be renewed in place, and replacement channels can
  overlap. Google retries only certain 5xx webhook responses.
- Shared-drive requests need `supportsAllDrives=true`; scoped change tokens can be
  obtained for a `driveId`. `files.list` over `allDrives` can return
  `incompleteSearch=true`; Google recommends narrowing to `user` or one `drive`
  corpus.
- Drive distinguishes invalid credentials (`401`), forbidden/quota cases
  (`403`), ambiguous inaccessible-or-nonexistent (`404`), rate limit (`429`), and
  transient server errors (`5xx`). Google recommends exponential backoff for
  rate and server errors.

Sources:

- [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)
- [changes.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list)
- [Change resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes)
- [Drive File resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Notifications for resource changes](https://developers.google.com/workspace/drive/api/guides/push)
- [Implement shared-drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [Resolve Drive API errors](https://developers.google.com/workspace/drive/api/guides/handle-errors)

**Proposed contract.** Classify signals without collapsing them:

| Provider evidence | Connector classification | Tombstone implication |
| --- | --- | --- |
| Read succeeds; digest changes | content changed | New immutable observation/revision |
| Same file ID; name/parents change | moved/renamed | No new object; revision only if the defined observed metadata is revision-worthy |
| `trashed=true` | provider-confirmed trashed state | Tombstone only if the R2 source policy treats trash as removal |
| `removed=true` in change feed | removed-or-access-lost | Reconcile; not enough alone for deletion tombstone |
| `404` | inaccessible-or-nonexistent | Reconcile/authorization failure; not enough alone for tombstone |
| `401` / invalid refresh token | connection authorization invalid | Suspend sync; no source tombstone |
| `403` without deletion evidence | forbidden/policy/quota | Access or operational failure; no tombstone |
| `429` or `5xx` | temporary failure | Retry with bounded exponential backoff; no tombstone |
| Missing item in complete, authorized, scoped reconciliation | confirmed absent from configured corpus | Eligible for tombstone under Spine policy |
| Previously tombstoned identity is read again | reappearance | New observation linked to same source identity; do not erase history |
| Any incomplete page/corpus/batch | incomplete synchronization | No absence conclusions and no cursor advancement |

## Incremental synchronization implications

**Proposed contract.** A safe minimum algorithm is:

1. Persist the connection principal, scope/corpus configuration, and a change
   cursor independently for every Google authority boundary (user corpus or
   shared-drive ID).
2. For initial import, obtain a start token, enumerate the explicitly allowed
   files/register, and capture each original plus deterministic digest. Then drain
   changes from the start token so changes during enumeration are not lost.
3. For an incremental run, call `changes.list` from the committed token and
   consume every page. Treat watches only as triggers.
4. Re-read the current resource for each relevant file/change, classify access
   and absence separately, and write the immutable original/observation before
   canonical acceptance.
5. Use a Spine-owned idempotency key such as
   `(workspace, environment, connection, provider object, observation digest,
   extraction-profile version)`; duplicate and out-of-order delivery converges on
   the same logical effect.
6. Do not commit `newStartPageToken` until every page and every observation in the
   interval has durably reached the ingestion handoff state required by the Spine
   contract. A crash after storing an original but before acceptance/cursor commit
   must replay safely. A crash before cursor commit repeats work, not loses work.
7. If pagination, a batch, or corpus search is incomplete, retain the prior cursor
   and retry/reconcile. Periodically run a complete scoped reconciliation because
   notifications are hints and removal/access signals are ambiguous.

**Unresolved evidence.** Google currently documents Drive page tokens as not
expiring; the reviewed API does not specify a normal expiry/invalidation recovery
protocol. A malformed/rejected token, changed corpus/principal, lost local token,
or any other untrusted checkpoint should therefore trigger an explicit full
scoped reconciliation, not an assumption that Google supplied a complete delta.

## Authentication and authorization alternatives

### Delegated user OAuth

**Verified provider fact.** OAuth client credentials access user-owned data after
user consent. Offline server processing requires `access_type=offline` and secure
refresh-token storage. Refresh tokens can become invalid, including through user
revocation. Requested scopes should be the narrowest possible and requested in
context.

Sources:

- [Workspace authentication overview](https://developers.google.com/workspace/guides/auth-overview)
- [OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server)
- [OAuth 2.0 policies](https://developers.google.com/identity/protocols/oauth2/policies)

**Proposed contract.** The Google principal is the consenting user; grant
lifecycle follows that user's consent and token revocation. Bind each connection
and encrypted credential reference to exactly one Spine workspace/environment,
record on-behalf-of identity and consent/scopes, and suspend on invalid grant.

### Service account explicitly shared to resources

**Verified provider fact.** A service account is a non-human principal. It can
access resources for which that service account has permission. Google explicitly
lists access to app-owned data or specific shared documents as service-account
use cases. This does not require domain-wide impersonation.

Sources:

- [Create access credentials](https://developers.google.com/workspace/guides/create-credentials)
- [Server-to-server OAuth](https://developers.google.com/identity/protocols/oauth2/service-account)

**Proposed contract.** The service account is the Google principal and only
explicitly shared, Data-Owner-approved file/folder/shared-drive IDs belong to the
connection. Its non-human credential lifecycle and resource shares need separate
revocation/rotation ownership.

### Domain-wide delegation

**Verified provider fact.** A Workspace super administrator can grant a service
account domain-wide authority for selected scopes. Calls then impersonate a
specified domain user and are limited by both that user's permissions and the
authorized scopes. Google emphasizes least privilege and notes service accounts
are not Workspace members and are not necessarily subject to end-user domain
policies in the same way.

Source: [Server-to-server OAuth and domain-wide delegation](https://developers.google.com/identity/protocols/oauth2/service-account)

**Proposed contract.** Defer this mode unless Security Owner explicitly approves
the impersonation scope, impersonated principals, audit/revocation process, and
operational controls. It must never be the default inferred from “service
account.”

### Scope facts

**Verified provider fact.** `drive.file` is Google's recommended non-sensitive
per-file scope for files the user shares with/opens through the app. Broad
`drive` and `drive.readonly` are restricted scopes; storing/transmitting data
obtained under restricted scopes can require a security assessment.
`spreadsheets.readonly` sees all of the user's spreadsheets and is sensitive.
Sheets scopes apply to the spreadsheet file, not an individual sheet. Metadata-
only Drive scope cannot download content.

Sources:

- [Choose Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Choose Sheets API scopes](https://developers.google.com/workspace/sheets/api/scopes)
- [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads)

**Proposed contract.** `drive.file` plus explicit selection/sharing is the
least-privilege candidate when its product flow covers every approved source.
`spreadsheets.readonly` or `drive.readonly` are candidates only when required by
the approved discovery/sync model. Provider permission answers only “may this
connector principal read this Google object?” It does not answer whether Spine
may process it, a manager may see it, or Finance allows it as pricing basis.

## Provider-grounded conclusions for the Spine specification

1. Row coordinate/A1 is only an observation locator. Require a Data-Owner-managed
   immutable business-ID column; optionally pair it with Developer Metadata as a
   non-secret provider anchor, with explicit mismatch quarantine.
2. Use Drive file ID and Sheets sheet ID for stable provider object identity;
   names and parents are observed metadata.
3. Google provides no immutable per-row version. Drive `version`, revisions,
   timestamps, and change cursors are provider signals. Spine must own immutable
   observation IDs, original artifacts, deterministic digests, and replay-safe
   acceptance.
4. Preserve entered/formula, effective, displayed, format, locale, and time-zone
   evidence separately. Do not derive pricing authority from the register.
5. A Drive removal event or 404 is ambiguous between deletion and loss of access.
   Temporary/authorization failures cannot create deletion tombstones.
6. Consume all change pages and advance the durable cursor only after all prior
   observations have durably reached the required ingestion handoff. Google
   delivery is not an exactly-once contract.
7. Production auth mode, allowed resources, connection/grant owner, revocation
   policy, allowed pricing sources, and operational limits remain human Security
   Owner/Data Owner decisions.

## Remaining provider evidence gaps

- No reviewed official page separately guarantees Developer Metadata behavior
  for every sort/copy/paste variant. The generic movement guarantee is strong
  enough to use metadata as an anchor, but contract tests must qualify the exact
  operations R2 supports.
- No Sheets v4 immutable per-row revision or optimistic-read token was found.
- Drive's documented non-expiring page tokens do not remove the need to recover
  from locally lost/rejected checkpoints or changed authorization/corpus.
- Google documents ambiguous `removed` and `404` outcomes; a connector cannot
  prove deletion from either signal alone.

## Existing Spine contract audit

The audit below is code-based. Documentation was used to interpret intent, not
as evidence that an implementation exists.

| Classification | Existing seam and evidence | Google applicability |
| --- | --- | --- |
| **ALREADY IMPLEMENTED** | `SourceObject`, `SourceRevision`, `SourceRevisionProvenance`, and receipt association in `src/spine/domain/sources/models.py`; source observation port/command in `src/spine/application/persistence/repositories.py`; in-memory and PostgreSQL implementations. Contract coverage: `tests/contracts/persistence/test_source_observation_contract.py`, `tests/application/persistence/test_source_observation.py`. | Reuse for Drive files and register rows. Do not create a Google-specific revision ledger. |
| **ALREADY IMPLEMENTED** | Verified originals and digest/length receipts in `src/spine/domain/sources/original_reference.py`, `src/spine/application/storage/`, and `src/spine/infrastructure/object_storage/`. Receipt association is atomic with observation. Tests: `tests/contracts/object_storage/test_conformance.py`, `tests/application/storage/test_upload_lifecycle.py`, source-observation receipt tests. | Store exact downloaded blob, exported native document, or deterministic row-observation JSON before acceptance. |
| **ALREADY IMPLEMENTED** | Append-only `CanonicalAcceptance` and current-head CAS/order policy in `src/spine/domain/sources/canonical.py`, `src/spine/application/canonical_acceptance.py`, and `src/spine/application/persistence/canonical.py`. Tests: `tests/application/persistence/test_canonical_acceptance.py`, `tests/contracts/persistence/test_canonical_acceptance_contract.py`. | Provider `File.version` may be registered as a trusted comparator only after its exact token rules are specified; otherwise use expected-current CAS. |
| **ALREADY IMPLEMENTED** | Tombstone/replacement/reappearance composition in `src/spine/application/source_lifecycle.py`. Tests: `tests/application/test_source_lifecycle.py`. | Use only after verified logical deletion under the approved source policy. Ambiguous removal/access failure is not eligible. |
| **ALREADY IMPLEMENTED** | Ingestion run/item/binding/receipt ledger and idempotent orchestration in `src/spine/domain/ingestion/`, `src/spine/application/persistence/ingestion.py`, and `src/spine/application/document_ingestion.py`. Tests: `tests/application/test_document_ingestion.py`, `tests/contracts/persistence/test_ingestion_repository_contract.py`, `tests/application/test_batch_ingestion.py`. | Google adapter hands observations into this pipeline; no second ingestion coordinator or exactly-once claim. |
| **ALREADY IMPLEMENTED** | Generic `Connector.sync(cursor, limit)` / `get(external_id)` with `SourceRecord` and `SyncBatch` in `src/spine/connectors/base.py`. | Useful vocabulary only; it is too weak for scoped Drive cursors, incomplete reconciliation, authorization state, row observations, and durable cursor-commit rules. |
| **ALREADY IMPLEMENTED** | Versioned allow-only `AccessPolicyVersion`, `AccessGrant`, and deny-only `SourceSecurityDomainBinding` in `src/spine/domain/sources/access_policy.py`; authorization service in `src/spine/application/content_authorization.py`. Tests: `tests/application/test_access_policy_activation.py`, `tests/application/test_content_authorization.py`, `tests/infrastructure/persistence/test_security_domain_bindings.py`. | Google grants are connector-read authority only. Spine policy remains mandatory for processing and disclosure. |
| **ALREADY IMPLEMENTED** | Trusted request identity/context and fail-closed authorization in `src/spine/application/content_authorization.py`, `src/spine/application/authorized_context_broker.py`, and API authorization composition. Tests include cross-workspace/non-leakage cases in `tests/application/test_authorized_context_broker.py`. | Bind each connection and invocation to workspace/environment and current authorization generation; do not trust provider payload identity as Spine authority. |
| **ALREADY IMPLEMENTED** | Forced workspace/environment PostgreSQL RLS and immutable source tables in migrations `20261009_02_tenant_rls.py` and `20261010_09_source_observations.py`. Tests: `tests/integration/postgresql/test_initial_tenancy_migration.py`. | New Google persistence must use the same tenant context/RLS posture. |
| **ALREADY IMPLEMENTED** | Context Broker `ContextProjection`/structured evidence contracts in `src/spine/memory/contracts.py`, plus canonical publication and retrieval gates. Tests: `tests/contracts/memory/`, `tests/application/test_authorized_context_broker.py`, and projection suites. | Google observations become ordinary canonical inputs; Cognee remains a rebuildable projection, never connector truth or ACL authority. |
| **EXTEND** | R1 `RevisionMetadata` and canonicalization profile in `src/spine/domain/sources/models.py` and `profile.py` are document-specific. | Add a versioned R2 row/file observation profile; preserve existing v1 hashes and meanings. |
| **EXTEND** | `XlsxCellLocator` in `src/spine/domain/artifacts/models.py` and its parser/tests (`tests/domain/artifacts/test_xlsx_parser.py`) address immutable uploaded XLSX bytes by sheet name/row/A1. | Add a Google-Sheets-specific locator contract using stable sheet ID and revision-scoped coordinate/business ID. Do not reinterpret `XlsxCellLocator`. |
| **EXTEND** | `SourceObject` connector identity already scopes `(workspace, environment, connection, namespace, generation, external_object_id)`, but public identifier validation is bounded/lowercase. | Define an opaque canonical encoding/digest for Google composite keys or explicitly widen the next schema; never truncate or normalize provider IDs lossy. |
| **NEW R2 CONTRACT** | No implemented `Connection`/`SyncCursor` models, Google adapters, Google grant lifecycle, row identity profile, external business-association model, or Google fake/contract suite was found. | Specify these at existing connector → source observation → ingestion seams. |
| **DEFERRED** | No Google production credentials/provisioning or pricing-authority implementation exists. | Authentication mode, approved resources, operating limits, and pricing authority remain owner decisions; adapter implementation follows `$to-spec`/`$to-tickets`. |

## Minimal versioned Spine source mapping

The following shapes are specification pseudocode, not production models:

```text
GoogleConnectionRef.v1 = {
  connection_id, workspace_id, environment_id,
  auth_mode: delegated_user | shared_service_account | domain_delegation,
  google_principal_ref, credential_ref, grant_ref,
  authorization_generation, approved_resource_scope_ref, status
}

GoogleDriveFileKey.v1 = { file_id, drive_id? }
GoogleSheetKey.v1 = { spreadsheet_id (= Drive file_id), sheet_id }
GoogleRegisterRowKey.v1 = {
  spreadsheet_id, sheet_id, business_id
}

GoogleChangeCursor.v1 = {
  connection_id, corpus: user | shared_drive, drive_id?,
  page_token, configuration_generation, committed_at
}

GoogleRegisterRowObservation.v1 = {
  key, observed_a1_range, developer_metadata_id?,
  provider_file_version_before, provider_file_version_after,
  extraction_profile_version, locale, time_zone,
  entered_values, effective_values, formatted_values, number_formats,
  normalized_original_sha256, coverage: complete | partial | failed,
  observed_at, authorization_provenance_ref
}

GoogleSheetCellLocator.v1 = {
  source_revision_id, spreadsheet_id, sheet_id, business_id,
  observed_a1_range, column_key, cell_coordinate,
  representation: entered | effective | formatted,
  locator_contract_version
}
```

Mapping rules:

- A Drive file is a `SourceObject(kind=document)` keyed by connection plus
  opaque `file_id`. A register row is a separate `SourceObject(kind=record)`
  keyed by connection plus a lossless/digest encoding of
  `(spreadsheet_id, sheet_id, business_id)`. The spreadsheet/file source can be
  linked as the row's provider container; the sheet title and row coordinate are
  not identity.
- `external_generation` denotes the versioned provider-identity scheme/corpus
  generation, not a content revision. A changed Drive `version` must never create
  a new `SourceObject`.
- The immutable original for a row is deterministic JSON containing all selected
  representations and extraction context. Its SHA-256 plus revision-bearing
  metadata feeds the existing `SourceRevision` digest. Exact repeats reuse the
  revision and may append new provenance.
- A Drive `version`, revision ID, change cursor, change time, and current A1
  coordinate belong to provenance/provider signals. None replaces the Spine
  `SourceRevision` or original digest.
- Rename/move or sheet-title change retains source identity. By default it
  appends provenance/operational observation without a new content revision;
  only a field explicitly declared revision-bearing in the profile may change
  the revision digest.
- Canonical acceptance remains separate and requires complete observation,
  admission, active eligible `AccessPolicy`, and CAS/trusted ordering evidence.
  Partial or failed extraction can be retained as raw evidence but is not
  accepted as a complete current register observation.

### Business association chain

```text
Google register row SourceRevision
  -> observed external references (Drive file IDs/URLs and declared row fields)
  -> Spine identity-resolution candidates
  -> explicitly accepted client/project/request/meeting EntityLinks
  -> HistoricalEstimate artifact referencing exact row/file revisions
  -> ApprovedFinalEstimate reference created by a Spine-owned approval decision
```

The row's literal IDs, links, and values are external-source facts. Mapping them
to a canonical client, project, request, or meeting is a Spine-owned business
association with provenance and acceptance. Textual client/project name matches
are only candidates and never identity proof. “Historical estimate” describes
an evidence role; “approved final estimate” requires an explicit Spine-owned
approval/version reference. Neither makes the source a valid pricing basis;
that authority belongs to the decision tracked separately from this research.

## Cursor, crash, and reconciliation rules

The initial and incremental algorithm above is refined as follows:

1. Capture a Drive start token **before** initial complete scoped enumeration;
   persist a pending run, ingest every allowed file/row, then drain changes from
   that token. Only the drained end token can become committed current.
2. For Sheets changes, Drive is only the wake-up/change signal. Reconcile the
   configured sheet by business ID because Sheets has no row change feed.
3. Fetch every `changes.list` page. Persist raw provider envelopes and per-item
   outcomes. A page checkpoint/cursor can advance only in the same durable unit
   as, or strictly after, all its required immutable originals, observations,
   dispositions, canonical acceptance/outbox effects, or explicit terminal
   quarantine records.
4. If a source was read but canonical acceptance has not completed, hold the
   cursor. If the original was stored and the process crashes, retry reuses the
   verified original/receipt and existing observation identity. If it crashes
   just before cursor commit, replay the interval idempotently.
5. Duplicate delivery returns the prior receipt; the same event identity with a
   different digest is an integrity conflict. Out-of-order content cannot replace
   a newer canonical head: use a registered numeric `File.version` comparator
   where applicable, otherwise expected-current CAS plus reconciliation.
6. Advance from `nextPageToken` only after that page is durably complete; commit
   `newStartPageToken` only at the feed end. An item fetch, batch, or pagination
   failure holds the prior committed cursor unless a durable terminal
   quarantine disposition is explicitly allowed by policy.
7. Treat `429`, quota `403`, and retryable `5xx` with bounded truncated
   exponential backoff and jitter. Persist retry state; do not spin inside an
   agent. Respect connector-owned range chunking for Sheets.
8. A missing/rejected/local-lost cursor, changed connection principal/corpus, or
   `incompleteSearch=true` triggers a new start-token-before-scan full scoped
   reconciliation. No absence/tombstone conclusion is allowed from an
   incomplete scan.
9. Periodic full reconciliation remains mandatory even with watches. Watch
   channels are renewable wake-up hints, not correctness or delivery receipts.

## Authorization boundaries and owner choices

| Boundary | Authority and minimum rule |
| --- | --- |
| **A. Connector may read Google** | Google grant to the user/service account plus OAuth scopes and allowed resource configuration. Candidate least privilege is `drive.file` with explicit selection/sharing when it supports the approved sync flow; broader read scopes require justification and possibly verification/security assessment. |
| **B. Spine may process** | Current Spine service `process_content` grant, approved source configuration, Security Domain narrowing, workspace/environment binding, purpose/operation, and admission. Google consent does not grant this. |
| **C. Manager may see content/evidence** | Current Spine human/team `read_content`, current source revision and disclosure checks. Connector access and cached originals do not grant this. |
| **D. Source may support pricing** | Finance/Data Owner-approved `PricingBasis` policy and exact accepted source revision. Neither Google visibility nor “historical estimate” grants this. |

Human decisions required before production provisioning:

1. Security Owner selects delegated user OAuth, explicitly shared service
   account, or separately governed domain-wide delegation; the last is not a
   default and requires approved impersonated principals/scopes/audit controls.
2. Data Owner enumerates allowed spreadsheet/file/folder/shared-drive resources,
   owns the immutable business-ID column, and decides whether project-visible
   Developer Metadata may be written.
3. Security/Data Owners assign connection/grant owner, refresh-token or service
   credential rotation, revocation response, reauthorization, retention after
   access loss, and whether trash counts as logical removal.
4. Finance/Data Owner decides which exact sources/revisions may be pricing basis;
   this research intentionally does not decide it.
5. Operations/Security approve quotas, polling/reconciliation cadence, batch and
   payload limits, retry ceilings, alerting, Shared Drive corpus strategy, watch
   use, and operational qualification evidence.

## Failure and deterministic contract-test matrix

All fixtures are synthetic and use fake Google ports; real-provider
qualification is separate and opt-in.

| Input / event | Expected canonical effect | Retry behavior | Evidence | Failure status |
| --- | --- | --- | --- | --- |
| Same row/file read, same digest | Reuse `SourceRevision`; optional new provenance; no new current-head effect | Stable replay | original digest, provider signals, receipt | `succeeded_no_change` |
| Row value/formula/format selected by profile changes | New row revision; accept by CAS/order after complete read | Idempotent by object/profile/digest | before/after provider version, cell representations, locator | `succeeded_changed` |
| Rows sorted | Same row objects by business ID; coordinates/locators updated; no false delete/create | Reconcile once; stable replay | full ID set, metadata anchor when configured | `succeeded_relocated` |
| Sheet renamed | Same sheet/row identities by `sheetId`; title is observed metadata | Replay-safe | sheet ID plus old/new title event | `succeeded_metadata_only` |
| Row missing from one incomplete chunk | No tombstone, no cursor advance | Retry/reconcile | incomplete coverage record | `retryable_incomplete` |
| Row absent from complete authorized sheet reconciliation | Tombstone same row object if source policy defines absence as deletion | Idempotent lifecycle command | complete manifest, prior identity, deletion provenance | `succeeded_tombstoned` |
| Same business ID reappears after tombstone | New content revision on same object with tombstone predecessor; fresh admission/policy | Idempotent | exact ID continuity, original digest | `succeeded_reappeared` |
| Duplicate/missing/mismatched business ID or metadata | No reassociation or canonical acceptance; preserve raw input | Retry only after correction; reconciliation allowed | conflict details in protected diagnostics | `quarantined_identity` |
| Drive content digest changes | New immutable file revision | Fetch again if guard version changes | downloaded/exported original, digest, MIME/export profile | `succeeded_changed` |
| File renamed/moved | Same object; metadata/provenance only by default | Idempotent | file ID, version, parents/name | `succeeded_metadata_only` |
| `trashed=true` | Tombstone only under approved trash-as-removal policy; otherwise unavailable/retained | Reconcile and apply policy | File state and source policy version | `tombstoned` or `source_unavailable` |
| Change `removed=true` or `files.get` 404 | No tombstone; classify deleted-or-access-lost and reconcile | Reauthorize or authoritative reconcile | raw change/error, principal/corpus | `ambiguous_unavailable` |
| OAuth revoked / 401 | Suspend connection; no source revisions/tombstones | No blind retry; reauthorization required | grant ref/generation, safe error code | `authorization_invalid` |
| Forbidden 403 | No deletion conclusion; distinguish permission/domain policy from quota | Reauthorize or backoff by reason | safe provider reason, no protected payload | `denied` or `retryable_rate_limited` |
| Lost/rejected cursor | No delta acceptance based on guessed gap; full reconciliation | New start-token-before-scan flow | old cursor/config generation, reconciliation run | `reconciliation_required` |
| Pagination error / `incompleteSearch=true` | No absence conclusions or cursor advance | Resume/retry page or narrow corpus | page tokens, completeness flag | `retryable_incomplete` |
| 429 / retryable 5xx | No new canonical effect | Bounded exponential backoff+jitter | attempt count and safe status | `retryable_provider` |
| Incomplete multi-range/batch row observation | Raw/partial evidence only; no complete canonical acceptance | Retry whole guarded observation | coverage/exclusion and provider version guard | `retryable_incomplete` |
| Provider version changes during extraction | Discard candidate; no revision/current change | Re-read with bounded retry | before/after version mismatch | `concurrent_edit` |
| Same event/idempotency key, changed digest | No second effect; typed integrity conflict | Do not silently retry as same command | stored and presented command digests | `integrity_conflict` |
| Duplicate sync execution | One logical observation/acceptance/cursor effect | Replay receipts/outcomes | idempotency receipt and cursor generation | `succeeded_replay` |
| Cross-workspace/environment lookup | No read/write and no existence leak | Never retry without corrected trusted context | safe denial audit; RLS | `denied` |
| Invalid/stale Spine authorization | No provider read/model use/disclosure | Rebuild trusted context/reauthorize | authorization generation and decision ref | `authorization_changed` |
| Crash after original, before observation/acceptance | No cursor advance; orphan remains inaccessible; retry attaches/reuses it | Resume idempotently | write receipt and missing later receipt | `recovering` |
| Crash after acceptance, before cursor | Accepted effect remains; replay returns same result then commits cursor | Replay interval | acceptance/outbox/idempotency receipts | `succeeded_replay` |
| Safe diagnostics request | Only content-free status unless separately authorized | N/A | trace/correlation and sanitized code | status matching underlying class |
| Periodic reconciliation after interruption | Converge exact allowed corpus; no false tombstones from incomplete view | Repeat until complete | scoped manifest and boundary/cursor | `succeeded_reconciled` |

## Claim traceability

| Claim | Google documentation | Existing Spine decision | Recommendation |
| --- | --- | --- | --- |
| Row/A1 is not stable identity | Sheets concepts; request model | Source identity is independent of path/locator (ADR-0018) | Business-ID row key; A1 only revision-scoped locator |
| Sheet ID survives rename | Sheets concepts; `SheetProperties` | Stable external identity maps to `SourceObject` | Scope row identity by spreadsheet ID + sheet ID |
| Metadata follows attached location but is mutable/deleted | Developer Metadata resource/guide | Provider identity must have provable continuity | Optional anchor, never sole business identity; quarantine mismatch |
| No immutable per-row version | Sheets v4 resources; Drive File/version docs | Spine owns immutable `SourceRevision` and original | Deterministic row observation digest; provider version is signal |
| Entered/effective/formatted/formula differ | CellData and ValueRenderOption | Facts and derived values must remain distinguishable | Preserve representations and formats separately; exact money remains non-LLM |
| Drive ID survives rename/move; name is non-unique | Files/folders and folder move docs | `SourceObject` identity excludes path/name/checksum | Key by file ID; move/rename is metadata/provenance |
| Revision history is not a durable archive | Drive revision docs | Originals and PostgreSQL are canonical (ADR-0001/0018) | Persist exact bytes/export plus digest for each accepted observation |
| `removed`/404 conflate deletion and access loss | Change resource/about changes/error docs | Tombstone requires verified deletion (ADR-0018) | Reconcile/authorization status; never tombstone from those signals alone |
| Change pages/tokens and watches are not exactly-once work | changes.list/manage changes/push docs | At-least-once/idempotent ingestion (ADR-0025) | Hold cursor until durable effects; replay safely; reconcile periodically |
| Google permission is not Spine disclosure authority | OAuth/scope docs | AccessPolicy is sole content authority (ADR-0011) | Keep four authority boundaries separate |

## Exact handoff to `$to-spec`

### Ready for specification

- Versioned identities for connection, Drive file, spreadsheet/sheet, register
  row business key, corpus cursor, row observation, and Google Sheets locator.
- Explicit business-ID primary row identity, optional Developer Metadata anchor,
  duplicate/mismatch quarantine, and sort/rename behavior.
- Spine-owned immutable original/digest/revision with provider versions and
  change tokens only as provenance/order signals.
- Typed content/metadata/removal/access/transient/incomplete classifications;
  tombstone and reappearance gates that reuse the existing lifecycle.
- Start-token-before-scan initial sync, paged incremental sync, guarded Sheets
  reconciliation, durable cursor advancement, idempotency, crash recovery, and
  periodic full reconciliation.
- Separation of Google read grant, Spine processing grant, manager disclosure,
  and Finance pricing authority.
- The deterministic fake/contract matrix above at the existing connector →
  source-observation/ingestion seam (the highest reusable seam); real Google
  qualification remains an adapter suite.

### Human decision required

- Production authentication mode and scopes; approved resources/corpora;
  connection/grant owner and revocation/retention policy; permission to write
  Developer Metadata; trash-as-removal policy; pricing-source authority; quotas,
  polling, reconciliation, watch, retry, and alerting limits.

### Candidate future implementation units (do not create tickets yet)

- Google Sheets register adapter plus fake/contract suite.
- Google Drive file/change adapter plus fake/contract suite.
- Versioned Google connection/grant and external-identity mapper.
- Incremental sync coordinator/cursor persistence and reconciliation path.
- Register-row immutable observation/original and locator support.
- Canonical ingestion handoff reusing current observation, acceptance, lifecycle,
  policy, outbox, and projection contracts.
- Security/authorization composition and operational qualification.

The research is **ready for `$to-spec`** at the connector/source-observation
boundary. It is not evidence that a connector, production grant, operational
qualification, business identity mapping, or pricing authority already exists.
