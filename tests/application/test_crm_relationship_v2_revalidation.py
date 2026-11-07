from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    Bitrix24CrmRelationshipV2ReadResult,
    CrmRelationshipBindingOutcome,
)
from spine.application.crm_relationship_v2_admission import (
    CrmRelationshipV2AdmissionDenied,
    CrmRelationshipV2DenialReason,
)
from spine.application.crm_relationship_v2_revalidation import (
    CrmRelationshipV2BindingObservation,
    CrmRelationshipV2MappingObservation,
    CrmRelationshipV2RevalidationRequest,
    CrmRelationshipV2RevalidationService,
    CrmRelationshipV2RevalidationTarget,
    materialize_revalidation_observation,
)
from spine.application.crm_relationship_v2_source_match import (
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchRequest,
    CrmRelationshipV2SourceMatchResult,
)
from spine.domain.sources.crm_relationship_v2 import (
    CrmEligibilityOutcome,
    CrmRelationshipClaim,
    CrmRelationshipEligibilityHead,
    CrmRelationshipIdentity,
    CrmRelationshipNaturalIdentity,
    relationship_key,
)
from spine.domain.sources import LifecycleEligibilityStatus
from tests.application.test_crm_relationship_v2_persistence import (
    admitted_read,
    binding_result,
    mapped_disk,
    source_match,
)

NOW = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)


def ident(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"crm-v2-revalidation:{value}")


def target(*, workspace_id: UUID | None = None) -> CrmRelationshipV2RevalidationTarget:
    command = admitted_read()
    selected_workspace_id = workspace_id or command.workspace_id
    natural = CrmRelationshipNaturalIdentity(
        workspace_id=selected_workspace_id,
        environment_id=command.environment_id,
        provider_instance=command.provider_instance,
        connection_id=command.connection_id,
        activity_namespace=command.activity_namespace,
        activity_type_id=command.activity_type_id,
        activity_id=command.activity_id,
        raw_entity_type_id=command.raw_entity_type_id,
        normalizer_id=command.mapping_profile.normalizer_id,
        normalizer_version=command.mapping_profile.normalizer_version,
        entity_namespace=command.entity_namespace,
        entity_id=command.entity_id,
        files_namespace=mapped_disk().files_item.namespace,
        files_item_id=mapped_disk().files_item.value,
        mapping_profile_id=command.mapping_profile.profile_id,
    )
    identity = CrmRelationshipIdentity(
        **natural.model_dump(mode="python"), relationship_key=relationship_key(natural)
    )
    matched = source_match()
    assert matched.source_object_id is not None
    assert matched.source_revision_id is not None
    claim = CrmRelationshipClaim(
        claim_id=ident("claim"),
        relationship_key=identity.relationship_key,
        workspace_id=selected_workspace_id,
        environment_id=command.environment_id,
        connection_id=command.connection_id,
        connection_version_id=command.connection_version_id,
        source_object_id=matched.source_object_id,
        source_revision_id=matched.source_revision_id,
        disk_namespace=mapped_disk().disk_revision.namespace,
        disk_object_id=mapped_disk().disk_revision.object_id,
        disk_version_evidence=mapped_disk().disk_revision.provider_revision,
        mapping_profile_id=command.mapping_profile.profile_id,
        mapping_profile_version=command.mapping_profile.profile_version,
        acting_human_id=command.acting_human_id,
        human_authorization_generation=command.human_authorization_generation,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        idempotency_key=ident("claim-key"),
        payload_digest="c" * 64,
        created_at=NOW,
    )
    head = CrmRelationshipEligibilityHead(
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        claim_id=claim.claim_id,
        verification_generation=1,
        lifecycle_generation=0,
        outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        receipt_id=ident("prior-receipt"),
        eligibility_id=ident("prior-eligibility"),
        updated_at=NOW,
    )
    return CrmRelationshipV2RevalidationTarget(
        identity=identity, claim=claim, eligibility_head=head
    )


def request() -> CrmRelationshipV2RevalidationRequest:
    current = target()
    return CrmRelationshipV2RevalidationRequest(
        receipt_id=ident("receipt"),
        eligibility_id=ident("eligibility"),
        idempotency_key=ident("attempt-key"),
        target=current,
        operations_profile=admitted_read().operations_profile,
        expected_verification_generation=current.eligibility_head.verification_generation,
        expected_lifecycle_generation=current.eligibility_head.lifecycle_generation,
    )


