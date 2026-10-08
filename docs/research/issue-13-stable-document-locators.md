# Issue #13: stable locators for R1 documents

Status: **prototype result, not production ingestion**. The question is whether
one parser boundary can emit repeatable, citation-grade locations for PDF, DOCX,
XLSX, Markdown, and UTF-8 text while retaining structure. This result informs
`to-spec` and the contracts in Issues #9, #3, and #21; it does not implement them.

## Reproduce

From the repository root, after `uv sync --extra dev`:

```bash
uv run python tests/prototypes/issue_13_stable_locators.py
```

The explicitly throwaway [prototype runner](../../tests/prototypes/issue_13_stable_locators.py)
creates all five synthetic documents in a temporary directory, prints the full
parsed records and failure outcomes, then removes the originals. No customer data,
provider, database, Cognee index, or production parser path is involved. Runs used
Python 3.12.3, pypdf 6.18.1, python-docx 1.2.0, and openpyxl 3.1.5 from `uv.lock`.
Two separate processes produced byte-identical JSON output after the synthetic
DOCX/XLSX ZIP metadata was fixed (SHA-256
`3becefae0549feb3964185a1fe126ab2146d6e423d74b232a5191b1fe43700e3`).
Every successful record is independently resolved against
the generated original and compared with its pre-normalization text.

## Observed results

| Format | Locator demonstrated | Evidence and boundary |
| --- | --- | --- |
| Markdown | Raw Unicode character range, physical line, optional heading path | CRLF, empty line, emoji and table-like syntax retain exact raw ranges. A heading path alone is ambiguous; line and range distinguish duplicates. |
| TXT | Raw Unicode character range and physical line | Identical `Повтор 😀` text at lines 1 and 3 yields distinct references. Invalid UTF-8 is rejected rather than replaced. |
| DOCX | Ordered OOXML block path through paragraphs, tables, rows, cells and nested tables; character range within paragraph | Identical text in body and table is distinct. Nested table text resolves to its exact structural path. Empty paragraphs are skipped. Merged cells, header stories and unknown blocks are rejected until locator strategies exist. |
| XLSX | Worksheet name, physical row, A1 cell and merged cell range | Nonempty cells at rows 1, 3, 5 and 9 remain at those physical rows. The current compatibility loader labels the same non-header data as `row2`, `row4`, `row8`, proving the physical-row mismatch. `A9:B9` resolves as a merged range. Formula `C7` without a cached value fails explicitly. |
| PDF | 1-based page and character range in that page's extracted text | Repeated text on page 1 has distinct ranges. A drawn two-column table on page 2 extracts as `Item Value` and `A 42`: its cell geometry is not recoverable from this output. Blank page and malformed PDF fail. |

The change-of-revision experiment preserves old TXT bytes while replacing the
current file. The old `SourceRevision`-scoped locator resolves against the saved
old bytes. Resolution rejects the new revision ID even with the old original,
and rejects different original bytes even when the same excerpt remains at the
same position. A revision ID and exact original digest must therefore bind
evidence; a path, filename, excerpt hash or bare locator cannot substitute for
them. The experiment supplies synthetic revision IDs; it does not decide
canonical acceptance or current revision.

Normalization is intentionally downstream of raw extraction. The runner retains
both `raw` and whitespace-collapsed `normalized`; its locators always address
the original or pinned parser output. A search hit in normalized text must carry
its originating raw segment and locator. Recomputing a position by searching for
the answer text fails on duplicates and after whitespace changes.

## Limits and failure policy

- **PDF:** `pypdf` provides page text, not a stable visual cell or bounding-box
  model. Page numbers refer to the immutable PDF; extracted offsets refer to a
  specific parser/configuration version. Scans, blank pages, extraction errors,
  ambiguous reading order and table cells require typed exclusions or another
  validated extraction path. The prototype rejects a page with no extractable
  text. A page-wide excerpt may be cited only if it supports the exact claim;
  `page=2` alone does not prove a claimed table cell.
