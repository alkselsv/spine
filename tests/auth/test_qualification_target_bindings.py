from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from spine.auth.qualification_targets import (
    ExternalSecretReference,
    InMemoryQualificationSecretResolver,
    InMemoryQualificationTargetBindingResolver,
    QualificationBindingDeniedError,
    QualificationDatabaseAccess,
    QualificationSecretDeniedError,
    QualificationSecretRecord,
    QualificationSecretStatus,
    QualificationTarget,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetRole,
)
from spine.auth.records import (
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
)
from tests.contracts.auth.qualification_target_binding_contract import (
    QualificationTargetContractCase,
    QualificationTargetResolverScenario,
    assert_qualification_target_binding_resolver_contract,
)


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
AUTHORITY_BINDING_ID = UUID("15000000-0000-0000-0000-000000000001")
QUALIFICATION_ID = UUID("16000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
RESTORE_ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000002")
SOURCE_PRINCIPAL_ID = UUID("17000000-0000-0000-0000-000000000001")
RESTORE_PRINCIPAL_ID = UUID("17000000-0000-0000-0000-000000000002")
VERIFIER_PRINCIPAL_ID = UUID("17000000-0000-0000-0000-000000000003")
SOURCE_POLICY_ID = UUID("1a000000-0000-0000-0000-000000000001")
RESTORE_POLICY_ID = UUID("1a000000-0000-0000-0000-000000000002")
PROVENANCE = AuthorizationRecordProvenance(
    change_id=UUID("19000000-0000-0000-0000-000000000001"),
    recorded_by="synthetic-qualification-test",
)


def secret_reference(target: QualificationTarget) -> ExternalSecretReference:
    return ExternalSecretReference(
        provider_reference="approved-provider-config:v1",
        secret_reference=f"qualification/{target.value}/postgres:v1",
    )


def binding(
    *,
    target: QualificationTarget = QualificationTarget.SOURCE,
    role: QualificationTargetRole = QualificationTargetRole.SOURCE_OPERATOR,
    environment_id: UUID = SOURCE_ENVIRONMENT_ID,
    principal_id: UUID = SOURCE_PRINCIPAL_ID,
    database_access: QualificationDatabaseAccess = QualificationDatabaseAccess.WRITE,
    binding_id: UUID = UUID("18000000-0000-0000-0000-000000000001"),
    binding_series_id: UUID | None = None,
    binding_version: int = 1,
    predecessor_binding_id: UUID | None = None,
    authority_binding_id: UUID = AUTHORITY_BINDING_ID,
    authority_binding_version: int = 1,
    alias_reference: str | None = None,
    secret: ExternalSecretReference | None = None,
    database_role_reference: str | None = None,
    target_scope_reference: str | None = None,
) -> QualificationTargetBinding:
    return QualificationTargetBinding(
        binding_id=binding_id,
        binding_series_id=binding_series_id or binding_id,
        binding_version=binding_version,
        predecessor_binding_id=predecessor_binding_id,
        authority_binding_id=authority_binding_id,
        authority_binding_version=authority_binding_version,
        qualification_id=QUALIFICATION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=environment_id,
        target=target,
        target_reference=f"target-inventory:{target.value}:v1",
        target_scope_reference=target_scope_reference or f"target-scope:{target.value}:v1",
        principal_id=principal_id,
        principal_kind=QualificationPrincipalKind.SERVICE,
        purpose="qualification_recovery",
        operation=role.value,
        alias_reference=alias_reference or f"principal-alias:{target.value}:v1",
        secret=secret or secret_reference(target),
        database_role_reference=database_role_reference or f"postgres-role:{role.value}:v1",
        target_role=role,
        database_access=database_access,
        access_policy_references=(
            SOURCE_POLICY_ID if target is QualificationTarget.SOURCE else RESTORE_POLICY_ID,
        ),
    )


def request(record: QualificationTargetBinding) -> QualificationTargetBindingRequest:
    return QualificationTargetBindingRequest(
        binding_id=record.binding_id,
        binding_series_id=record.binding_series_id,
        binding_version=record.binding_version,
        predecessor_binding_id=record.predecessor_binding_id,
        authority_binding_id=record.authority_binding_id,
        authority_binding_version=record.authority_binding_version,
        qualification_id=record.qualification_id,
        workspace_id=record.workspace_id,
        environment_id=record.environment_id,
        target=record.target,
        target_reference=record.target_reference,
        target_scope_reference=record.target_scope_reference,
        principal_id=record.principal_id,
        principal_kind=record.principal_kind,
        purpose=record.purpose,
        operation=record.operation,
        alias_reference=record.alias_reference,
        secret=record.secret,
        database_role_reference=record.database_role_reference,
        target_role=record.target_role,
        required_database_access=record.database_access,
        access_policy_references=record.access_policy_references,
    )


def authority_binding(
    record: QualificationTargetBinding,
    *,
    status: AuthorizationRecordStatus = AuthorizationRecordStatus.ACTIVE,
    expires_at: datetime = NOW + timedelta(hours=1),
    version: int | None = None,
) -> QualificationAuthorityBinding:
    return QualificationAuthorityBinding(
        binding_id=record.authority_binding_id,
        qualification_id=record.qualification_id,
        workspace_id=record.workspace_id,
        environment_id=record.environment_id,
        principal_kind=record.principal_kind,
        principal_id=record.principal_id,
        purpose=record.purpose,
        operation=record.operation,
        expires_at=expires_at,
        version=version or record.authority_binding_version,
        status=status,
        provenance=PROVENANCE,
    )


def target_resolver(
    *records: QualificationTargetBinding,
    authority_records: tuple[QualificationAuthorityBinding, ...] | None = None,
) -> InMemoryQualificationTargetBindingResolver:
    authorities = authority_records or tuple(authority_binding(record) for record in records)
    return InMemoryQualificationTargetBindingResolver(
        tuple(records),
        authority_bindings=authorities,
        approved_access_policy_references=frozenset(
            policy
            for record in records
            for policy in record.access_policy_references
        ),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_in_memory_qualification_target_binding_resolver_contract() -> None:
    async def scenario(
        case: QualificationTargetContractCase,
    ) -> QualificationTargetResolverScenario:
        source = binding()
        authority = authority_binding(source)
        candidate = request(source)
        expected: QualificationTargetBinding | None = source
        if case == "target_mismatch":
            candidate = candidate.model_copy(
                update={
                    "target": QualificationTarget.RESTORE,
                    "target_reference": "target-inventory:restore:v1",
                    "target_scope_reference": "target-scope:restore:v1",
                    "environment_id": RESTORE_ENVIRONMENT_ID,
                }
            )
            expected = None
        elif case == "revoked":
            authority = authority_binding(
                source, status=AuthorizationRecordStatus.REVOKED
            )
            expected = None
        return QualificationTargetResolverScenario(
            resolver=target_resolver(
                source,
                authority_records=(authority,),
            ),
            request=candidate,
            expected=expected,
        )

    await assert_qualification_target_binding_resolver_contract(scenario)


def test_target_binding_accepts_references_only_and_rejects_inline_secret() -> None:
    record = binding()

    assert record.secret.secret_reference == "qualification/source/postgres:v1"
    assert "value" not in record.secret.model_dump()

    with pytest.raises(ValidationError):
        ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference="qualification/source/postgres:v1",
            value="must-not-enter-configuration",
        )


def test_verifier_binding_is_restore_only_and_read_only() -> None:
    verifier = binding(
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.VERIFIER,
        environment_id=RESTORE_ENVIRONMENT_ID,
        principal_id=VERIFIER_PRINCIPAL_ID,
        database_access=QualificationDatabaseAccess.READ_ONLY,
    )

    assert verifier.database_access is QualificationDatabaseAccess.READ_ONLY

    with pytest.raises(ValidationError, match="verifier.*restore.*read-only"):
        binding(
            role=QualificationTargetRole.VERIFIER,
            principal_id=VERIFIER_PRINCIPAL_ID,
        )


@pytest.mark.asyncio
async def test_target_resolver_denies_source_binding_against_restore() -> None:
    source = binding()
    restore_request = request(source).model_copy(
        update={
            "target": QualificationTarget.RESTORE,
            "target_reference": "target-inventory:restore:v1",
            "target_scope_reference": "target-scope:restore:v1",
            "environment_id": RESTORE_ENVIRONMENT_ID,
        }
    )
    resolver = target_resolver(source)

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(restore_request)


@pytest.mark.asyncio
async def test_target_resolver_denies_ambiguous_binding() -> None:
    source = binding()
    resolver = target_resolver(source, source)

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(source))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shared_field",
    (
        "principal",
        "alias",
        "secret",
        "database_role",
        "target_scope",
    ),
)
async def test_target_resolver_denies_cross_target_reference_reuse(
    shared_field: str,
) -> None:
    source = binding()
    overrides: dict[str, object] = {
        "principal_id": RESTORE_PRINCIPAL_ID,
    }
    if shared_field == "principal":
        overrides["principal_id"] = source.principal_id
    if shared_field == "alias":
        overrides["alias_reference"] = source.alias_reference
    if shared_field == "secret":
        overrides["secret"] = source.secret
    if shared_field == "database_role":
        overrides["database_role_reference"] = source.database_role_reference
    if shared_field == "target_scope":
        overrides["target_scope_reference"] = source.target_scope_reference
    restore = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000002"),
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.RESTORE_OPERATOR,
        environment_id=RESTORE_ENVIRONMENT_ID,
        **overrides,
    )
    resolver = target_resolver(source, restore)

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(source))

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(restore))


