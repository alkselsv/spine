# ContextProjection contract suite

`test_conformance.py` is the provider-neutral behavior suite for every
`ContextProjection` implementation. It receives an implementation through the
`context_projection_factory` fixture and never imports a provider SDK.

The default fixture runs the suite against
`DeterministicContextProjection`. A physical adapter adds its factory to the
`params` list in `conftest.py` (or overrides the fixture from its own test
package) and supplies the same `ContextProjectionContractAdapter` wrapper.
The `seed_retrieval` callback is test setup only; it is not a second production
port or a second reference fake.

The suite covers command replay and digest conflicts, revision/evidence fences,
structured retrieval provenance, tenant/environment isolation, safe diagnostics,
shadow namespace exclusion, physical partial failure recovery, and the
project/update/remove/rebuild operations.

`test_context_broker_v1.py` covers the provider-neutral Context Broker v1
request/response seam. Its deterministic profile, retrieval and bundle ports
are explicitly fake-backed and do not qualify production authorization or
evidence disclosure. The legacy `ContextBroker` protocol remains available;
`LegacyContextBrokerAdapter` is the compatibility seam for v1 dispositions.

`CogneeChunkProjectionStore` is a lower-level `ProjectionEffectStore` write
adapter, not a complete `ContextProjection`: it intentionally has no retrieval
or seed hook. Its public effect seam is covered by the provider-free SDK fake
tests in `tests/memory/cognee/test_indexing.py`; the shared suite above remains
for complete ContextProjection implementations.
