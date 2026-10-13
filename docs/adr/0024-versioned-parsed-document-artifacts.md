# ADR-0024: Versioned parsed-document artifacts

## Status

Accepted

## Context

R1 parsing must preserve the immutable `SourceRevision` boundary while making
TXT and Markdown output reproducible, tenant-scoped and inspectable by exact
locators. Parser output is a projection of source evidence, not a replacement
for the source ledger.

## Decision

Spine persists a frozen, schema-versioned `ParsedDocument` whose identity is a
SHA-256 digest of source/revision identity, original digest, parser and config
versions, locator-contract version, coverage, exclusions and segment content
digests. Each `ParsedSegment` has a digest-derived UUID and a physical-line,
raw-Unicode-code-point locator. Markdown heading paths are context and do not
change locator semantics.

TXT/Markdown parsing is strict UTF-8 and bounded. Unsupported Markdown fenced
structures and malformed input produce typed exclusions; partial or failed
coverage can never be reported without an exclusion. Parsed documents and
segments are inserted atomically into immutable PostgreSQL tables protected by
workspace/environment RLS. Cognee and other projections consume these artifacts
through application ports and are not their source of truth.

## Consequences

The artifact can be replayed and resolved exactly without approximate text
search, while changed command digests remain conflicts. New format adapters
must implement the same parser/artifact port and introduce their own locator
contract version and regression fixtures. An in-memory implementation is
maintained alongside PostgreSQL for offline contract tests.
