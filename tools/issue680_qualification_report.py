"""Write a closed, sanitized operator report for issue #680."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import cast, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

QualificationStatus = Literal["PASS", "FAIL", "NOT_VERIFIED"]


class ScenarioResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    preflight: QualificationStatus = "NOT_VERIFIED"
    containment: QualificationStatus = "NOT_VERIFIED"
    streaming_limits: QualificationStatus = "NOT_VERIFIED"
    path_scoped_rename: QualificationStatus = "NOT_VERIFIED"
    unstable_read: QualificationStatus = "NOT_VERIFIED"
    mount_interruption: QualificationStatus = "NOT_VERIFIED"
    recovery_reconciliation: QualificationStatus = "NOT_VERIFIED"
    identity: QualificationStatus = "NOT_VERIFIED"
    cleanup: QualificationStatus = "NOT_VERIFIED"


class ReaderIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    logical_name: Literal["spine-source-reader"] = "spine-source-reader"
    uid: int
    gid: int


class SafetyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mount_operation_performed_by_spine: Literal[False] = False
    source_write_operation_performed_by_spine: Literal[False] = False
    source_content_recorded: Literal[False] = False
    source_names_recorded: Literal[False] = False
    share_address_recorded: Literal[False] = False
    credentials_recorded: Literal[False] = False
    raw_errors_recorded: Literal[False] = False


class MountedFilesystemOperatorReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["issue-680-mounted-filesystem-operator:v1"] = (
        "issue-680-mounted-filesystem-operator:v1"
    )
    ticket: Literal[680] = 680
    status: Literal["FAIL", "NOT_VERIFIED"]
    reason_code: Literal[
        "prerequisite_missing",
        "operator_failure",
        "independent_evidence_review_required",
    ]
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    connection_version_id: UUID | None = None
    backend: str | None = None
    mount_option_fingerprint: str | None = None
    reader_identity: ReaderIdentity | None = None
    scenarios: ScenarioResults
    identity_conclusion: Literal["path_scoped"] = "path_scoped"
    backend_file_id_claim: Literal["NOT_CLAIMED"] = "NOT_CLAIMED"
    safety: SafetyEvidence = SafetyEvidence()


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    state: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        state[key] = value
    return state


def _status(state: dict[str, str], key: str) -> QualificationStatus:
    value = state.get(key, "NOT_VERIFIED")
    if value not in {"PASS", "FAIL", "NOT_VERIFIED"}:
        return "NOT_VERIFIED"
    return cast(QualificationStatus, value)


def _uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def build_report(
    state: dict[str, str],
    requested_reason: Literal[
        "prerequisite_missing", "independent_evidence_review_required"
    ],
) -> MountedFilesystemOperatorReport:
    scenarios = ScenarioResults(
        preflight=_status(state, "PREFLIGHT_STATUS"),
        containment=_status(state, "CONTAINMENT_STATUS"),
        streaming_limits=_status(state, "LIMITS_STATUS"),
        path_scoped_rename=_status(state, "RENAME_STATUS"),
        unstable_read=_status(state, "UNSTABLE_STATUS"),
        mount_interruption=_status(state, "OUTAGE_STATUS"),
        recovery_reconciliation=_status(state, "RECOVERY_STATUS"),
        identity=_status(state, "IDENTITY_STATUS"),
        cleanup=_status(state, "CLEANUP_STATUS"),
    )
    values = tuple(scenarios.model_dump().values())
    failed = "FAIL" in values
    reader_identity = None
    if state.get("READER_UID", "").isdigit() and state.get("READER_GID", "").isdigit():
        reader_identity = ReaderIdentity(
            uid=int(state["READER_UID"]), gid=int(state["READER_GID"])
        )
    return MountedFilesystemOperatorReport(
        status="FAIL" if failed else "NOT_VERIFIED",
        reason_code="operator_failure" if failed else requested_reason,
        workspace_id=_uuid_or_none(state.get("WORKSPACE_ID")),
        environment_id=_uuid_or_none(state.get("ENVIRONMENT_ID")),
        connection_version_id=_uuid_or_none(state.get("CONNECTION_VERSION_ID")),
        backend=state.get("FILESYSTEM_TYPE") or None,
        mount_option_fingerprint=state.get("MOUNT_OPTION_FINGERPRINT") or None,
        reader_identity=reader_identity,
        scenarios=scenarios,
    )


def write_report(path: Path, report: MountedFilesystemOperatorReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        json.dump(report.model_dump(mode="json"), output, indent=2, sort_keys=True)
        output.write("\n")
        temporary = Path(output.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--reason",
        choices=("prerequisite_missing", "independent_evidence_review_required"),
        required=True,
    )
    args = parser.parse_args(argv)
    report = build_report(_load_state(args.state), args.reason)
    write_report(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
