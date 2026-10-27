# Google source identity and Sheets evidence contracts

Issue #600 introduces additive offline contracts for the R2 Google readers. It
does not add provider calls, credentials, cursor state, canonical ingestion or a
Cognee path. Existing R1 source profiles and `locator:xlsx-cell:v1` retain their
current meaning.

## Versions

| Contract | Version |
| --- | --- |
| Opaque source identity envelope | `google-source-identity:v1` |
| Source identity scheme (`external_generation`) | `google-source-key:v1` |
| Drive provider key | `google-drive-file-key:v1` |
| Sheet-scope provider key | `google-sheet-scope-key:v1` |
| Register-row provider key | `google-register-row-key:v1` |
| Row original | `google-sheet-row-original:v1` |
| Row extraction profile | `google-sheets-row-extraction:v1` |
| Row revision profile | `google-sheets-row-revision:v1` |
| Row location provenance | `google-sheet-row-location:v1` |
| Row observation | `google-sheet-row-observation:v1` |
| Cell locator | `locator:google-sheet-cell:v1` |

## Identity encoding

The three provider keys are:

- Drive file: `connection_id + file_id`;
- sheet scope: `connection_id + spreadsheet_id + sheet_id`;
- register row: sheet scope plus `immutable_business_id`.

The exact typed provider key remains inspectable beside the opaque identifier.
`external_object_id` is `gsk1:` plus the unpadded, lowercase Base32 encoding of
SHA-256 over UTF-8 canonical JSON (`sort_keys=true`, compact separators,
`ensure_ascii=false`) of that complete key. It is 57 characters, fits the
existing `SourceObject` bound and includes the scheme version independently in
`external_generation`. No filename, path, title, row/A1 coordinate, content
digest or Developer Metadata value enters identity.

## Row revision profile

`google-sheets-row-revision:v1` hashes the canonical row-original JSON. Cells
are ordered by `column_key`; finite decimal values use non-exponent decimal
notation with insignificant trailing fractional zeroes removed. The revision
data contains the exact business row key, locale, timezone and, for every
selected column, entered/formula, effective, formatted and number-format
representations. Blank cells are explicit typed values.

Sheet title, observed row/A1 coordinates, Developer Metadata and observation
time live in the separate observation/location contract. They never enter the
row content digest. Changing a selected value, formula, formatted value or
number format does enter the digest.

## Locator resolution

`GoogleSheetCellLocator` pins the exact `source_revision_id`, spreadsheet,
stable numeric sheet ID, immutable business ID, configured column and selected
representation. Its observed A1 range and cell coordinate are provenance hints
only. Resolution reads the immutable `GoogleSheetRowOriginal`; it rejects a
wrong revision, provider row key or column and never consults a live sheet.

This ticket defines the locator as a standalone evidence contract; it does not
widen the persisted `ParsedSegment` locator union or an active projection
format. The Sheets reader/ingestion slice adds that adapter integration at the
shared canonical handoff; it does not widen the active projection locator union.
Consequently #600 and #603 require no projection rebuild or rollback. If a later slice
adds the locator to active projection lineage, it must use ADR-0043's normal
successor-publication path, retain the last active publication on rebuild
failure, roll back through a new successor, and extend the pinned evidence
lineage regression evaluation before activation.

## Connector observation handoff

Issue #601 adds `connector-observation:v1` and the application
`ConnectorIngestionOrchestrator`. A Google reader returns a normalized
observation only after the trusted `GoogleReadAdmission` boundary has accepted
the connection, resource scope, authorization generation and configuration
generation. The observation contains the exact normalized bytes, media type,
extraction/locator profile versions, provider identity, event provenance and
coverage state; it contains no provider SDK or Cognee value.

The application handoff then reuses the existing original-upload, source
observation, admission, canonical-acceptance, ingestion-ledger and
transactional projection-outbox ports. Connector `SourceObject` identity is
derived from workspace/environment, connection and the versioned external
identity, while revision identity is derived from that source, the observed
original digest and the selected representation profile. A repeated command is
idempotent; a conflicting replay is rejected by the existing idempotency and
observation repositories.

No connector code calls Cognee directly. Projection remains a downstream
consumer of the committed `ingestion.projection.requested` intent.

## Drive file reader

Issue #602 adds a provider-neutral Drive adapter behind the same admission and
handoff boundary. It reads one explicitly approved file by taking metadata,
downloading the exact bytes (or using the configured export MIME), and taking
metadata again. The candidate is rejected if the file ID or provider version
changes during that sequence. The resulting observation retains the provider
MIME, representation profile, exact bytes and both version-guard values.

`connection_id + file_id` is mapped through `google-drive-file-key:v1`; file
name, URL, drive/parent metadata and the current provider version are
provenance or operational evidence, not source identity. The normalized
handoff carries the provider version as an ordering token and the selected
MIME/profile into the existing immutable original, `SourceRevision`, acceptance
and projection-outbox path. The adapter has no cursor, deletion behavior,
Cognee dependency or credential handling.

Issue #604 extends this handoff with Drive revision semantics. The provider
version is validated as canonical decimal and compared numerically by the
`google-drive-version:v1` ordering policy, so an older observation cannot move
the canonical head backwards. A metadata guard mismatch causes the adapter to
discard the torn read and retry from a fresh metadata/version pair; the bounded
profile fails with `GoogleDriveConcurrentModificationError` when retries are
exhausted.