class CurrentTargetFake:
    def __init__(
        self,
        value: CrmRelationshipV2RevalidationTarget,
        *later: CrmRelationshipV2RevalidationTarget | None,
    ) -> None:
        self.values = [value, *later]
        self.calls = 0

    async def resolve_current(self, requested: CrmRelationshipV2RevalidationRequest):
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class BindingFake:
    def __init__(
        self,
        value: Bitrix24CrmRelationshipV2ReadResult | Exception | None = None,
    ) -> None:
        self.value = value or binding_result()
        self.calls = 0

    async def observe(
        self, requested: CrmRelationshipV2RevalidationRequest
    ) -> CrmRelationshipV2BindingObservation:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return CrmRelationshipV2BindingObservation(
            command=admitted_read(), result=self.value
        )


class MappingFake:
    def __init__(
        self, value: CrmRelationshipV2MappingObservation | Exception | None = None
    ) -> None:
        self.value = value or CrmRelationshipV2MappingObservation(
            outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
            evidence=mapped_disk(),
        )
        self.calls = 0

    async def observe_exact(
        self,
        command: Bitrix24CrmRelationshipV2ReadCommand,
        current: CrmRelationshipV2RevalidationTarget,
    ) -> CrmRelationshipV2MappingObservation:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class SourceFake:
    def __init__(
        self, value: CrmRelationshipV2SourceMatchResult | Exception | None = None
    ) -> None:
        self.value = value or source_match()
        self.calls = 0

    async def match(
        self, requested: CrmRelationshipV2SourceMatchRequest
    ) -> CrmRelationshipV2SourceMatchResult:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.mark.asyncio
async def test_current_exact_relationship_produces_bounded_eligible_receipt() -> None:
    current = target()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(current),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    assert result.eligible
    assessment = materialize_revalidation_observation(
        result.observation,
        verification_generation=2,
        lifecycle_generation=0,
    )
    assert assessment.receipt.claim_id == current.claim.claim_id
    assert assessment.receipt.files_item_id == current.identity.files_item_id
    assert assessment.receipt.source_revision_id == current.claim.source_revision_id
    assert assessment.receipt.verification_generation == 2
    assert not assessment.receipt.model_dump().keys() & {
        "access_policy",
        "pricing_basis",
        "source_content",
        "raw_payload",
        "signed_url",
    }
    assert not result.grants_disclosure
    assert not result.grants_pricing_basis


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_outcome", "expected"),
    (
        (
            CrmRelationshipBindingOutcome.NOT_VERIFIED,
            CrmEligibilityOutcome.NOT_VERIFIED,
        ),
        (
            CrmRelationshipBindingOutcome.INACCESSIBLE,
            CrmEligibilityOutcome.INACCESSIBLE,
        ),
        (
            CrmRelationshipBindingOutcome.AUTHORITY_UNAVAILABLE,
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        ),
    ),
)
async def test_binding_uncertainty_preserves_typed_outcome_without_source_use(
    binding_outcome: CrmRelationshipBindingOutcome,
    expected: CrmEligibilityOutcome,
) -> None:
    mapping = MappingFake()
    source = SourceFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(binding_result(binding_outcome)),
        mappings=mapping,
        sources=source,
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is expected
    assert not result.eligible
    assert mapping.calls == 0
    assert source.calls == 0
    assessment = materialize_revalidation_observation(
        result.observation,
        verification_generation=2,
        lifecycle_generation=0,
    )
    assert not assessment.receipt.positive_pair_observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("denial", "expected"),
    (
        (
            CrmRelationshipV2DenialReason.HUMAN_AUTHORITY_UNAVAILABLE,
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        ),
        (
            CrmRelationshipV2DenialReason.GRANT_UNAVAILABLE,
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        ),
        (
            CrmRelationshipV2DenialReason.CONNECTION_UNAVAILABLE,
            CrmEligibilityOutcome.STALE_CONTEXT,
        ),
        (
            CrmRelationshipV2DenialReason.PROFILE_UNAVAILABLE,
            CrmEligibilityOutcome.STALE_CONTEXT,
        ),
        (
            CrmRelationshipV2DenialReason.AUTHORITY_SUPERSEDED,
            CrmEligibilityOutcome.STALE_CONTEXT,
        ),
        (
            CrmRelationshipV2DenialReason.SCOPE_UNAPPROVED,
            CrmEligibilityOutcome.INACCESSIBLE,
        ),
    ),
)
async def test_current_admission_denial_is_sanitized_and_typed(
    denial: CrmRelationshipV2DenialReason,
    expected: CrmEligibilityOutcome,
) -> None:
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(CrmRelationshipV2AdmissionDenied(denial)),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is expected
    dumped = result.observation.model_dump_json()
    assert "access-secret" not in dumped
    assert "refresh-secret" not in dumped
    assert "provider-user-20" not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    (
        CrmEligibilityOutcome.NOT_VERIFIED,
        CrmEligibilityOutcome.INACCESSIBLE,
        CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        CrmEligibilityOutcome.PROVIDER_REVOCATION_UNQUALIFIED,
    ),
)
async def test_files_mapping_uncertainty_does_not_mutate_source_or_claim_provider_revocation(
    outcome: CrmEligibilityOutcome,
) -> None:
    source = SourceFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(CrmRelationshipV2MappingObservation(outcome=outcome)),
        sources=source,
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is outcome
    assert source.calls == 0
    assert (
        result.observation.target.claim.source_revision_id
        == target().claim.source_revision_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    (
        CrmEligibilityOutcome.STALE_CONTEXT,
        CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
        CrmEligibilityOutcome.SOURCE_INELIGIBLE,
        CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
    ),
)
async def test_current_source_outcome_is_preserved(
    outcome: CrmEligibilityOutcome,
) -> None:
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(source_match(outcome)),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is outcome
    assert not result.eligible


