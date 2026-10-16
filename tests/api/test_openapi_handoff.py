from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from spine.api.openapi import create_openapi_app


def test_generation_export_matches_backend_openapi_document(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    subprocess.run(
        [
            sys.executable,
            "web/api/codegen/export_openapi.py",
            str(output),
        ],
        check=True,
    )

    document = json.loads(output.read_text(encoding="utf-8"))

    assert document == create_openapi_app().openapi()
    assert "/api/v1/documents/batches" in document["paths"]
    assert "/api/v1/documents/processing-history" in document["paths"]
    assert "/api/v1/documents" in document["paths"]
    assert "/api/v1/documents/{source_object_id}" in document["paths"]
    assert "/api/v1/documents/{source_object_id}/revisions" in document["paths"]
    assert "/api/v1/ingestion/runs" in document["paths"]
    assert "/api/v1/ingestion/runs/{run_id}" in document["paths"]
    assert "/api/v1/documents/uploads" in document["paths"]
    assert "/api/v1/ingestion/runs/{run_id}/items/{item_id}" in document["paths"]
    assert "/api/v1/projections/{projection_kind}/status" in document["paths"]
    assert "/api/v1/projections/{projection_kind}/recovery" in document["paths"]
    assert "/api/v1/qa/citations/{citation_id}" in document["paths"]
    assert "/api/v1/qa/sessions/{session_id}/messages/{message_id}/events" in document["paths"]
    assert "/api/v1/knowledge/documents/{source_object_id}/revisions/{source_revision_id}/parsed-content" in document["paths"]

    upload_operation = document["paths"]["/api/v1/documents/uploads"]["post"]
    upload_parameter_names = {
        parameter["name"] for parameter in upload_operation["parameters"]
    }
    assert {
        "Idempotency-Key",
        "X-Upload-Identity",
        "X-Filename",
        "X-Content-SHA256",
        "X-Workspace-ID",
        "X-Environment-ID",
    } <= upload_parameter_names
    digest_parameter = next(
        parameter
        for parameter in upload_operation["parameters"]
        if parameter["name"] == "X-Content-SHA256"
    )
    assert digest_parameter["schema"] == {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": "^[0-9a-f]{64}$",
    }
    assert upload_operation["responses"]["202"]["content"]["application/json"][
        "schema"
    ]["$ref"] == "#/components/schemas/IngestionUploadResponse"

    item_status_operation = document["paths"][
        "/api/v1/ingestion/runs/{run_id}/items/{item_id}"
    ]["get"]
    assert item_status_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"] == "#/components/schemas/IngestionStatusResponse"

    status_operation = document["paths"]["/api/v1/projections/{projection_kind}/status"]["get"]
    recovery_operation = document["paths"]["/api/v1/projections/{projection_kind}/recovery"]["post"]
    assert status_operation["operationId"] == "projection.status"
    assert recovery_operation["operationId"] == "projection.recovery"
    citation_operation = document["paths"]["/api/v1/qa/citations/{citation_id}"]["get"]
    assert citation_operation["operationId"] == "qa.citations.inspect"
    assert citation_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"] == "#/components/schemas/ApiResponseEnvelope_CitationInspectionResult_"
    parsed_content_operation = document["paths"][
        "/api/v1/knowledge/documents/{source_object_id}/revisions/{source_revision_id}/parsed-content"
    ]["get"]
    assert parsed_content_operation["operationId"] == "knowledge.parsed_content.inspect"
    assert parsed_content_operation["parameters"]
    assert document["paths"]["/api/v1/documents"]["get"]["operationId"] == "documents.list"
    assert document["paths"]["/api/v1/documents/{source_object_id}"]["get"]["operationId"] == "documents.detail"
    assert document["paths"]["/api/v1/documents/{source_object_id}/revisions"]["get"]["operationId"] == "documents.revisions"
    stream_operation = document["paths"][
        "/api/v1/qa/sessions/{session_id}/messages/{message_id}/events"
    ]["get"]
    assert stream_operation["operationId"] == "qa.messages.events"
    assert "text/event-stream" in stream_operation["responses"]["200"]["content"]
    assert any(
        parameter["name"] == "Idempotency-Key"
        and parameter["in"] == "header"
        and parameter["required"] is True
        for parameter in recovery_operation["parameters"]
    )