Drive file name, URL, parent IDs and modified time participate in the immutable
event provenance digest and event identity, but not in content revision
identity. Therefore rename, move, URL-only and modified-time-only observations
append provenance to the same `SourceObject`/`SourceRevision`. The approved
representation profile is revision-bearing, so changing download/export
representation creates a distinct immutable revision even when the returned
bytes happen to match. A connector handoff emits a projection intent only when
canonical acceptance advances to a different revision; repeated or
operational-only observations do not duplicate downstream work.

## Sheets register-row reader

Issue #603 adds the first production-selected row-aware Sheets adapter. Its
fakeable service boundary models one bounded `spreadsheets.get`-style response
for the configured `spreadsheet_id` and stable numeric `sheet_id`; it never
uses Drive CSV export. `GoogleSheetsReadProfile` fixes the business-ID column,
selected grid columns, optional Developer Metadata key and optional target row.

The adapter preserves each selected column's entered/formula, effective,
formatted and number-format values, together with spreadsheet locale and time
zone, in `GoogleSheetRowOriginal`. Row number, A1 range, sheet title and a
validated Developer Metadata ID are stored in `GoogleSheetRowLocation` only.
They are not part of the row revision digest.

The reader fails closed with a typed `GoogleSheetsUnsafeIdentityError` for a
missing or conflicting business ID, duplicate ID in the observed scope, or a
configured Developer Metadata mismatch. It also rejects a response whose
spreadsheet or numeric sheet ID, configured column, or observed cell coordinate
does not match the approved profile. No rejected row is converted into a
normalized observation, so the shared connector handoff cannot create an
accepted revision or projection intent for it.

A valid row maps to `NormalizedConnectorObservation` with the existing
`google-register-row-key:v1` identity and `locator:google-sheet-cell:v1`
contract. The observation then enters the same immutable-original,
SourceObject/SourceRevision, canonical-acceptance and transactional-outbox
path as the Drive reader. Replaying an unchanged row reproduces the same
original bytes, event identity and revision digest; locator resolution remains
bound to the exact stored row revision and representation.

Before and after the configured spreadsheet/range extraction, the reader checks
the provider file version. A version change rejects the whole candidate, so no
partial row reaches canonical ingestion. The original row digest includes only
configured cell representations and spreadsheet locale/timezone. Row/A1
position, sheet title and Developer Metadata locator details stay in
revision-scoped provenance. A location-only observation appends provenance to
the current revision without repeating canonical acceptance or requesting a
new projection. A changed row representation creates a new revision and follows
the normal acceptance and projection-intent path.

## Drive initial snapshot

Issue #606 adds the first bounded durable Drive enumeration. The coordinator
captures the start page token before listing the approved scope and stores it as
the run's proposed boundary. Every listed page is persisted with its requested
and next page tokens, `incompleteSearch` status, workspace/environment,
connection, service principal and authorization/configuration generations.

Each file is handed to the existing connector ingestion orchestrator with a
stable per-file idempotency key. A page cannot complete, and the proposed
boundary cannot become the committed cursor, until every item has a durable
terminal canonical outcome. An interrupted page reopens from the same page
token and skips already successful item outcomes. Item/provider failures leave
the previous committed cursor unchanged and mark the run resumable;
`incompleteSearch` produces `reconciliation_required` instead of advancing the
cursor. The coordinator owns this state and the Drive adapter has no hidden
unbounded retry loop.

## Drive incremental changes

Issue #607 adds a separate durable change-feed coordinator. It starts from the
last committed cursor produced by the initial snapshot, persists every page and
per-change terminal outcome, and keeps the provider's `newStartPageToken` as a
proposed cursor until the final page and all canonical ingestion effects are
durable. A replay uses the same change-derived idempotency key, so a crash after
canonical acceptance but before the ledger checkpoint cannot create a second
logical acceptance or advance the cursor prematurely. Canonical ingestion's
numeric Drive revision ordering remains authoritative for duplicate and
out-of-order file versions; the change ledger records the replay outcome without
overwriting that head.

Authority, scope and configuration generations are captured in the run and are
checked before a resumed page is read. Provider token expiry, retry exhaustion,
token gaps, incomplete pages and a missing terminal cursor retain the previous
cursor and enter `reconciliation_required`, which emits one bounded,
idempotent full-reconciliation command. Removed changes are recorded as a
terminal ignored outcome; deletion/tombstone policy remains outside this slice.

## Sheets full reconciliation

Issue #608 adds a bounded row reconciliation invoked either by a Drive
spreadsheet change wake-up or by a periodic full-scan command. The caller
supplies the approved immutable spreadsheet/sheet/A1-range set from the current
connection configuration. The coordinator reads every page in every configured
range and requires each page to be complete, correctly scoped and guarded by
one stable spreadsheet provider version before it starts canonical ingestion.

The durable tenant-scoped run ledger stores only opaque row identities,
revision references, safe state/failure codes, configured scope identities,
page tokens, provider version and captured workspace/connection authority. It
does not store row payloads. After the entire read set passes its guards, each
normalized row goes through the existing canonical connector handoff with a
stable row identity-derived idempotency key. A crash after acceptance but
before the item checkpoint safely replays that same command; unchanged row
content and coordinate-only movement reuse the existing `SourceRevision`.

An incomplete page/range, pagination cycle, row/page bound breach, duplicate
business identity or mid-read version change leaves the prior cursor in force
and accepts no rows from that candidate read. Failed canonical writes leave
the run resumable; successful completion promotes the proposed Drive cursor.
Scheduled full reconciliation uses the same coordinator without proposing a
Drive cursor. The ledger records no absence decisions, tombstones or quarantine
dispositions; those remain separate policy work.
