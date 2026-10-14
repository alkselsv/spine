from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.projections import (
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionOperation,
    ProjectionRun,
    projection_evidence_digest,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
ENVIRONMENT = UUID("00000000-0000-0000-0000-000000000002")


def digest(schema: str, payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def ontology_payload() -> dict[str, object]:
    return {
        "schema_version": "ontology-definition:r1",
        "entity_types": [
            {"name": "document"},
            {"name": "person"},
        ],
        "relation_types": [
            {
                "name": "mentions",
                "source_entity_type": "document",
                "target_entity_type": "person",
            }
        ],
    }


def ontology() -> OntologyVersion:
    payload = ontology_payload()
    return OntologyVersion(
        ontology_version_id=UUID("00000000-0000-0000-0000-000000000010"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        ontology_name="qa",
        version=1,
        definition_payload=payload,
        definition_digest=digest("ontology-definition:r1", payload),
        publication_eligible=True,
        publication_decision_reference="decision-1",
        publisher_authority_reference="authority-1",
        audit_event_reference="audit-1",
        published_at=NOW,
    )


def test_ontology_definition_is_immutable_and_digest_addressed() -> None:
    value = ontology()

    assert value.definition_schema_version == "ontology-definition:r1"
    assert value.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        value.definition_digest = "0" * 64  # type: ignore[misc]


def test_ontology_rejects_unregistered_definition_schema() -> None:
    payload = ontology_payload()
    payload["schema_version"] = "ontology-definition:unknown"

    with pytest.raises(ValidationError, match="schema is not registered"):
        OntologyVersion(
            ontology_version_id=UUID("00000000-0000-0000-0000-000000000010"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            ontology_name="qa",
            version=1,
            definition_payload=payload,
            definition_digest=digest("ontology-definition:r1", payload),
            publication_eligible=True,
            publication_decision_reference="decision-1",
            publisher_authority_reference="authority-1",
            audit_event_reference="audit-1",
            published_at=NOW,
        )


def test_config_digest_is_distinct_from_ontology_and_run_identity() -> None:
    payload: dict[str, object] = {"extractor": "provider-neutral:v1"}
    config = ProjectionConfigVersion(
        projection_config_version_id=UUID("00000000-0000-0000-0000-000000000020"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID("00000000-0000-0000-0000-000000000010"),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=payload,
        config_digest=digest("projection-config:r1", payload),
        created_at=NOW,
    )
    run = ProjectionRun(
        projection_run_id=UUID("00000000-0000-0000-0000-000000000030"),
        projection_candidate_id=UUID("00000000-0000-0000-0000-000000000031"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        operation=ProjectionOperation.BUILD,
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=UUID("00000000-0000-0000-0000-000000000040"),
        command_identity="run-1",
        command_digest="1" * 64,
        idempotency_receipt_id=UUID("00000000-0000-0000-0000-000000000050"),
        started_at=NOW,
    )

    assert config.config_digest != ontology().definition_digest
    assert run.candidate_state is ProjectionCandidateState.OPEN
    assert run.candidate_id == run.projection_candidate_id


def test_evidence_digest_changes_when_candidate_evidence_changes() -> None:
    assert projection_evidence_digest(partitions=(), receipts=(), references=()) != "0" * 64