@pytest.mark.asyncio
async def test_restore_writer_and_verifier_share_only_restore_inventory() -> None:
    writer = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000002"),
        authority_binding_id=UUID("15000000-0000-0000-0000-000000000002"),
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.RESTORE_OPERATOR,
        environment_id=RESTORE_ENVIRONMENT_ID,
        principal_id=RESTORE_PRINCIPAL_ID,
        alias_reference="principal-alias:restore-writer:v1",
        secret=ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference="qualification/restore/writer:v1",
        ),
        target_scope_reference="target-scope:restore-writer:v1",
    )
    verifier = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000003"),
        authority_binding_id=UUID("15000000-0000-0000-0000-000000000003"),
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.VERIFIER,
        environment_id=RESTORE_ENVIRONMENT_ID,
        principal_id=VERIFIER_PRINCIPAL_ID,
        database_access=QualificationDatabaseAccess.READ_ONLY,
        alias_reference="principal-alias:restore-verifier:v1",
        secret=ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference="qualification/restore/verifier:v1",
        ),
        target_scope_reference="target-scope:restore-verifier:v1",
    )
    resolver = target_resolver(writer, verifier)

    assert writer.target_reference == verifier.target_reference
    assert await resolver.resolve(request(writer)) == writer
    assert await resolver.resolve(request(verifier)) == verifier


