from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    PolicyActivationDraft,
    access_policy_document_hash,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceOutboxPayload,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import (
    CanonicalAuthorityConflictError,
    CanonicalEligibilityError,
    OptimisticConflictError,
)
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.application.persistence.repositories import SourceObservationCommand
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    AdmissionDecision,
    AdmissionDisposition,
    EligibilityDisposition,
    InitialPolicyEligibilityDecision,
    PolicyBenefitClassification,
    PolicyOriginKind,
    PolicyProposal,
    PrincipalKind,
    SourceRevisionReceiptAssociation,
)

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid
from .outbox_events import WorkspaceCreatedPayload
from .test_source_observation_contract import (
    NOW,
    prepare,
    provenance,
    revision,
    source,
)


def _receipt_association(
    source_object: object,
    source_revision: object,
    source_provenance: object,
    *,
    receipt_id: UUID,
    number: int,
) -> SourceRevisionReceiptAssociation:
    reference = source_revision.original_reference
    assert reference is not None
    return SourceRevisionReceiptAssociation(
        association_id=synthetic_uuid(number + 15100),
        source_object_id=source_object.source_object_id,
        revision_id=source_revision.revision_id,
        provenance_id=source_provenance.provenance_id,
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=receipt_id,
        original_command_result_id=synthetic_uuid(number + 15101),
        original_command_result_schema_version=1,
        original_upload_id=synthetic_uuid(number + 15102),
        original_reference=reference,
        original_sha256=source_revision.original_sha256,
        byte_length=source_revision.byte_length,
        verification_status="verified",
        storage_status="finalized_verified",
        finalized_at=NOW,
        associated_at=NOW,
    )


@dataclass(frozen=True)
class CanonicalCase:
    context: object
    source: object
    revision: object
    provenance: object
    admission_id: UUID
    policy_id: UUID
    command: CanonicalAcceptanceCommand


def _audit(*, context: object, source_revision_id: UUID, event_id: UUID, reason: str):
    return AuditEventRegistry.with_default_families().build_event(
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition="source.accepted"),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=NOW,
        outcome=AuditOutcome.COMMITTED,
        reason=reason,
        target=AuditObjectReference(
            object_type="source_revision",
            object_id=source_revision_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"canonical:{event_id}",
        audit_event_id=event_id,
    )


def _outbox(
    adapter: PersistenceAdapter,
    *,
    context: object,
    source_object_id: UUID,
    event_id: UUID,
):
    return adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type="workspace.created",
        schema_version=1,
        payload=WorkspaceCreatedPayload(
            workspace=OpaqueObjectReference(
                object_type="source_object",
                object_id=source_object_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        trace_id=context.trace_id,
    )


def _canonical_outbox(
    adapter: PersistenceAdapter,
    *,
    context: object,
    source_object_id: UUID,
    source_revision_id: UUID,
    acceptance_id: UUID,
    generation: int,
    audit_event_id: UUID,
    event_id: UUID,
    transition: str = "source.accepted",
):
    return adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload=CanonicalAcceptanceOutboxPayload(
            canonical_acceptance_id=acceptance_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            accepted_generation=generation,
            audit_event_id=audit_event_id,
            transition=transition,
        ),
        trace_id=context.trace_id,
        aggregate=OpaqueObjectReference(
            object_type="source_object",
            object_id=source_object_id,
            schema_version=1,
        ),
    )


async def _original_receipt(
    adapter: PersistenceAdapter, context: object, number: int
) -> UUID:
    async with adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey(f"canonical-original-{number}"),
            digest=digest_command(
                operation=context.operation,
                operation_schema_version=1,
                payload={"canonical_original": number},
            ),
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=synthetic_uuid(number + 15000),
                schema_version=1,
            ),
        )
        await uow.commit()
    return claim.receipt_id


