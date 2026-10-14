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
    assert "/api/v1/ingestion/runs/{run_id}" in document["paths"]
