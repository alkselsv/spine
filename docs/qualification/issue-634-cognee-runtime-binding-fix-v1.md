# Issue #634 Cognee runtime binding lifecycle fix v1

Status: **implemented and verified offline; recovered application remains
NOT_VERIFIED**.

The first authorized #634 run proved that the complete runner could retain a
Cognee 1.5.4 database configuration created before the fresh recovery target
was selected. Cognee memoizes its relational, vector and graph settings. The
Spine public-SDK boundary configured project-local storage but did not discard
those earlier settings, so the runner correctly stopped before migrations,
provider calls or Context Broker access with
`cognee_runtime_binding_mismatch`.

The adapter now fingerprints the process database environment without retaining
or logging configuration values. Before the first real SDK bridge is exposed,
it invalidates any earlier Cognee relational, vector, graph, migration and base
configuration caches, materializes the selected binding and freezes it for the
life of the process. Repeating construction with the same binding is stable. A
later attempt to retarget the initialized process fails closed with the same
content-free reason code.

The deterministic regression starts with all three Cognee database surfaces
cached against synthetic stale targets, selects fresh synthetic targets and
constructs the real installed Cognee 1.5.4 public SDK bridge. It proves that the
fresh relational, vector and graph settings are selected, that repeated setup
does not recreate them, and that a subsequent target change is rejected without
including the target in the exception.

Offline verification:

```text
uv run pytest -q tests/test_cognee_runtime_binding_lifecycle.py \
  tests/memory/cognee/test_indexing.py \
  tests/memory/cognee/test_retrieval.py \
  tests/workers/test_runtime.py \
  tests/qualification/test_issue634_qualification.py
```

This fix makes another run eligible for consideration; it does not authorize or
claim one. A future run still requires a fresh isolated target and separate
authorization, provider-backed projection and publication, exactly one
authorized Context Broker request, cleanup, metadata-only evidence and an
independent final evidence review.