async def _activate_policy(
    adapter: PersistenceAdapter,
    *,
    context: object,
    source_object: object,
    number: int,
    expected_policy_id: UUID | None = None,
    expected_generation: int = 0,
    expected_cas: int = 0,
) -> UUID:
    policy_id = synthetic_uuid(number + 16000)
    proposal_id = synthetic_uuid(number + 16001)
    initial = expected_generation == 0
    policy = AccessPolicyVersion(
        access_policy_version_id=policy_id,
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        source_object_id=source_object.source_object_id,
        version=expected_generation + 1,
        predecessor_policy_version_id=expected_policy_id,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=(
            PolicyOriginKind.ELIGIBLE_TEMPLATE
            if initial
            else PolicyOriginKind.APPROVED_PROPOSAL
        ),
        origin_reference=proposal_id,
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=synthetic_uuid(number + 16002),
        created_at=NOW,
        grants=(
            AccessGrant(
                access_grant_id=synthetic_uuid(number + 16003),
                principal_kind=PrincipalKind.SERVICE,
                principal_id=context.service_principal_id,
                permission=AccessPermission.PROCESS_CONTENT,
                purpose="contract_test",
                operation="canonical_acceptance",
            ),
        ) if initial else (),
    )
    policy = policy.model_copy(
        update={"document_hash": access_policy_document_hash(policy)}
    )
    activation_id = synthetic_uuid(number + 16004)
    audit_id = synthetic_uuid(number + 16005)
    outbox_id = synthetic_uuid(number + 16006)
    audit = AuditEventRegistry.with_default_families().build_event(
        audit_event_id=audit_id,
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="access_policy.activate"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=NOW,
        outcome=AuditOutcome.ACCEPTED,
        reason="policy_activated",
    )
    outbox = _outbox(
        adapter,
        context=context,
        source_object_id=source_object.source_object_id,
        event_id=outbox_id,
    )
    eligibility = (
        InitialPolicyEligibilityDecision(
            decision_id=synthetic_uuid(number + 16007),
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            source_object_id=source_object.source_object_id,
            policy_version_id=policy.access_policy_version_id,
            policy_hash=policy.document_hash,
            origin_kind=policy.origin_kind,
            origin_reference=policy.origin_reference,
            origin_version=policy.origin_version,
            origin_hash=policy.origin_hash,
            membership_generation=1,
            authority_generation=1,
            service_authority_generation=1,
            disposition=EligibilityDisposition.ELIGIBLE,
            reason_code="eligible_template",
            evaluator_contract_version="initial-policy.v1",
            authority_snapshot_reference=synthetic_uuid(number + 16008),
            audit_event_id=synthetic_uuid(number + 16009),
            decided_at=NOW,
        )
        if initial
        else None
    )
    proposal = (
        None
        if initial
        else PolicyProposal(
            policy_proposal_id=proposal_id,
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            source_object_id=source_object.source_object_id,
            proposal_version=1,
            proposed_policy_version_id=policy.access_policy_version_id,
            proposed_policy_hash=policy.document_hash,
            requester_canonical_human_id=synthetic_uuid(number + 16002),
            expected_policy_version_id=expected_policy_id,
            expected_policy_generation=expected_generation,
            benefit_classification=PolicyBenefitClassification.REVOCATION,
            benefit_schema_version="policy-benefit.v1",
            membership_generation=1,
            authority_generation=1,
            service_authority_generation=1,
            submitted_at=NOW,
        )
    )
    command = AccessPolicyActivationCommand(
        policy=policy,
        requester_canonical_human_id=synthetic_uuid(number + 16002),
        activator_canonical_human_id=synthetic_uuid(number + 16002),
        expected_policy_version_id=expected_policy_id,
        expected_policy_generation=expected_generation,
        expected_cas_version=expected_cas,
        activation_id=activation_id,
        idempotency_key=IdempotencyKey(f"canonical-policy-{number}"),
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        activated_at=NOW,
        audit_event=audit,
        outbox_intent=outbox,
        proposal=proposal,
        eligibility=eligibility,
    )
    draft = PolicyActivationDraft(
        values={
            "policy_activation_id": activation_id,
            "workspace_id": source_object.workspace_id,
            "environment_id": source_object.environment_id,
            "source_object_id": source_object.source_object_id,
            "predecessor_policy_version_id": expected_policy_id,
            "new_policy_version_id": policy_id,
            "policy_generation": expected_generation + 1,
            "policy_proposal_id": (
                proposal.policy_proposal_id if proposal is not None else None
            ),
            "policy_proposal_version": (
                proposal.proposal_version if proposal is not None else None
            ),
            "policy_proposal_hash": (
                proposal.proposed_policy_hash if proposal is not None else None
            ),
            "policy_approval_id": None,
            "policy_approval_decision": None,
            "initial_eligibility_decision_id": (
                eligibility.decision_id if eligibility is not None else None
            ),
            "requester_canonical_human_id": command.requester_canonical_human_id,
            "activator_canonical_human_id": command.activator_canonical_human_id,
            "membership_generation": 1,
            "authority_generation": 1,
            "service_authority_generation": 1,
            "audit_event_id": audit_id,
            "outbox_event_id": outbox_id,
            "activated_at": NOW,
        }
    )
    async with adapter.uow_factory(context) as uow:
        await uow.access_policies.activate(command, draft)
        await uow.commit()
    return policy_id


