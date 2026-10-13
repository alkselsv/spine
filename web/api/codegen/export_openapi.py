"""Export the server-owned FastAPI OpenAPI document deterministically."""

from __future__ import annotations

import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from spine.api.main import create_app  # noqa: E402


def main() -> None:
    output = (
        Path(sys.argv[1])
        if len(sys.argv) == 2
        else REPOSITORY_ROOT / "web" / "api" / "openapi.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(
        create_app().openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    output.write_text(f"{document}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
