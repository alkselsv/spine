# Cognee dependency and upgrade policy

Spine supports exactly Cognee `1.5.4`. This is the version whose public API was
audited against the Context projection contract. `pyproject.toml` pins that
version exactly and `uv.lock` must resolve the same version for every supported
Python release (`>=3.10,<3.15`). A compatible-release or lower-bound specifier is
not sufficient because an unaudited Cognee release may change projection,
provenance, deletion, backend, or access-control behavior.

The adapter must use public Cognee SDK calls. A version upgrade must not preserve
behavior by importing or patching Cognee internals. The audit rationale and
public API inventory are recorded in
[`research/cognee-1.5.4-context-projection-contract.md`](research/cognee-1.5.4-context-projection-contract.md).

## Upgrade gate

A pull request may change the exact pin only after the candidate version passes
all of the following contract suites against the supported backend profiles:

- `projection-rebuild`: `tests/contracts/memory/test_projection_rebuild_contract.py`
- `provenance-mapping`: `tests/contracts/memory/test_provenance_mapping_contract.py`
- `retrieval`: `tests/contracts/memory/test_retrieval_contract.py`
- `deletion`: `tests/contracts/memory/test_deletion_contract.py`
- `backend-contract`: `tests/contracts/memory/test_backend_contract.py`
- `acl-leakage`: `tests/contracts/memory/test_acl_leakage_contract.py`

The suites must prove, at minimum:

- shadow rebuild, validation, activation, failed rebuild, and rollback by
  publishing a successor snapshot at a non-regressing canonical boundary;
- stable `SourceRevision` to chunk/entity/relation/reference mappings and
  citation-grade locators;
- structured retrieval parity without accepting a Cognee completion string as a
  Spine answer;
- tombstone fencing, repeated deletion, update, and absence of stale evidence;
- backend namespace isolation, cutover, rollback, and failure recovery;
- allowed, denied, mixed-access, and two-workspace retrieval with zero protected
  content leakage.

The upgrade pull request must record the candidate version, backend matrix,
commands, results, and metric deltas. Real-provider checks may remain opt-in, but
the offline fake and adapter contract suites are mandatory. The repository
dependency-contract test rejects a post-`1.5.4` audited version until all six
test paths exist; the full pytest gate then executes them. Only after those
results are accepted may the pull request update the audited version in the
dependency-contract test, `pyproject.toml`, and `uv.lock` together.

If a candidate fails any gate, keep `1.5.4` pinned. If a regression is found
after adoption, restore the last accepted exact pin and lockfile and publish a
new successor `ProjectionSnapshot` at a non-regressing `CanonicalBoundary`.
Prior physical artifacts may be reused only after revision identity, provenance,
compatibility, and integrity are verified; an older snapshot is never simply
reactivated. Rebuild derived projections from canonical revisions where needed.
