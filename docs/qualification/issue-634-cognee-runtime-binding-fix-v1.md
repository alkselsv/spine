# Issue #634 Cognee runtime binding lifecycle fix v1

Status: **implemented and covered by deterministic offline tests; recovered
application remains BLOCKED / NOT_VERIFIED**.

The first authorized #634 run proved that the complete runner could retain a
Cognee 1.5.4 database configuration created before the fresh recovery target
was selected. Cognee memoizes its relational, migration, vector and graph
settings. The Spine public-SDK boundary configured project-local storage but
did not discard those earlier settings, so the runner correctly stopped before
migrations, provider calls or Context Broker access with
`cognee_runtime_binding_mismatch`.

The adapter now fingerprints the process database environment without retaining
or logging configuration values. Before the first real SDK bridge is exposed,
it invalidates any earlier Cognee relational, vector, graph, migration and base
configuration caches, materializes the selected binding and freezes it for the
life of the process. Repeating construction with the same binding is stable. A
later attempt to retarget the initialized process fails closed with the same
content-free reason code.

The default deterministic regression uses a cache-shaped Cognee port fake. It
starts with all four database configuration surfaces cached against synthetic
stale targets and proves that fresh relational, migration, vector and graph
settings are selected. Separate tests prove that repeated setup with the same
binding is accepted and that a subsequent target change is rejected without
including the target in the exception. The provider-backed recovery canary
remains a separately authorized, opt-in integration exercise.

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