@pytest.mark.asyncio
async def test_governance_withdrawal_short_circuits_positive_reads_and_cannot_resurrect() -> (
    None
):
    withdrawn = target()
    withdrawn = withdrawn.model_copy(
        update={
            "eligibility_head": withdrawn.eligibility_head.model_copy(
                update={"outcome": CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN}
            )
        }
    )
    requested = request().model_copy(update={"target": withdrawn})
    binding = BindingFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(withdrawn),
        bindings=binding,
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(requested)

    assert result.outcome is CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
    assert binding.calls == 0


@pytest.mark.asyncio
async def test_late_positive_response_cannot_advance_a_new_verification_generation() -> (
    None
):
    initial = target()
    advanced = initial.model_copy(
        update={
            "eligibility_head": initial.eligibility_head.model_copy(
                update={
                    "verification_generation": 2,
                    "receipt_id": ident("newer-receipt"),
                    "eligibility_id": ident("newer-eligibility"),
                }
            )
        }
    )
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(initial, advanced),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert result.observation.expected_verification_generation == 1


@pytest.mark.asyncio
async def test_withdrawal_during_read_wins_over_positive_observations() -> None:
    initial = target()
    withdrawn = initial.model_copy(
        update={
            "eligibility_head": initial.eligibility_head.model_copy(
                update={
                    "lifecycle_generation": 1,
                    "outcome": CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
                }
            )
        }
    )
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(initial, withdrawn),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
    assert not result.eligible


@pytest.mark.asyncio
async def test_stale_admitted_connection_generation_cannot_qualify_target() -> None:
    stale_command = admitted_read()
    stale_command = replace(
        stale_command,
        authorization_generation=stale_command.authorization_generation + 1,
    )

    class StaleBinding:
        async def observe(self, requested):
            return CrmRelationshipV2BindingObservation(
                command=stale_command, result=binding_result()
            )

    mapping = MappingFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=StaleBinding(),
        mappings=mapping,
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert mapping.calls == 0


@pytest.mark.asyncio
async def test_mapping_for_different_disk_revision_cannot_transfer_claim() -> None:
    different = mapped_disk().model_copy(
        update={
            "disk_revision": mapped_disk().disk_revision.model_copy(
                update={"provider_revision": "18"}
            )
        }
    )
    source = SourceFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(
            CrmRelationshipV2MappingObservation(
                outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
                evidence=different,
            )
        ),
        sources=source,
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert source.calls == 0


@pytest.mark.asyncio
async def test_positive_source_result_for_sibling_revision_cannot_transfer_claim() -> (
    None
):
    sibling = source_match()
    assert sibling.source_revision_id is not None
    sibling = sibling.model_copy(
        update={"source_revision_id": ident("sibling-revision")}
    )
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(sibling),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert not result.eligible


