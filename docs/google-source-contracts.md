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
format. The Sheets reader/ingestion slice must add that adapter integration with
its persistence and projection contract tests before emitting these locators.
Consequently #600 requires no projection rebuild or rollback. If a later slice
adds the locator to active projection lineage, it must use ADR-0043's normal
successor-publication path, retain the last active publication on rebuild
failure, roll back through a new successor, and extend the pinned evidence
lineage regression evaluation before activation.
