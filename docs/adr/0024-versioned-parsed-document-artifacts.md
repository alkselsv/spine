# ADR-0024: Versioned parsed-document artifacts

## Status

Accepted

## Context

R1 parsing must preserve the immutable `SourceRevision` boundary while making
TXT, Markdown, PDF and DOCX output reproducible, tenant-scoped and inspectable by
exact locators. Parser output is a projection of source evidence, not a
replacement for the source ledger.

## Decision

Spine persists a frozen, schema-versioned `ParsedDocument` whose identity is a
SHA-256 digest of source/revision identity, original digest, parser and config
versions, locator-contract version, coverage, exclusions and segment content
digests. Each `ParsedSegment` has a digest-derived UUID and a format-specific
locator governed by the document's independently versioned locator contract.
TXT/Markdown use physical-line, raw-Unicode-code-point locators; Markdown
heading paths are context and do not change locator semantics.

PDF parsing uses a lazy `pypdf` adapter. Its parser identity pins the installed
`pypdf` version, and its configuration identity pins byte, page, per-page text
and total extracted-text limits. PDF segments use a 1-based page number plus a
half-open Unicode-code-point range in that page's extracted text. The locator
therefore distinguishes repeated text without approximate search but remains
valid only with the pinned parser and extraction configuration.

DOCX parsing uses a lazy `python-docx` adapter over the immutable OOXML package.
Each text segment identifies one paragraph by an ordered path of paragraph,
table, row and cell steps, including nested tables, plus a half-open Unicode
code-point range within the paragraph. The parser identity pins the installed
`python-docx` version; the configuration identity pins compressed input,
archive-entry, uncompressed-size, structural-block, paragraph-text,
total-extracted-text and nesting-depth limits.

TXT/Markdown parsing is strict UTF-8 and bounded. Unsupported Markdown fenced
structures and malformed input produce typed exclusions; partial or failed
coverage can never be reported without an exclusion. Parsed documents and
segments are inserted atomically into immutable PostgreSQL tables protected by
workspace/environment RLS. Cognee and other projections consume these artifacts
through application ports and are not their source of truth.

Blank PDF pages, malformed files, extraction failures and resource-limit
failures produce content-free typed exclusions. A document with no safe segment
has failed coverage; a document with safe segments plus exclusions has partial
coverage. Resolver calls require the exact workspace, environment, source
revision, revision digest, original digest, parser version, locator-contract
version and locator already enforced by the shared artifact repository.
The parsed-document and parsed-segment envelopes remain at v1: PDF adds a new
format-specific locator contract rather than changing either envelope's
identity or lifecycle semantics.

DOCX header, footer, note and comment stories remain excluded until they have
their own locator strategy. Drawings, ambiguous merged cells, unknown body or
cell blocks, malformed OOXML packages and bounded subtrees produce content-free
typed exclusions. Text inside an excluded drawing or bounded nested table is not
promoted into a segment. Exact DOCX resolution reopens the authorized original,
walks the structural path and validates the stored paragraph range; it never
searches for duplicate text approximately.

## Consequences

The artifact can be replayed and resolved exactly without approximate text
search, while changed command digests remain conflicts. New format adapters
must implement the same parser/artifact port and introduce their own locator
contract version and regression fixtures. An in-memory implementation is
maintained alongside PostgreSQL for offline contract tests.
