from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.domain.sources.crm_relationship_v2 import (
    CRM_RELATIONSHIP_V2,
    CrmEligibilityOutcome,
    CrmRelationshipEligibility,
    CrmRelationshipEligibilityHead,
    CrmRelationshipIdentity,
    CrmRelationshipMappingProfile,
    CrmRelationshipObservationReceipt,
    relationship_key,
)


def _identity_values() -> dict[str, object]:
    return {
        "schema_version": CRM_RELATIONSHIP_V2,
        "record_kind": "logical_relationship",
        "workspace_id": UUID(int=1),
        "environment_id": UUID(int=2),
        "provider_instance": "portal-a",
        "connection_id": UUID(int=3),
        "activity_namespace": "bitrix.activity",
        "activity_id": "17",
        "raw_entity_type_id": "2",
        "normalizer_id": "bitrix.enum.ownertype",
        "normalizer_version": "1",
        "entity_namespace": "bitrix.company",
        "entity_id": "42",
        "files_namespace": "bitrix.activity.files",
        "files_item_id": "9",
        "mapping_profile_id": UUID(int=4),
    }


def test_relationship_key_uses_stable_natural_identity() -> None:
    values = _identity_values()
    key = relationship_key(values)

    assert key == relationship_key(dict(reversed(tuple(values.items()))))
    assert len(key) == 64
    assert key == key.lower()


def test_relationship_key_separates_tenant_and_exact_files_item() -> None:
    values = _identity_values()
    assert relationship_key(values) != relationship_key({**values, "workspace_id": UUID(int=11)})
    assert relationship_key(values) != relationship_key({**values, "files_item_id": "10"})


def test_identity_rejects_digest_that_does_not_match_natural_key() -> None:
    with pytest.raises(ValueError, match="relationship_key"):
        CrmRelationshipIdentity(
            **_identity_values(),
            relationship_key="0" * 64,
        )


def test_relationship_key_fails_closed_for_missing_natural_key_field() -> None:
    values = _identity_values()
    del values["normalizer_version"]
    with pytest.raises(KeyError):
        relationship_key(values)


def test_eligibility_head_requires_receipt_for_positive_generation() -> None:
    with pytest.raises(ValueError, match="receipt reference"):
        CrmRelationshipEligibilityHead(
            workspace_id=UUID(int=1),
            environment_id=UUID(int=2),
            claim_id=UUID(int=3),
            verification_generation=1,
            lifecycle_generation=0,
            outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_mapping_profile_requires_ownership_proof_for_attached_objects() -> None:
    with pytest.raises(ValueError, match="ownership proof"):
        CrmRelationshipMappingProfile(
            schema_version="bitrix24-crm-files-mapping-profile:v1",
            record_kind="mapping_profile",
            profile_id=UUID(int=1),
            workspace_id=UUID(int=2),
            environment_id=UUID(int=3),
            profile_version=1,
            provider_instance="portal-test",
            connection_id=UUID(int=4),
            connection_approval_id=UUID(int=5),
            qualification_reference=UUID(int=6),
            approval_reference=UUID(int=7),
            activity_namespace="bitrix.activity",
            raw_entity_type_ids=("2",),
            entity_namespace="bitrix.company",
            files_namespace="bitrix.activity.files",
            normalizer_id="bitrix.enum.ownertype",
            normalizer_version="1",
            read_methods=("crm.activity.binding.list", "disk.attachedObject.get"),
            mapping_strategy="attached_object",
            disk_namespace="bitrix.disk",
        )


def test_positive_receipt_requires_exact_binding_pair_evidence() -> None:
    with pytest.raises(ValueError, match="positive eligibility"):
        CrmRelationshipObservationReceipt(
            receipt_id=UUID(int=1),
            claim_id=UUID(int=2),
            workspace_id=UUID(int=3),
            environment_id=UUID(int=4),
            provider_instance="portal-test",
            connection_id=UUID(int=5),
            connection_version_id=UUID(int=6),
            activity_namespace="bitrix.activity",
            activity_id="17",
            raw_entity_type_id="2",
            normalizer_id="bitrix.enum.ownertype",
            normalizer_version="1",
            entity_namespace="bitrix.company",
            entity_id="42",
            files_namespace="bitrix.activity.files",
            files_item_id="9",
            mapping_profile_id=UUID(int=7),
            mapping_profile_version=1,
            binding_method="crm.activity.binding.list",
            binding_page_count=1,
            binding_evidence_reference="synthetic:binding:page:1",
            binding_evidence_digest="a" * 64,
            positive_pair_observed=False,
            disk_namespace="bitrix.disk",
            disk_object_id="disk-1",
            disk_version_evidence="version-1",
            source_object_id=UUID(int=8),
            source_revision_id=UUID(int=9),
            acting_human_id=UUID(int=10),
            authorization_generation=1,
            configuration_generation=1,
            verification_generation=1,
            outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
            evidence_digest="b" * 64,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            idempotency_key=UUID(int=11),
            payload_digest="c" * 64,
        )


def test_eligibility_contract_pins_its_derived_record_identity() -> None:
    eligibility = CrmRelationshipEligibility(
        eligibility_id=UUID(int=4),
        claim_id=UUID(int=3),
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        verification_generation=1,
        lifecycle_generation=0,
        outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        evaluated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert eligibility.record_kind == "eligibility"
    assert eligibility.eligibility_id == UUID(int=4)