- **DOCX:** OOXML child indices resolve against the exact immutable original.
  Editing the document creates a new revision and can shift all paths. Headers,
  footers, notes, comments, tracked changes, drawings and merged-table grid
  positions need explicit coverage rules; this prototype only traverses body
  paragraphs and tables. It rejects linked header/footer/note/comment stories,
  merged cells, drawings and unknown body blocks instead of silently declaring
  complete coverage. Other OOXML constructs still need a production coverage
  inventory before complete-document status can be trusted.
- **XLSX:** A worksheet title and physical A1 coordinates identify stored cells
  within one revision. A computed formula value is evidence only when a cached
  value is present and its meaning is accepted by the parser policy. Formulas
  without cache, hidden sheets/rows, comments, charts, external links and merged
  cells beyond the demonstrated top-left value need an explicit policy. A merged
  range is not a license to infer values in its other cells.
- **Markdown/TXT:** Strict UTF-8 decoding and raw code-point offsets were tested.
  Other encodings require an explicit decoder/version. Markdown heading text is
  contextual metadata, not identity: duplicate headings and malformed syntax
  need the raw range. Markdown tables here are located as source lines; cell
  semantics were not inferred. The parser must specify whether offsets count
  Unicode code points, UTF-8 bytes or UTF-16 units and keep that choice stable.
- **All formats:** An unresolvable locator, missing revision identity, digest
  mismatch, unsupported structure, parser exception or missing source text must
  produce a typed failure/exclusion. It must never generate a citation from an
  approximate text match or error message. Whether partial parsing can serve a
  question is a request-specific completeness decision; failed segments cannot
  silently count as covered evidence.

The current compatibility loader in `src/spine/ingest/loaders.py` does not meet
these guarantees: it flattens PDF/DOCX, converts their extraction exceptions into
indexable text, decodes Markdown/TXT with replacement characters, and labels
XLSX nonempty rows with an ordinal that differs from the physical row after
gaps. This finding is for Issue #3's future ingestion specification; the loader
is unchanged here.

## Recommended typed boundary for `to-spec`

Use a versioned parsed-document result owned by the ingestion boundary:

```text
ParsedDocument
  source_revision_id, original_sha256, media_type
  parser_name, parser_version, parser_config_version
  coverage: complete | partial | failed
  segments: ParsedSegment[]
  exclusions: ParseExclusion[]

ParsedSegment
  segment_id (revision + parser version + locator, deterministic)
  kind (paragraph | line | table_cell | sheet_cell | page_text | ...)
  raw_text, normalized_text
  locator: discriminated union by source format
  parent/heading context (optional, never sole identity)

Locator
  text/markdown: line + raw_char_start/end
  docx: OOXML structural block_path + paragraph char_start/end
  xlsx: sheet + physical row + A1 cell/range
  pdf: page + extracted_char_start/end + extraction version

ParseExclusion
  format, location if known, reason code, parser version,
  effect on coverage/completeness
```

For PDF and DOCX, an excerpt validator should reopen the immutable original
with the pinned parser configuration and confirm the raw span. For text, slice
the strict-decoded original. For XLSX, read the exact cell/range. Reject when
the resolved text differs. Stable segment IDs and locators are scoped to one
`SourceRevision`; parser or normalization upgrades require a new projection
configuration and regression comparison, not mutation of existing references.

Parser output is an observation/projection input, never a canonical business
fact. Before any Q&A disclosure, the Context Broker must still validate the
exact current revision, tombstone status, source coverage and current
`AccessPolicy` under ADR 0011 and ADR 0018. Historical citation inspection is
separate from ordinary current-revision retrieval.

## Verdict

**One typed parser boundary is feasible for these formats, but no universal
locator is.** Use a format-discriminated locator scoped to an immutable
`SourceRevision`, preserve raw-to-normalized segment lineage, and fail closed on
unlocatable or incomplete material. Issue #9 can define citation identity and
validation around these guarantees; Issue #3 can specify the parser and coverage
contract. Table-cell claims from PDF and unsupported DOCX structures remain
explicitly outside the demonstrated guarantee until a separate parser is proven.