@pytest.mark.asyncio
async def test_restore_verifier_cannot_reuse_writer_secret() -> None:
    writer = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000002"),
        authority_binding_id=UUID("15000000-0000-0000-0000-000000000002"),
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.RESTORE_OPERATOR,
        environment_id=RESTORE_ENVIRONMENT_ID,
        principal_id=RESTORE_PRINCIPAL_ID,
        alias_reference="principal-alias:restore-writer:v1",
        target_scope_reference="target-scope:restore-writer:v1",
    )
    verifier = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000003"),
        authority_binding_id=UUID("15000000-0000-0000-0000-000000000003"),
        target=QualificationTarget.RESTORE,
        role=QualificationTargetRole.VERIFIER,
        environment_id=RESTORE_ENVIRONMENT_ID,
        principal_id=VERIFIER_PRINCIPAL_ID,
        database_access=QualificationDatabaseAccess.READ_ONLY,
        alias_reference="principal-alias:restore-verifier:v1",
        secret=writer.secret,
        target_scope_reference="target-scope:restore-verifier:v1",
    )
    resolver = target_resolver(writer, verifier)

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(verifier))


def test_access_policy_references_are_canonical_and_unique() -> None:
    record = binding().model_copy(
        update={"access_policy_references": (RESTORE_POLICY_ID, SOURCE_POLICY_ID)}
    )
    canonical = QualificationTargetBinding.model_validate(record.model_dump())

    assert canonical.access_policy_references == (SOURCE_POLICY_ID, RESTORE_POLICY_ID)

    with pytest.raises(ValidationError, match="must be unique"):
        QualificationTargetBindingRequest.model_validate(
            {
                **request(binding()).model_dump(),
                "access_policy_references": (SOURCE_POLICY_ID, SOURCE_POLICY_ID),
            }
        )