@pytest.mark.asyncio
async def test_positive_binding_for_different_entity_is_not_verified() -> None:
    mismatched = binding_result().model_copy(update={"entity_id": "deal-sibling"})
    mapping = MappingFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(mismatched),
        mappings=mapping,
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.NOT_VERIFIED
    assert mapping.calls == 0


@pytest.mark.asyncio
async def test_mapping_strategy_must_match_current_admitted_profile() -> None:
    command = admitted_read()
    attached_profile = command.mapping_profile.model_copy(
        update={
            "mapping_strategy": "attached_object",
            "ownership_proof_reference": "qualification:attached:v1",
            "read_methods": (
                *command.mapping_profile.read_methods,
                "disk.attachedObject.get",
            ),
        }
    )
    attached_command = replace(command, mapping_profile=attached_profile)

    class AttachedBinding:
        async def observe(self, requested):
            return CrmRelationshipV2BindingObservation(
                command=attached_command, result=binding_result()
            )

    source = SourceFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=AttachedBinding(),
        mappings=MappingFake(),
        sources=source,
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert source.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (
            LifecycleEligibilityStatus.READ_WITHDRAWN,
            CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
        ),
        (
            LifecycleEligibilityStatus.AUTHORITY_UNAVAILABLE,
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        ),
        (
            LifecycleEligibilityStatus.RETENTION_INELIGIBLE,
            CrmEligibilityOutcome.SOURCE_INELIGIBLE,
        ),
    ),
)
async def test_ineligible_lifecycle_authority_preserves_typed_outcome(
    status: LifecycleEligibilityStatus,
    expected: CrmEligibilityOutcome,
) -> None:
    matched = source_match()
    assert matched.authority is not None
    ineligible_lifecycle = matched.authority.lifecycle.model_copy(
        update={"status": status}
    )
    stale_authority = matched.authority.model_copy(
        update={"lifecycle": ineligible_lifecycle}
    )
    matched = matched.model_copy(update={"authority": stale_authority})
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(matched),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is expected
    assert not result.eligible


@pytest.mark.asyncio
async def test_changed_source_authorization_generation_is_stale_context() -> None:
    matched = source_match()
    assert matched.authority is not None
    admission = matched.authority.processing_admission
    changed_admission = admission.model_copy(
        update={"authorization_generation": admission.authorization_generation + 1}
    )
    changed_authority = matched.authority.model_copy(
        update={"processing_admission": changed_admission}
    )
    matched = matched.model_copy(update={"authority": changed_authority})

    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(matched),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert not result.eligible


@pytest.mark.asyncio
async def test_foreign_workspace_target_is_denied_before_provider_use() -> None:
    requested = request()
    foreign_workspace_id = ident("foreign-workspace")
    foreign = target(workspace_id=foreign_workspace_id)
    binding = BindingFake()
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(foreign),
        bindings=binding,
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(requested)

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert binding.calls == 0
    assert str(foreign_workspace_id) not in result.observation.model_dump_json()


@pytest.mark.asyncio
async def test_same_idempotency_key_is_stable_after_transient_read_failure() -> None:
    requested = request()
    first = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(RuntimeError("transient source failure")),
        clock=lambda: NOW,
    ).revalidate(requested)
    second = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(requested)

    assert first.outcome is CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE
    assert second.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    first_retry = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(requested)
    assert first_retry.observation == second.observation
    assert second.observation.idempotency_key == requested.idempotency_key


@pytest.mark.asyncio
async def test_materialization_requires_store_allocated_generation_fences() -> None:
    result = await CrmRelationshipV2RevalidationService(
        targets=CurrentTargetFake(target()),
        bindings=BindingFake(),
        mappings=MappingFake(),
        sources=SourceFake(),
        clock=lambda: NOW,
    ).revalidate(request())
    observation = result.observation

    with pytest.raises(ValueError, match="verification generation"):
        materialize_revalidation_observation(
            observation,
            verification_generation=3,
            lifecycle_generation=0,
        )
    with pytest.raises(ValueError, match="lifecycle generation"):
        materialize_revalidation_observation(
            observation,
            verification_generation=2,
            lifecycle_generation=1,
        )


def test_request_requires_caller_supplied_synthetic_operations_profile() -> None:
    with pytest.raises(ValueError, match="operations_profile"):
        CrmRelationshipV2RevalidationRequest.model_validate(
            {**request().model_dump(mode="python"), "operations_profile": None}
        )
