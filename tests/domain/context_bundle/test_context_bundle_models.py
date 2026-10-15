from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceReference,
    ContextBundleRef,
    ContextBundleOutcome,
    PolicyDecisionGeneration,
)
from spine.domain.context_profiles import ContextProfileRef
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionSnapshot,
    ProjectionSnapshotState,
)
from spine.domain.sources import CanonicalBoundary


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
PROFILE = UUID(int=3)
SNAPSHOT = UUID(int=4)
ACTIVATION = UUID(int=5)
BOUNDARY = UUID(int=6)
CONFIG = UUID(int=7)
RUN = UUID(int=8)
ATTEMPT = UUID(int=9)


def _config() -> ProjectionConfigVersion:
    digest = sha256(
        json.dumps(
            {"schema": "projection-config:r1", "payload": {}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=10),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload={},
        config_digest=digest,
        created_at=NOW,
    )


def context_bundle(**overrides: object) -> ContextBundle:
    values: dict[str, object] = {
        "bundle_id": UUID(int=11),
        "bundle_version": 1,
        "bundle_digest": "0" * 64,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "profile_ref": ContextProfileRef(
            profile_id=PROFILE,
            profile_version=1,
            profile_digest="1" * 64,
        ),
        "trusted_request_context_ref": UUID(int=12),
        "authorization_context_ref": UUID(int=13),
        "request_id": UUID(int=14),
        "run_id": RUN,
        "attempt_id": ATTEMPT,
        "idempotency_key": "qa-attempt-1",
        "projection_snapshot": ProjectionSnapshot(
            projection_snapshot_id=SNAPSHOT,
            projection_run_id=UUID(int=15),
            projection_candidate_id=UUID(int=16),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            snapshot_state=ProjectionSnapshotState.FINALIZED,
            projection_kind="qa",
            projection_config_version_id=CONFIG,
            canonical_boundary_id=BOUNDARY,
            candidate_evidence_digest="2" * 64,
            manifest_digest="3" * 64,
            member_count=1,
            created_at=NOW,
        ),
        "projection_activation": ProjectionActivation(
            projection_activation_id=ACTIVATION,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_kind="qa",
            projection_snapshot_id=SNAPSHOT,
            activation_generation=1,
            activation_kind=ProjectionActivationKind.PUBLISH,
            command_identity="activation-1",
            command_digest="4" * 64,
            idempotency_receipt_id=UUID(int=17),
            audit_event_id=UUID(int=18),
            activated_at=NOW,
        ),
        "activation_generation": 1,
        "canonical_boundary": CanonicalBoundary(
            boundary_id=BOUNDARY,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_kind="qa",
            scope_version="boundary:v1",
            accepted_through_generation=2,
            boundary_time=NOW,
            manifest_digest="5" * 64,
            idempotency_receipt_id=UUID(int=19),
        ),
        "canonical_as_of": CanonicalAsOf(
            canonical_boundary_id=BOUNDARY,
            accepted_through_generation=2,
            boundary_time=NOW,
        ),
        "projection_config_version": _config(),
        "freshness": ProjectionFreshness.CURRENT,
        "health": ProjectionHealth.HEALTHY,
        "retrieval_strategy": "hybrid",
        "outcome": ContextBundleOutcome.ALLOWED,
        "reason_code": "allowed",
        "evidence_references": (
            ContextBundleEvidenceReference(
                evidence_id="evidence-1",
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                projection_snapshot_id=SNAPSHOT,
                activation_generation=1,
                source_object_id=UUID(int=20),
                source_revision_id=UUID(int=21),
                revision_digest="6" * 64,
                processing_receipt_id=UUID(int=22),
                locator_contract_version="locator:v1",
                locator={"kind": "page", "value": "1"},
                evidence_digest="7" * 64,
            ),
        ),
        "policy_decision_generations": (
            PolicyDecisionGeneration(
                source_object_id=UUID(int=20),
                policy_version_id=UUID(int=23),
                policy_generation=1,
                membership_generation=1,
                authority_generation=1,
                service_authority_generation=1,
                decision_reference=UUID(int=24),
            ),
        ),
        "created_at": NOW,
    }
    values.update(overrides)
    values.pop("bundle_digest", None)
    return ContextBundle.create(**values)


def test_context_bundle_create_is_digest_addressed_and_immutable() -> None:
    bundle = context_bundle()

    assert bundle.bundle_digest == ContextBundle.calculate_digest(bundle)
    with pytest.raises(ValidationError):
        bundle.attempt_id = UUID(int=25)  # type: ignore[misc]


def test_context_bundle_ref_matches_public_v1_identity() -> None:
    bundle = context_bundle()

    assert bundle.reference == ContextBundleRef(
        bundle_id=bundle.bundle_id,
        bundle_version=1,
        bundle_digest=bundle.bundle_digest,
        publication_pin_id=None,
    )


def test_context_bundle_lineage_ref_binds_identity_to_tenant_and_attempt() -> None:
    bundle = context_bundle()

    assert bundle.lineage_reference.bundle == bundle.reference
    assert bundle.lineage_reference.workspace_id == WORKSPACE
    assert bundle.lineage_reference.environment_id == ENVIRONMENT
    assert bundle.lineage_reference.run_id == RUN
    assert bundle.lineage_reference.attempt_id == ATTEMPT


def test_context_bundle_rejects_mixed_workspace_evidence() -> None:
    with pytest.raises(ValidationError, match="tenant"):
        context_bundle(
            evidence_references=(
                ContextBundleEvidenceReference(
                    evidence_id="evidence-foreign",
                    workspace_id=UUID(int=99),
                    environment_id=ENVIRONMENT,
                    projection_snapshot_id=SNAPSHOT,
                    activation_generation=1,
                    source_object_id=UUID(int=20),
                    source_revision_id=UUID(int=21),
                    locator={"kind": "page", "value": "1"},
                    evidence_digest="7" * 64,
                ),
            )
        )


def test_context_bundle_rejects_ambiguous_duplicate_evidence_identifier() -> None:
    first = context_bundle().evidence_references[0]
    duplicate = first.model_copy(
        update={
            "source_object_id": UUID(int=25),
            "source_revision_id": UUID(int=26),
        }
    )
    with pytest.raises(ValidationError, match="evidence identifiers must be unique"):
        context_bundle(
            evidence_references=(first, duplicate),
            policy_decision_generations=(
                *context_bundle().policy_decision_generations,
                PolicyDecisionGeneration(
                    source_object_id=UUID(int=25),
                    policy_version_id=UUID(int=27),
                    policy_generation=1,
                    membership_generation=1,
                    authority_generation=1,
                    decision_reference=UUID(int=28),
                ),
            ),
        )


def test_context_bundle_rejects_mismatched_snapshot_generation() -> None:
    with pytest.raises(ValidationError, match="generation"):
        context_bundle(activation_generation=2)
