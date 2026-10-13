from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from spine.api.main import create_app


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

    assert json.loads(output.read_text(encoding="utf-8")) == create_app().openapi()
