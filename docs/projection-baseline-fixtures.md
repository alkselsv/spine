# R1 projection baseline fixtures

This document describes the offline contract supplied by Issue #196. The
definitions are adapter inputs and synthetic qualification data; they are not
the PostgreSQL-owned OntologyVersion or ProjectionConfigVersion records from
Issue #7.

The public implementation is
spine.memory.cognee.models.baseline. It has no Cognee or network dependency.

## Versioned definitions

BASELINE_ONTOLOGY is ontology-definition:r1, version r1, with this allowlist:

| Entity | Aliases |
| --- | --- |
| person | human |
| organization | company |
| document | — |
| location | — |

| Relation | Source | Target | Aliases |
| --- | --- | --- | --- |
| works_for | person | organization | employed_by |
| located_in | organization | location | — |

Its canonical SHA-256 digest is
90b2bf6ad1835c48d1ee46b250c47feabeb1ecb7d9e5bb049da2fccc4422ecee.

BASELINE_EXTRACTION_CONFIG is extraction-config:r1, version r1. It pins the
ontology digest, stable-locator:r1, provider-neutral extractor contract
provider-neutral:v1, entity confidence 0.75, and relation confidence 0.8. Its
canonical SHA-256 digest is
e90df3e1eb73c4a8a722dd393ecdf21d3a88c1c3e852f65262efa23998f4202a.

Both definitions are frozen Pydantic models with extra="forbid". The digest is
computed over the JSON representation with sorted keys, compact separators, and
UTF-8 encoding; the digest field itself is excluded.

## Fixture schema

build_baseline_fixtures() returns immutable ProjectionFixture values keyed by
scenario:

- source_revisions: synthetic source identities, revision digest, revision
  number and current/stale/deleted state;
- chunks: synthetic chunk identity, source revision and stable evidence;
- entities: typed projected entities, source revision, chunk IDs and evidence;
- relations: typed endpoint IDs, source revision and evidence;
- conclusions: probabilistic extraction proposals, deliberately separate from
  accepted entities and relations.

Every accepted entity or relation must carry at least one EvidenceReference. A
reference includes the exact source_revision_id, a StableLocator, and an
evidence quality. The validator rejects evidence that is missing, ambiguous,
stale, tombstoned, or attached to another revision. Relations additionally
require ontology-approved endpoint types and a matching source revision for
both endpoints.

validate_fixture(fixture) returns only opaque accepted IDs. It raises
FixtureValidationError with a stable safe code and case ID for invalid
fixtures. It never includes source text in the exception or result.

The checked-in scenarios cover positive, forbidden, invalid_relation,
missing_evidence, ambiguous_evidence, ambiguous_mapping,
cross_revision_evidence, stale_revision, and tombstone_revision. The fixtures
are deterministic, synthetic, immutable and independent of LLMs, Cognee,
filesystems and network services.
