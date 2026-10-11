# Downstream diagnostics conformance

The helpers in `fixtures.py` are the shared trace/error seam for downstream
modules. They depend only on Spine's application contracts and can be reused
without FastAPI, SQLAlchemy, Cognee, Temporal, or a model-provider SDK.

- `assert_downstream_diagnostic_context` verifies the exact trusted trace,
  correlation, and immediate causation identity observed by a consumer.
- `assert_safe_structured_error` verifies the complete Structured Error v1
  public field set.
- `assert_no_diagnostic_leaks` applies the common secret, document, prompt,
  answer, SQL, URL, provider-failure, and traceback corpus to captured public
  responses and diagnostics.
- `deterministic_id_source`, `diagnostic_context`, and `synthetic_uuid` keep
  conformance tests independent of randomness and wall-clock state.

Tickets #3 (ingestion), #15 (projection), #25 (generation), #26 (API/SSE), and
#27 (retrieval) must apply these helpers at their own public input/output seam.
They should add their module-specific protected values to `leak_corpus` rather
than define another trace or error envelope.
