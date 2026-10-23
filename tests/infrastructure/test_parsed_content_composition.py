from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.content_authorization import (
    AuthorizationDecisionKind,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.domain.audit import AuditEvent, AuditOutcome
from spine.domain.common import ContextOrigin
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyOriginKind,
    PrincipalKind,
    RevisionKind,
)
from spine.infrastructure.parsed_content_composition import (
    ProductionParsedContentAuthorizer,
    UnitOfWorkParsedContentDecisionAudit,
)


NOW = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
SUBJECT_ID = UUID(int=3)
SOURCE_ID = UUID(int=4)
REVISION_ID = UUID(int=5)


def context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_parsed_content"),
        trace_id=UUID(int=6),
        acting_subject_id=SUBJECT_ID,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID(int=7),
            signature=b"trusted",
        ),
    )


def allowed_decision() -> ContentAuthorizationDecision:
    return ContentAuthorizationDecision(
        allowed=True,
        decision_kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_ids=(SOURCE_ID,),
        policy_evidence=(
            AuthorizationEvidence(
                source_object_id=SOURCE_ID,
                source_revision_id=REVISION_ID,
                policy_version_id=UUID(int=8),
                policy_generation=1,
            ),
        ),
        membership_generation=1,
        authority_generation=1,
        authority_snapshot_reference=UUID(int=9),
        purpose="knowledge_control",
        operation="inspect_parsed_content",
        reason_code=AuthorizationReasonCode.ALLOWED,
        decision_reference=UUID(int=10),
        trace_id=UUID(int=6),
        evaluated_at=NOW,
    )


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> UUID:
        self.events.append(event)
        return UUID(int=11)


class AuditUnitOfWork:
    def __init__(self, writer: RecordingAuditWriter) -> None:
        self.audit = writer
        self.committed = False

    async def __aenter__(self) -> AuditUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class CurrentAuthorizationUnitOfWork:
    def __init__(self, *, grant_read: bool = True) -> None:
        policy_id = UUID(int=12)
        self.sources = SimpleNamespace(
            resolve_source=lambda source_id: _value(
                SimpleNamespace(source_object_id=source_id)
            ),
            resolve_revision=lambda revision_id: _value(
                SimpleNamespace(revision_id=revision_id, observed_at=NOW)
            ),
            list_provenance=lambda revision_id: _value((revision_id,)),
        )
        self.canonical = SimpleNamespace(
            resolve_current=lambda source_id: _value(
                SimpleNamespace(
                    current_revision_id=REVISION_ID,
                    current_revision_kind=RevisionKind.CONTENT,
                )
            )
        )
        self.access_policies = SimpleNamespace(
            resolve_current=lambda source_id: _value(
                CurrentAccessPolicy(
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    source_object_id=source_id,
                    access_policy_version_id=policy_id,
                    policy_activation_id=UUID(int=13),
                    policy_generation=1,
                    cas_version=1,
                )
            ),
            resolve_policy=lambda requested_policy_id: _value(
                AccessPolicyVersion(
                    access_policy_version_id=requested_policy_id,
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    source_object_id=SOURCE_ID,
                    version=1,
                    predecessor_policy_version_id=None,
                    document_schema_version="access-policy.v1",
                    document_hash="a" * 64,
                    origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
                    origin_reference=UUID(int=14),
                    origin_version=1,
                    origin_hash="b" * 64,
                    created_by_canonical_human_id=SUBJECT_ID,
                    created_at=NOW,
                    grants=(
                        (
                            AccessGrant(
                                access_grant_id=UUID(int=15),
                                principal_kind=PrincipalKind.HUMAN,
                                principal_id=SUBJECT_ID,
                                permission=AccessPermission.READ_CONTENT,
                            ),
                        )
                        if grant_read
                        else ()
                    ),
                )
            ),
        )

    async def __aenter__(self) -> CurrentAuthorizationUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        return None


async def _value(value: object) -> object:
    return value


class CurrentHumanAuthorityResolver:
    def __init__(self, generation: int | None = 1) -> None:
        self.generation = generation

    async def resolve_human_authority(self, **kwargs: object) -> int | None:
        del kwargs
        return self.generation


