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
identity, while revision identity is derived from that source and the observed
original digest. A repeated command is idempotent; a conflicting replay is
rejected by the existing idempotency and observation repositories.

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