@pytest.mark.asyncio
async def test_same_target_successor_rotates_binding_and_denies_predecessor() -> None:
    predecessor = binding()
    successor = binding(
        binding_id=UUID("18000000-0000-0000-0000-000000000004"),
        binding_series_id=predecessor.binding_series_id,
        binding_version=2,
        predecessor_binding_id=predecessor.binding_id,
        authority_binding_version=2,
        secret=ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference="qualification/source/postgres:v2",
        ),
    )
    resolver = target_resolver(
        predecessor,
        successor,
        authority_records=(
            authority_binding(predecessor),
            authority_binding(successor, version=2),
        ),
    )

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(predecessor))
    assert await resolver.resolve(request(successor)) == successor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expires_at"),
    (
        (AuthorizationRecordStatus.REVOKED, NOW + timedelta(hours=1)),
        (AuthorizationRecordStatus.ACTIVE, NOW),
    ),
    ids=("revoked", "expired"),
)
async def test_target_resolver_denies_when_canonical_authority_is_not_current(
    status: AuthorizationRecordStatus,
    expires_at: datetime,
) -> None:
    source = binding()
    resolver = target_resolver(
        source,
        authority_records=(
            authority_binding(source, status=status, expires_at=expires_at),
        ),
    )

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(source))


@pytest.mark.asyncio
async def test_target_resolver_denies_missing_approved_access_policy_reference() -> None:
    source = binding()
    resolver = InMemoryQualificationTargetBindingResolver(
        (source,),
        authority_bindings=(authority_binding(source),),
        approved_access_policy_references=frozenset(),
        clock=lambda: NOW,
    )

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(request(source))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (QualificationSecretStatus.EXPIRED, QualificationSecretStatus.REVOKED),
)
async def test_secret_resolver_denies_unusable_reference_without_disclosure(
    status: QualificationSecretStatus,
) -> None:
    reference = secret_reference(QualificationTarget.SOURCE)
    resolver = InMemoryQualificationSecretResolver(
        (
            QualificationSecretRecord(
                reference=reference,
                target=QualificationTarget.SOURCE,
                target_reference="target-inventory:source:v1",
                database_role_reference="postgres-role:source_operator:v1",
                status=status,
                expires_at=NOW - timedelta(seconds=1),
                value=SecretStr("synthetic-secret-that-must-not-leak"),
            ),
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(QualificationSecretDeniedError) as failure:
        await resolver.resolve(
            reference,
            target=QualificationTarget.SOURCE,
            target_reference="target-inventory:source:v1",
            database_role_reference="postgres-role:source_operator:v1",
        )

    assert "synthetic-secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_secret_resolver_returns_secret_only_for_exact_target_and_role() -> None:
    reference = secret_reference(QualificationTarget.SOURCE)
    resolver = InMemoryQualificationSecretResolver(
        (
            QualificationSecretRecord(
                reference=reference,
                target=QualificationTarget.SOURCE,
                target_reference="target-inventory:source:v1",
                database_role_reference="postgres-role:source_operator:v1",
                status=QualificationSecretStatus.ACTIVE,
                expires_at=NOW + timedelta(hours=1),
                value=SecretStr("synthetic-source-secret"),
            ),
        ),
        clock=lambda: NOW,
    )

    resolved = await resolver.resolve(
        reference,
        target=QualificationTarget.SOURCE,
        target_reference="target-inventory:source:v1",
        database_role_reference="postgres-role:source_operator:v1",
    )
    assert resolved.get_secret_value() == "synthetic-source-secret"
    assert "synthetic-source-secret" not in repr(resolved)

    with pytest.raises(QualificationSecretDeniedError):
        await resolver.resolve(
            reference,
            target=QualificationTarget.RESTORE,
            target_reference="target-inventory:restore:v1",
            database_role_reference="postgres-role:restore_operator:v1",
        )


@pytest.mark.asyncio
async def test_secret_resolver_denies_missing_and_ambiguous_references() -> None:
    reference = secret_reference(QualificationTarget.SOURCE)
    record = QualificationSecretRecord(
        reference=reference,
        target=QualificationTarget.SOURCE,
        target_reference="target-inventory:source:v1",
        database_role_reference="postgres-role:source_operator:v1",
        status=QualificationSecretStatus.ACTIVE,
        expires_at=NOW + timedelta(hours=1),
        value=SecretStr("synthetic-source-secret"),
    )

    for records in ((), (record, record)):
        resolver = InMemoryQualificationSecretResolver(records, clock=lambda: NOW)
        with pytest.raises(QualificationSecretDeniedError):
            await resolver.resolve(
                reference,
                target=QualificationTarget.SOURCE,
                target_reference="target-inventory:source:v1",
                database_role_reference="postgres-role:source_operator:v1",
            )