@pytest.mark.asyncio
async def test_allowed_object_decision_is_committed_as_immutable_audit() -> None:
    writer = RecordingAuditWriter()
    uow = AuditUnitOfWork(writer)
    adapter = UnitOfWorkParsedContentDecisionAudit(
        uow_factory=lambda trusted: uow,
        audit_events=AuditEventRegistry.with_default_families(),
    )

    audit_reference = await adapter.record_decision(
        context=context(),
        decision=allowed_decision(),
        requested_source_revision_id=REVISION_ID,
    )

    assert audit_reference == UUID(int=11)
    assert uow.committed is True
    assert len(writer.events) == 1
    event = writer.events[0]
    assert event.event_type == "access.decision"
    assert event.schema_version == 3
    assert event.outcome is AuditOutcome.ALLOWED
    assert event.reason == "allowed"
    assert event.target is not None
    assert event.target.object_type == "source_object"
    assert event.target.object_id == SOURCE_ID
    assert event.payload.model_dump() == {
        "purpose": "knowledge_control",
        "operation": "inspect_parsed_content",
        "decision_kind": "human_disclosure",
        "decision_reference": UUID(int=10),
        "authority_snapshot_reference": UUID(int=9),
        "membership_generation": 1,
        "authority_generation": 1,
        "service_authority_generation": None,
        "source_revision_id": REVISION_ID,
        "authorized_source_revision_id": REVISION_ID,
        "policy_version_id": UUID(int=8),
        "policy_generation": 1,
    }


@pytest.mark.asyncio
async def test_denied_object_decision_retains_requested_revision_without_policy() -> None:
    writer = RecordingAuditWriter()
    adapter = UnitOfWorkParsedContentDecisionAudit(
        uow_factory=lambda trusted: AuditUnitOfWork(writer),
        audit_events=AuditEventRegistry.with_default_families(),
    )
    allowed = allowed_decision()
    denied = ContentAuthorizationDecision(
        allowed=False,
        decision_kind=allowed.decision_kind,
        workspace_id=allowed.workspace_id,
        environment_id=allowed.environment_id,
        source_object_ids=allowed.source_object_ids,
        membership_generation=allowed.membership_generation,
        authority_generation=allowed.authority_generation,
        authority_snapshot_reference=allowed.authority_snapshot_reference,
        purpose=allowed.purpose,
        operation=allowed.operation,
        reason_code=AuthorizationReasonCode.AUTHORIZATION_DENIED,
        decision_reference=UUID(int=16),
        trace_id=allowed.trace_id,
        evaluated_at=allowed.evaluated_at,
    )

    await adapter.record_decision(
        context=context(),
        decision=denied,
        requested_source_revision_id=REVISION_ID,
    )

    payload = writer.events[0].payload.model_dump()
    assert payload["source_revision_id"] == REVISION_ID
    assert payload["authorized_source_revision_id"] is None
    assert payload["policy_version_id"] is None
    assert payload["policy_generation"] is None


@pytest.mark.asyncio
async def test_production_authorizer_uses_current_policy_and_human_authority() -> None:
    adapter = ProductionParsedContentAuthorizer(
        uow_factory=lambda trusted: CurrentAuthorizationUnitOfWork(),
        authority_resolver=CurrentHumanAuthorityResolver(),
    )

    decision = await adapter.decide_read_content(
        context=context(),
        requester=ParsedContentInspectionRequester(
            subject_id=SUBJECT_ID,
            authorization_generation=1,
        ),
        source_object_id=SOURCE_ID,
        source_revision_id=REVISION_ID,
    )

    assert decision.allowed is True, decision.reason_code
    assert decision.policy_evidence[0].source_revision_id == REVISION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority_generation", "grant_read", "reason"),
    (
        (2, True, AuthorizationReasonCode.AUTHORITY_STALE),
        (None, True, AuthorizationReasonCode.DECISION_UNAVAILABLE),
        (1, False, AuthorizationReasonCode.AUTHORIZATION_DENIED),
    ),
    ids=("stale-authority", "unavailable-authority", "missing-grant"),
)
async def test_production_authorizer_fails_closed_without_current_authority(
    authority_generation: int | None,
    grant_read: bool,
    reason: AuthorizationReasonCode,
) -> None:
    adapter = ProductionParsedContentAuthorizer(
        uow_factory=lambda trusted: CurrentAuthorizationUnitOfWork(
            grant_read=grant_read
        ),
        authority_resolver=CurrentHumanAuthorityResolver(authority_generation),
    )

    decision = await adapter.decide_read_content(
        context=context(),
        requester=ParsedContentInspectionRequester(
            subject_id=SUBJECT_ID,
            authorization_generation=1,
        ),
        source_object_id=SOURCE_ID,
        source_revision_id=REVISION_ID,
    )

    assert decision.allowed is False
    assert decision.reason_code is reason
    assert decision.policy_evidence == ()
