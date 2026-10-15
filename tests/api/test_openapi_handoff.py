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
    assert "/api/v1/ingestion/runs" in document["paths"]
    assert "/api/v1/ingestion/runs/{run_id}" in document["paths"]
    assert "/api/v1/projections/{projection_kind}/status" in document["paths"]
    assert "/api/v1/projections/{projection_kind}/recovery" in document["paths"]

    status_operation = document["paths"]["/api/v1/projections/{projection_kind}/status"]["get"]
    recovery_operation = document["paths"]["/api/v1/projections/{projection_kind}/recovery"]["post"]
    assert status_operation["operationId"] == "projection.status"
    assert recovery_operation["operationId"] == "projection.recovery"
    assert any(
        parameter["name"] == "Idempotency-Key"
        and parameter["in"] == "header"
        and parameter["required"] is True
        for parameter in recovery_operation["parameters"]
    )
