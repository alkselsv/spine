# Issue #154 document-ingestion qualification

Status: **implemented and qualified for the supported local single-file path**.

The qualification covers the five R1 formats in the Issue #154 scope: TXT,
Markdown, PDF, DOCX, and XLSX. The application orchestrator now routes every
supported media type through its production parser adapter, persists only
successful or partial parsed artifacts, and records parser identity and stable
locator metadata on every processing receipt, including failed parses.

Batch coordination, tombstone/reappearance, and projection delivery remain
owned by the follow-up qualification ticket #194 and are intentionally not
claimed here.

## Evidence matrix

| Requirement | Evidence |
| --- | --- |
| All five formats complete the parser/orchestration path | `tests/application/test_document_ingestion.py::test_document_ingestion_completes_each_r1_format_with_parser_metadata` |
| Exact locator round-trips | The same test reopens a physical-line, PDF page, DOCX OOXML, or XLSX cell locator; parser contract suites are `tests/ingestion/test_pdf_parser.py`, `tests/ingestion/test_docx_parser.py`, and `tests/domain/artifacts/test_xlsx_parser.py`. |
| Stable replay and one logical processing outbox effect | `tests/application/test_document_ingestion.py::test_document_ingestion_completes_text_path_and_replays_stably` and the filesystem replay/concurrency suite in `tests/integration/object_storage/test_filesystem.py`. |
| Failed parsing remains distinct and content-safe | `tests/application/test_document_ingestion.py::test_document_ingestion_failed_parse_keeps_parser_evidence_without_output_artifact` plus the malformed/bounded parser cases. |
| Durable upload/status HTTP contract and workspace denial | `tests/api/test_ingestion.py` and `tests/application/test_ingestion_api.py`, using `httpx.ASGITransport`. |
| Legacy `/ingest` and loader compatibility | `tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header` and `tests/domain/artifacts/test_xlsx_parser.py::test_parse_document_selects_xlsx_adapter_without_using_legacy_loader`. |
| Domain and compile boundaries | `tests/test_architecture_scaffold.py`, the repository offline suite, and `uv run python -m compileall -q src tests`. |

The qualification uses synthetic, in-memory documents and temporary filesystem
roots. No customer content, credentials, or provider payloads are part of the
evidence. PostgreSQL adapter and migration coverage remains exercised by the
repository's opt-in PostgreSQL integration suite; it is not silently presented
as a live database qualification when that prerequisite is unavailable.

## Verification commands

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

The follow-up #194 owns the complete multi-file, tombstone, and projection
qualification and must reuse this single-file evidence.