async def prepare_case(
    adapter: PersistenceAdapter,
    number: int,
    *,
    include_association: bool = True,
    order_scheme: str | None = None,
    order_token: str | None = None,
) -> CanonicalCase:
    assert adapter.source_context is not None
    assert adapter.ingestion_context is not None
    workspace_id = synthetic_uuid(number)
    environment_id = synthetic_uuid(number + 1)
    await prepare(adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    source_revision = revision(source_object, synthetic_uuid(number + 2))
    source_provenance = provenance(
        source_object, source_revision.revision_id, "event-1"
    )
    if order_scheme is not None or order_token is not None:
        source_provenance = source_provenance.model_copy(
            update={"order_scheme": order_scheme, "order_token": order_token}
        )
    source_context = adapter.source_context(workspace_id, environment_id)
    association = None
    if include_association:
        receipt_id = await _original_receipt(adapter, source_context, number)
        association = _receipt_association(
            source_object,
            source_revision,
            source_provenance,
            receipt_id=receipt_id,
            number=number,
        )
    observation = SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_provenance,
        idempotency_key=IdempotencyKey(f"canonical-observation-{number}"),
        receipt_association=association,
    )
    async with adapter.uow_factory(source_context) as uow:
        await uow.sources.record_observation(observation)
        await uow.commit()

    policy_context = adapter.ingestion_context(
        workspace_id, environment_id, "access_policy_activation"
    )
    policy_id = await _activate_policy(
        adapter,
        context=policy_context,
        source_object=source_object,
        number=number,
    )
    canonical_context = adapter.ingestion_context(
        workspace_id, environment_id, "canonical_acceptance"
    )
    admission_id = synthetic_uuid(number + 17000)
    async with adapter.uow_factory(canonical_context) as uow:
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=admission_id,
                source_object_id=source_object.source_object_id,
                revision_id=source_revision.revision_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                decision_version=1,
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="admission:r1",
                reason_schema_version="reason.v1",
                reason_codes=("clean",),
                authority_reference={
                    "reference_type": "validator",
                    "reference_id": synthetic_uuid(number + 17001),
                    "schema_version": 1,
                },
                trace_id=canonical_context.trace_id,
                audit_event_id=synthetic_uuid(number + 17002),
                decided_at=NOW,
            )
        )
        await uow.commit()
    audit_id = synthetic_uuid(number + 18000)
    outbox_id = synthetic_uuid(number + 18001)
    command = CanonicalAcceptanceCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_provenance,
        expected_current_revision_id=None,
        idempotency_key=IdempotencyKey(f"canonical-acceptance-{number}"),
        canonical_acceptance_id=synthetic_uuid(number + 18002),
        admission_decision_id=admission_id,
        access_policy_version_id=policy_id,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=_audit(
            context=canonical_context,
            source_revision_id=source_revision.revision_id,
            event_id=audit_id,
            reason="source_accepted",
        ),
        outbox_intent=_canonical_outbox(
            adapter,
            context=canonical_context,
            source_object_id=source_object.source_object_id,
            source_revision_id=source_revision.revision_id,
            acceptance_id=synthetic_uuid(number + 18002),
            generation=1,
            audit_event_id=audit_id,
            event_id=outbox_id,
        ),
    )
    return CanonicalCase(
        canonical_context,
        source_object,
        source_revision,
        source_provenance,
        admission_id,
        policy_id,
        command,
    )


@pytest.mark.asyncio
async def test_canonical_happy_path_and_exact_replay(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19000)
    async with persistence_adapter.uow_factory(case.context) as uow:
        first = await uow.canonical.accept(case.command)
        await uow.commit()
    async with persistence_adapter.uow_factory(case.context) as uow:
        replay = await uow.canonical.accept(case.command)
        await uow.rollback()
    assert first.advanced is True
    assert replay.replay is True
    assert replay.result_reference == first.result_reference


@pytest.mark.asyncio
async def test_canonical_rejects_missing_finalized_receipt_association(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19100, include_association=False)
    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await uow.canonical.accept(case.command)


