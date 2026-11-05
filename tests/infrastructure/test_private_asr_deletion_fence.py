from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.private_asr_corpus import (
    PrivateArtifactDeletionAuthorization,
    PrivateArtifactDeletionRequestV2,
    PrivateArtifactDeletionSourceBinding,
    PrivateArtifactSourceLineage,
)
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.domain.sources.retention import RetentionArtifactClass
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.private_asr.postgresql_storage import (
    PostgreSQLPrivateASRDeletionFence,
    PrivateASRStorageDenied,
)


NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
SOURCE_OBJECT_ID = UUID(int=3)
SOURCE_REVISION_ID = UUID(int=4)

BOUNDARY = TrustedContextBoundary.for_testing(
    issuer_id=UUID(int=10), secret=b"issue-976-deletion-fence-tests-secret"
)
CONTEXT = BOUNDARY.worker(
    scope=EnvironmentScope(
        workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
    ),
    service_principal_id=UUID(int=11),
    purpose=PersistencePurpose("asr_model_evaluation"),
    operation=PersistenceOperation("private_asr_retention_expiration"),
    trace_id=UUID(int=12),
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return None


class _Transaction:
    def __init__(self, revision_digest: str) -> None:
        self.revision_digest = revision_digest
        self.statements: list[object] = []
        self.parameters: list[object | None] = []
        self.guard = None

    async def execute_statement(
        self, statement: object, parameters: object | None = None
    ) -> _Result:
        self.statements.append(statement)
        self.parameters.append(parameters)
        return _Result(self.revision_digest)

    def register_commit_guard(self, guard) -> None:
        self.guard = guard


def _request_and_authorization() -> tuple[
    PrivateArtifactDeletionRequestV2, PrivateArtifactDeletionAuthorization
]:
    digest = "a" * 64
    source = PrivateArtifactSourceLineage(
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        source_revision_digest=digest,
    )
    binding = PrivateArtifactDeletionSourceBinding(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        revision_digest=digest,
        applicability_id=UUID(int=20),
        applicability_generation=1,
        expected_current_applicability_id=UUID(int=20),
        expected_current_generation=1,
        artifact_class=RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
        retention_policy_id=UUID(int=21),
        retention_policy_version_id=UUID(int=22),
        retention_policy_digest="b" * 64,
        approving_authority_reference=UUID(int=23),
        approving_authority_version_id=UUID(int=24),
        approving_authority_digest="c" * 64,
        applicability_evidence_reference=UUID(int=25),
        applicability_evidence_digest="d" * 64,
        approval_evidence_reference=UUID(int=26),
        terms_version="terms:v1",
        terms_digest="e" * 64,
        deletion_obligation_reference=UUID(int=27),
        backup_obligation_reference=UUID(int=28),
    )
    request = PrivateArtifactDeletionRequestV2(
        artifact_id=UUID(int=30),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        purpose="asr_model_evaluation",
        artifact_kind="report",
        action="retention_expiration",
        expected_state_digest="f" * 64,
        retention_profile_ref=PrivateReference(
            reference_id=UUID(int=31), reference_kind="retention_applicability"
        ),
        retention_deadline=NOW - timedelta(minutes=1),
        requested_at=NOW,
        expected_source_lineage=(source,),
        source_bindings=(binding,),
        authority_version="deletion-authority:v1",
        valid_until=NOW + timedelta(minutes=5),
    )
    authorization = PrivateArtifactDeletionAuthorization(
        request_digest=request.calculate_digest(),
        authorization_ref=PrivateReference(
            reference_id=UUID(int=32),
            reference_kind="private_asr_deletion_authorization",
        ),
        authority_version=request.authority_version,
        revocation_epoch=1,
        valid_until=request.valid_until,
        authorized=True,
    )
    return request, authorization


def _two_source_request(
    *, reversed_sources: bool,
) -> tuple[PrivateArtifactDeletionRequestV2, PrivateArtifactDeletionAuthorization]:
    request, _ = _request_and_authorization()
    first_binding = request.source_bindings[0]
    second_binding = first_binding.model_copy(update={
        "source_object_id": UUID(int=5),
        "source_revision_id": UUID(int=6),
    })
    first_lineage = request.expected_source_lineage[0]
    second_lineage = first_lineage.model_copy(update={
        "source_object_id": UUID(int=5),
        "source_revision_id": UUID(int=6),
    })
    bindings = (first_binding, second_binding)
    lineage = (first_lineage, second_lineage)
    if reversed_sources:
        bindings = tuple(reversed(bindings))
        lineage = tuple(reversed(lineage))
    composite = PrivateArtifactDeletionRequestV2.model_validate({
        **request.model_dump(),
        "source_bindings": bindings,
        "expected_source_lineage": lineage,
    })
    authorization = PrivateArtifactDeletionAuthorization(
        request_digest=composite.calculate_digest(),
        authorization_ref=PrivateReference(
            reference_id=UUID(int=32),
            reference_kind="private_asr_deletion_authorization",
        ),
        authority_version=composite.authority_version,
        revocation_epoch=1,
        valid_until=composite.valid_until,
        authorized=True,
    )
    return composite, authorization


@pytest.mark.asyncio
async def test_postgresql_deletion_fence_binds_exact_lineage_and_registers_expiry_guard() -> None:
    request, authorization = _request_and_authorization()
    clock = _Clock(NOW)
    transaction = _Transaction(request.source_bindings[0].revision_digest)

    await PostgreSQLPrivateASRDeletionFence(clock).acquire(
        request,
        authorization,
        context=CONTEXT,
        transaction=transaction,
    )

    assert transaction.guard is not None
    assert transaction.guard() is True
    clock.now = request.valid_until
    assert transaction.guard() is False


@pytest.mark.asyncio
async def test_postgresql_deletion_fence_denies_authorization_for_another_request() -> None:
    request, authorization = _request_and_authorization()
    transaction = _Transaction(request.source_bindings[0].revision_digest)
    other_request = request.model_copy(update={"expected_state_digest": "0" * 64})

    with pytest.raises(PrivateASRStorageDenied):
        await PostgreSQLPrivateASRDeletionFence(lambda: NOW).acquire(
            other_request,
            authorization,
            context=CONTEXT,
            transaction=transaction,
        )

    assert transaction.statements == []


@pytest.mark.asyncio
async def test_postgresql_deletion_fence_denies_stale_source_revision() -> None:
    request, authorization = _request_and_authorization()
    transaction = _Transaction("0" * 64)

    with pytest.raises(PrivateASRStorageDenied):
        await PostgreSQLPrivateASRDeletionFence(lambda: NOW).acquire(
            request,
            authorization,
            context=CONTEXT,
            transaction=transaction,
        )

    assert transaction.guard is None


@pytest.mark.asyncio
async def test_postgresql_deletion_fence_denies_zero_revocation_epoch() -> None:
    request, authorization = _request_and_authorization()
    authorization = authorization.model_copy(update={"revocation_epoch": 0})
    transaction = _Transaction(request.source_bindings[0].revision_digest)

    with pytest.raises(PrivateASRStorageDenied):
        await PostgreSQLPrivateASRDeletionFence(lambda: NOW).acquire(
            request,
            authorization,
            context=CONTEXT,
            transaction=transaction,
        )

    assert transaction.statements == []


@pytest.mark.asyncio
async def test_postgresql_deletion_fence_acquires_source_locks_in_canonical_order() -> None:
    request, authorization = _two_source_request(reversed_sources=True)
    transaction = _Transaction(request.source_bindings[0].revision_digest)

    await PostgreSQLPrivateASRDeletionFence(lambda: NOW).acquire(
        request,
        authorization,
        context=CONTEXT,
        transaction=transaction,
    )

    acquired_keys = [
        parameters["key"]
        for parameters in transaction.parameters
        if isinstance(parameters, dict) and "key" in parameters
    ]
    assert acquired_keys == [
        f"{WORKSPACE_ID}:{ENVIRONMENT_ID}:{UUID(int=3)}",
        f"{WORKSPACE_ID}:{ENVIRONMENT_ID}:{UUID(int=5)}",
    ]
