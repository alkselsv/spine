from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-5-identity-authorization.md"
RUNBOOK = PROJECT_ROOT / "docs/runbooks/oidc-provider-qualification.md"


def test_issue_5_traceability_maps_every_acceptance_criterion_to_executable_evidence() -> None:
    content = TRACEABILITY.read_text(encoding="utf-8")

    criteria = [int(value) for value in re.findall(r"^\| AC(\d+) \|", content, re.MULTILINE)]
    evidence_rows = [
        line for line in content.splitlines() if re.match(r"^\| AC\d+ \|", line)
    ]

    assert criteria == list(range(1, 31))
    assert all("`tests/" in row and "::test_" in row for row in evidence_rows)
    assert "Status: implemented" in content
    assert "Live-provider qualification: not run" in content


def test_live_oidc_runbook_declares_opt_in_command_and_exact_prerequisites() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert "uv run pytest -q -m live_oidc" in content
    for prerequisite in (
        "SPINE_RUN_LIVE_OIDC",
        "SPINE_LIVE_OIDC_CONFIGURATION_VERSION",
        "SPINE_LIVE_OIDC_ISSUER",
        "SPINE_LIVE_OIDC_AUDIENCE",
        "SPINE_LIVE_OIDC_CLIENT_ID",
        "SPINE_LIVE_OIDC_ACCESS_TOKEN",
        "SPINE_LIVE_OIDC_CLIENT_CREDENTIALS_DISABLED_EVIDENCE",
    ):
        assert prerequisite in content
    assert "RFC 9068" in content
    assert "key rotation" in content.lower()
    assert "client-credentials" in content
    assert "not part of the default offline suite" in content