@pytest.mark.asyncio
async def test_canonical_rejects_unknown_ordering_scheme(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(
        persistence_adapter,
        19900,
        order_scheme="unknown.v1",
        order_token="v1",
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(OptimisticConflictError):
            await uow.canonical.accept(case.command)


@pytest.mark.asyncio
async def test_canonical_rejects_forged_authoritative_snapshot(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19200)
    forged_revision = case.revision.model_copy(
        update={"media_type": "application/forged"}
    )
    forged = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=forged_revision,
        provenance=case.provenance,
        expected_current_revision_id=None,
        idempotency_key=IdempotencyKey("canonical-forged-authority"),
        canonical_acceptance_id=synthetic_uuid(19210),
        admission_decision_id=case.admission_id,
        access_policy_version_id=case.policy_id,
        audit_event_id=synthetic_uuid(19211),
        outbox_event_id=synthetic_uuid(19212),
        accepted_at=NOW,
        audit_event=case.command.audit_event,
        outbox_intent=case.command.outbox_intent,
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(CanonicalAuthorityConflictError):
            await uow.canonical.accept(forged)


@pytest.mark.asyncio
async def test_canonical_replay_requires_current_policy_authority(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19600)
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.canonical.accept(case.command)
        await uow.commit()
    assert persistence_adapter.ingestion_context is not None
    policy_context = persistence_adapter.ingestion_context(
        case.source.workspace_id,
        case.source.environment_id,
        "access_policy_activation",
    )
    await _activate_policy(
        persistence_adapter,
        context=policy_context,
        source_object=case.source,
        number=19620,
        expected_policy_id=case.policy_id,
        expected_generation=1,
        expected_cas=1,
    )

    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await uow.canonical.accept(case.command)


@pytest.mark.asyncio
async def test_canonical_rejects_stale_expected_current_without_success_effect(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19700)
    async with persistence_adapter.uow_factory(case.context) as uow:
        first = await uow.canonical.accept(case.command)
        await uow.commit()
    audit_id = synthetic_uuid(19720)
    outbox_id = synthetic_uuid(19721)
    stale = CanonicalAcceptanceCommand.create(
        source=case.source,
        revision=case.revision,
        provenance=case.provenance,
        expected_current_revision_id=synthetic_uuid(19722),
        idempotency_key=IdempotencyKey("canonical-stale-cas"),
        canonical_acceptance_id=synthetic_uuid(19723),
        admission_decision_id=case.admission_id,
        access_policy_version_id=case.policy_id,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=_audit(
            context=case.context,
            source_revision_id=case.revision.revision_id,
            event_id=audit_id,
            reason="source_accepted",
        ),
        outbox_intent=_canonical_outbox(
            persistence_adapter,
            context=case.context,
            source_object_id=case.source.source_object_id,
            source_revision_id=case.revision.revision_id,
            acceptance_id=synthetic_uuid(19723),
            generation=2,
            audit_event_id=audit_id,
            event_id=outbox_id,
        ),
    )
    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(OptimisticConflictError):
            await uow.canonical.accept(stale)
    async with persistence_adapter.uow_factory(case.context) as uow:
        current = await uow.canonical.resolve_current(case.source.source_object_id)
        await uow.rollback()
    assert current == first.current


@pytest.mark.asyncio
async def test_canonical_rejects_superseded_admission(
    persistence_adapter: PersistenceAdapter,
) -> None:
    case = await prepare_case(persistence_adapter, 19800)
    async with persistence_adapter.uow_factory(case.context) as uow:
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=synthetic_uuid(19820),
                source_object_id=case.source.source_object_id,
                revision_id=case.revision.revision_id,
                workspace_id=case.source.workspace_id,
                environment_id=case.source.environment_id,
                decision_version=2,
                predecessor_decision_id=case.admission_id,
                disposition=AdmissionDisposition.REJECTED,
                validator_profile_version="admission:r1",
                reason_schema_version="reason.v1",
                reason_codes=("revoked",),
                authority_reference={
                    "reference_type": "validator",
                    "reference_id": synthetic_uuid(19821),
                    "schema_version": 1,
                },
                trace_id=case.context.trace_id,
                audit_event_id=synthetic_uuid(19822),
                decided_at=NOW,
            )
        )
        await uow.commit()
    async with persistence_adapter.uow_factory(case.context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await uow.canonical.accept(case.command)
