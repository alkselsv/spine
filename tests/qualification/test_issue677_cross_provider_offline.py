"""Executable cross-provider offline qualification for issue #677."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from spine.domain.connections.provider import ProviderCapabilityKind, ProviderKind
from spine.memory.contracts import ContextOutcome
from tests.qualification.cross_provider_conformance import (
    BINARY_PROVIDER_CASES,
    BinaryProviderCase,
    ingest_case,
    ingest_pre_read_observation,
    representation_observations,
    retrieve_canonical_revision,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-677-cross-provider-offline.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-cross-provider-offline-qualification.md"


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue677_report_defines_the_reusable_provider_scenario_matrix() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-677-cross-provider-offline:v1"
    assert report["ticket"] == 677
    assert report["qualification_boundary"] == (
        "provider-admission-canonical-outbox-authorized-retrieval"
    )
    assert set(report["providers"]) == {
        "google_compatibility",
        "yandex_disk",
        "bitrix24",
        "mounted_filesystem",
    }
    assert set(report["required_scenarios"]) == {
        "common_envelope_and_provider_provenance",
        "authorized_canonical_retrieval",
        "workspace_isolation_and_pre_provider_denial",
        "replay_duplicates_pagination_and_partial_failure",
        "durable_cursor_or_scan_commit",
        "rename_move_identity",
        "deletion_and_access_loss",
        "reappearance",
        "revocation",
        "outage",
        "large_files",
        "google_regression_qualification",
    }
    for provider in report["providers"].values():
        assert provider["offline_result"] == "PASS"
        assert provider["live_result"] == "NOT_VERIFIED"
        assert provider["live_prerequisites"]
        assert provider["capabilities"]
        assert set(provider["capabilities"].values()) <= {
            "supported",
            "unsupported",
            "unqualified",
        }
        assert set(provider["scenarios"]) == set(report["required_scenarios"])
        assert set(provider["scenarios"].values()) <= {
            "PASS",
            "NOT_APPLICABLE",
        }


def test_issue677_matrix_points_to_existing_executable_evidence() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    for provider in report["providers"].values():
        assert set(provider["evidence"]) == set(report["required_scenarios"])
        for scenario, result in provider["scenarios"].items():
            node_ids = provider["evidence"][scenario]
            if result == "NOT_APPLICABLE":
                assert node_ids == []
                continue
            assert node_ids
            for node_id in node_ids:
                relative_path, test_name = node_id.split("::", 1)
                assert test_name in _test_names(ROOT / relative_path), node_id


def test_issue677_runbook_executes_every_evidence_suite_and_google_regression() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = re.search(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)

    assert command is not None
    evidence_paths = {
        node_id.split("::", 1)[0]
        for provider in report["providers"].values()
        for node_ids in provider["evidence"].values()
        for node_id in node_ids
    }
    for path in evidence_paths:
        assert path in command.group(1), path
    assert "tests/qualification/test_issue614_google_offline_qualification.py" in (
        command.group(1)
    )
    assert "uv run pytest -q" in command.group(1)


def test_issue677_report_records_offline_gates() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["status"] == "offline_pass"
    assert report["verification"]["focused_result"] == (
        "299 passed, 0 skipped, 0 failed"
    )
    assert report["verification"]["repository_result"] == (
        "5003 passed, 7 skipped, 0 failed"
    )
    assert report["verification"]["compileall_result"] == "passed"


def test_issue677_report_records_only_synthetic_privacy_safe_inputs() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["privacy"] == {
        "synthetic_fakes_only": True,
        "credentials_recorded": False,
        "signed_urls_recorded": False,
        "provider_payloads_recorded": False,
        "customer_content_recorded": False,
        "raw_provider_errors_recorded": False,
    }


def test_issue677_report_does_not_claim_live_provider_verification() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert all(
        provider["live_result"] == "NOT_VERIFIED"
        for provider in report["providers"].values()
    )


def test_issue677_report_contains_no_common_secret_markers() -> None:
    lowered = REPORT_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("access_token", "bearer ", "signed-secret", "refresh_token"):
        assert forbidden not in lowered


@pytest.mark.asyncio
async def test_typed_representations_keep_one_common_canonical_retrieval_contract(
    tmp_path: Path,
) -> None:
    observations = await representation_observations(tmp_path)

    assert [item.representation.kind for item in observations] == [
        "binary",
        "tabular",
        "structured",
    ]
    assert [
        next(
            capability
            for capability in item.required_capabilities()
            if capability
            in {
                ProviderCapabilityKind.BINARY_READ,
                ProviderCapabilityKind.TABULAR_READ,
                ProviderCapabilityKind.STRUCTURED_READ,
            }
        )
        for item in observations
    ] == [
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.TABULAR_READ,
        ProviderCapabilityKind.STRUCTURED_READ,
    ]

    for observation in observations:
        result, outbox_intent_count = await ingest_pre_read_observation(observation)

        assert result.revision.original_sha256 == (
            __import__("hashlib").sha256(observation.original_bytes).hexdigest()
        )
        assert result.source.external_object_id == (
            observation.external_identity.external_object_id
        )
        assert outbox_intent_count == 1
        response, retrieval = await retrieve_canonical_revision(
            revision=result.revision,
            provenance=result.provenance,
            processing_receipt=result.processing_receipt,
        )
        assert response.outcome is ContextOutcome.ALLOWED
        assert retrieval.evidence[0].source_revision_id == result.revision.revision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", BINARY_PROVIDER_CASES, ids=lambda case: case.name)
async def test_provider_binary_runs_through_real_adapter_and_admission(
    case: BinaryProviderCase, tmp_path: Path
) -> None:
    ingested = await ingest_case(case, tmp_path / case.name)
    first = ingested.first

    assert first.observation.provider_kind is case.provider_kind
    assert ingested.admission_count == 1
    assert first.observation.external_identity.provider_key.provider_object_id == (
        case.provider_object_id
    )
    external = first.revision.revision_metadata.external_file_revision
    if case.provider_kind is ProviderKind.GOOGLE:
        assert external is None
        assert first.observation.external_identity.provider_key.provider_object_id == (
            case.provider_object_id
        )
        assert first.observation.provider_version_after == case.provider_revision
        assert first.provenance.origin_locator_value == case.provider_object_id
    else:
        assert external is not None
        assert external.provider_kind == case.provider_kind.value
        if case.provider_revision:
            assert external.provider_revision == case.provider_revision
        else:
            assert external.provider_revision
        if case.provider_kind is ProviderKind.YANDEX_DISK:
            assert external.provider_resource_id == "1573541:proposal-677"
        elif case.provider_kind is ProviderKind.MOUNTED_FILESYSTEM:
            assert external.provider_resource_id is None
        else:
            assert external.provider_resource_id == case.provider_object_id
        if case.provider_kind is ProviderKind.MOUNTED_FILESYSTEM:
            assert external.resource_name is None
        else:
            assert external.resource_name == case.resource_name
        assert external.resource_path == case.resource_path


@pytest.mark.asyncio
async def test_equivalent_provider_bytes_produce_equivalent_canonical_content(
    tmp_path: Path,
) -> None:
    ingested = [
        await ingest_case(case, tmp_path / case.name) for case in BINARY_PROVIDER_CASES
    ]

    assert {item.first.revision.original_sha256 for item in ingested} == {
        hashlib.sha256(b"%PDF synthetic equivalent proposal material").hexdigest()
    }
    assert {item.first.revision.byte_length for item in ingested} == {
        len(b"%PDF synthetic equivalent proposal material")
    }
    assert {item.first.revision.media_type for item in ingested} == {"application/pdf"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", BINARY_PROVIDER_CASES, ids=lambda case: case.name)
async def test_provider_replay_has_one_canonical_outbox_effect(
    case: BinaryProviderCase, tmp_path: Path
) -> None:
    ingested = await ingest_case(case, tmp_path / case.name)

    assert ingested.first.revision == ingested.replay.revision
    assert ingested.first.processing_receipt == ingested.replay.processing_receipt
    assert ingested.replay.replay is True
    assert ingested.replay.canonical_acceptance is None
    assert ingested.outbox_intent_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", BINARY_PROVIDER_CASES, ids=lambda case: case.name)
async def test_authorized_retrieval_preserves_exact_ingested_lineage(
    case: BinaryProviderCase, tmp_path: Path
) -> None:
    ingested = await ingest_case(case, tmp_path / case.name)
    first = ingested.first
    response, retrieval = await retrieve_canonical_revision(
        revision=first.revision,
        provenance=first.provenance,
        processing_receipt=first.processing_receipt,
    )

    assert response.outcome is ContextOutcome.ALLOWED
    assert response.context_bundle_ref is not None
    assert retrieval.evidence[0].source_object_id == first.source.source_object_id
    assert retrieval.evidence[0].source_revision_id == first.revision.revision_id
    assert retrieval.evidence[0].revision_digest == first.revision.revision_digest
    assert (
        retrieval.evidence[0].processing_receipt_id
        == first.processing_receipt.processing_receipt_id
    )
    assert retrieval.evidence[0].locator_contract_version == (
        first.provenance.origin_locator_schema
        or first.processing_receipt.locator_contract_version
    )
