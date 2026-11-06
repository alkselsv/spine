from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.yandex_qualification_connection import (
    YANDEX_QUALIFICATION_APPROVAL_ID,
    YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID,
    YANDEX_QUALIFICATION_BINARY_READ_REFERENCE,
    YANDEX_QUALIFICATION_CONNECTION_ID,
    YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
    YANDEX_QUALIFICATION_GRANT_REFERENCE,
    YANDEX_QUALIFICATION_REFERENCE,
    YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
    YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
    YandexQualificationConnectionConfig,
    YandexQualificationConnectionBootstrap,
    YandexQualificationBinaryReadConfig,
    YandexQualificationBinaryReadProvisioner,
    build_yandex_qualification_binary_read_plan,
    build_yandex_qualification_connection_plan,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


def _id(value: int) -> UUID:
    return UUID(f"76200000-0000-4000-8000-{value:012d}")


def _config() -> YandexQualificationConnectionConfig:
    return YandexQualificationConnectionConfig(
        workspace_id=_id(1),
        environment_id=_id(2),
        approval_id=YANDEX_QUALIFICATION_APPROVAL_ID,
        connection_id=YANDEX_QUALIFICATION_CONNECTION_ID,
        service_principal_id=YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
        operator_identity_id=_id(10),
        credential_reference=YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
        grant_reference=YANDEX_QUALIFICATION_GRANT_REFERENCE,
        resource_scope_reference=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
        qualification_reference=YANDEX_QUALIFICATION_REFERENCE,
        authorization_generation=2,
        configuration_generation=3,
        approved_at=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc),
        security_approval_reference="issue-701-comment-6539550358",
        data_approval_reference="issue-702-comment-6539550684",
        fixture_manifest_version="spine-yandex-qualification-fixtures-v1",
        scope_version="spine-yandex-qualification-scope:v1",
        approved_root="/Spine-Qualification-678-v1",
    )


def _binary_read_config() -> YandexQualificationBinaryReadConfig:
    approved_at = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    return YandexQualificationBinaryReadConfig(
        workspace_id=_id(1),
        environment_id=_id(2),
        operator_identity_id=_id(10),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(days=7),
        manifest_digest=(
            "487360a49df37aa6fd3dac3b78750b0d991a8a7de44f5fc405a85f304befedc7"
        ),
        security_approval_reference="issue-789-comment-6687500665",
        data_approval_reference="issue-789-comment-6687500665",
        max_live_runs=10,
        download_redirect_host="mds.storage.yandex.net",
    )


def _metadata_connection() -> ProviderConnection:
    plan = build_yandex_qualification_connection_plan(_config())
    return ProviderConnection(
        connection_version_id=_id(30),
        connection_id=plan.request.connection_id,
        approval_id=plan.approval.approval_id,
        version=1,
        predecessor_version_id=None,
        workspace_id=plan.approval.workspace_id,
        environment_id=plan.approval.environment_id,
        service_principal_id=plan.approval.service_principal_id,
        provider_kind=plan.approval.provider_kind,
        credential_reference=plan.approval.credential_reference,
        grant_reference=plan.approval.grant_reference,
        approved_resource_scope_references=(
            OpaqueProviderReference(value=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE),
        ),
        authorization_generation=2,
        configuration_generation=3,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=plan.observation.capabilities,
    )


def test_binary_read_plan_is_exact_successor_of_metadata_authority() -> None:
    predecessor = _metadata_connection()

    plan = build_yandex_qualification_binary_read_plan(
        _binary_read_config(), predecessor=predecessor
    )

    assert plan.approval.approval_id == YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID
    assert plan.approval.authorization_generation == 2
    assert plan.approval.configuration_generation == 4
    assert plan.approval.valid_until == _binary_read_config().expires_at
    assert plan.request.expected_version == 1
    assert plan.request.expected_predecessor_version_id == predecessor.connection_version_id
    assert tuple(capability.kind for capability in plan.observation.capabilities) == (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.FULL_RECONCILIATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
    )
    assert plan.observation.capabilities[-1] == ProviderCapability(
        kind=ProviderCapabilityKind.BINARY_READ,
        status=ProviderCapabilityStatus.SUPPORTED,
        qualification_reference=OpaqueProviderReference(
            value=YANDEX_QUALIFICATION_BINARY_READ_REFERENCE
        ),
    )


async def test_binary_read_provisioning_returns_refs_only_successor_report(
    tmp_path: Path,
) -> None:
    config = _binary_read_config()
    predecessor = _metadata_connection()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(20), secret=b"issue-789-application-test-secret"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=config.workspace_id,
            environment_id=config.environment_id,
        ),
        acting_subject_id=config.operator_identity_id,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=_id(21),
    )

    class Authority:
        async def verify(self, candidate):
            assert candidate == config

    class Repository:
        async def get_current(self, connection_id, supplied_context):
            assert connection_id == predecessor.connection_id
            assert supplied_context == context
            return predecessor

        async def provision(self, *, result, **_):
            return result

    class Writer:
        def create(self, path, content):
            path.write_bytes(content)

    report_path = tmp_path / "binary-read-report.json"
    report = await YandexQualificationBinaryReadProvisioner(
        authority=Authority(),
        repository=Repository(),
        artifact_writer=Writer(),
        context=context,
    ).run(config, report_path=report_path)

    assert report.connection_version == 2
    assert report.predecessor_version_id == predecessor.connection_version_id
    assert report.configuration_generation == 4
    assert report.valid_until == config.expires_at
    assert report.manifest_digest == config.manifest_digest
    assert set(report.capability_kinds) == {
        "binary_read",
        "enumeration",
        "full_reconciliation",
        "metadata_read",
    }
    assert report.credentials_recorded is False
    assert report.provider_request_made is False
    assert report_path.exists()


@pytest.mark.parametrize(
    "predecessor",
    (
        _metadata_connection().model_copy(update={"version": 2}),
        _metadata_connection().model_copy(
            update={"approval_id": YANDEX_QUALIFICATION_BINARY_READ_APPROVAL_ID}
        ),
        _metadata_connection().model_copy(update={"workspace_id": _id(99)}),
    ),
    ids=("stale_version", "wrong_approval", "wrong_workspace"),
)
def test_binary_read_plan_rejects_noncanonical_predecessor(
    predecessor: ProviderConnection,
) -> None:
    with pytest.raises(ValueError, match="approved metadata authority"):
        build_yandex_qualification_binary_read_plan(
            _binary_read_config(), predecessor=predecessor
        )


def test_plan_builds_only_approved_metadata_qualification_authority() -> None:
    plan = build_yandex_qualification_connection_plan(_config())

    assert plan.approval.provider_kind is ProviderKind.YANDEX_DISK
    assert plan.approval.workspace_id == _id(1)
    assert plan.approval.environment_id == _id(2)
    assert plan.request.connection_id == YANDEX_QUALIFICATION_CONNECTION_ID
    assert plan.request.expected_version == 0
    assert plan.request.expected_predecessor_version_id is None
    assert (
        plan.request.idempotency_key == "issue-762-yandex-qualification-connection-v1"
    )
    assert tuple(
        (capability.kind, capability.status)
        for capability in plan.observation.capabilities
    ) == (
        (ProviderCapabilityKind.ENUMERATION, ProviderCapabilityStatus.SUPPORTED),
        (
            ProviderCapabilityKind.FULL_RECONCILIATION,
            ProviderCapabilityStatus.SUPPORTED,
        ),
        (ProviderCapabilityKind.METADATA_READ, ProviderCapabilityStatus.SUPPORTED),
    )


def test_config_rejects_unapproved_scope_version() -> None:
    payload = _config().model_dump(mode="python")
    payload["scope_version"] = "wider-yandex-scope:v1"

    with pytest.raises(ValidationError):
        YandexQualificationConnectionConfig.model_validate(payload)


@pytest.mark.parametrize(
    "field_name",
    [
        "service_principal_id",
        "resource_scope_reference",
        "qualification_reference",
    ],
)
def test_config_rejects_authority_outside_fixed_678_references(
    field_name: str,
) -> None:
    payload = _config().model_dump(mode="python")
    payload[field_name] = _id(99)

    with pytest.raises(ValidationError):
        YandexQualificationConnectionConfig.model_validate(payload)


async def test_bootstrap_rejects_operator_outside_trusted_context(
    tmp_path: Path,
) -> None:
    config = _config()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(20), secret=b"issue-762-operator-mismatch-test"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=config.workspace_id,
            environment_id=config.environment_id,
        ),
        acting_subject_id=_id(99),
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=_id(21),
    )

    class MustNotRun:
        def __getattr__(self, name):
            raise AssertionError(f"{name} must not run for a mismatched operator")

    service = YandexQualificationConnectionBootstrap(
        authority=MustNotRun(),
        repository=MustNotRun(),
        artifact_writer=MustNotRun(),
        context=context,
    )

    with pytest.raises(ValueError, match="operator"):
        await service.run(config, report_path=tmp_path / "report.json")


async def test_bootstrap_retry_after_report_failure_has_one_logical_effect(
    tmp_path: Path,
) -> None:
    config = _config()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(20), secret=b"issue-762-application-test-secret-v1"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=config.workspace_id,
            environment_id=config.environment_id,
        ),
        acting_subject_id=config.operator_identity_id,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=_id(21),
    )

    class Authority:
        async def verify(self, candidate):
            assert candidate == config

    class Repository:
        result = None
        logical_effects = 0

        async def bootstrap(self, *, result, **_):
            if self.result is None:
                self.result = result
                self.logical_effects += 1
                return result
            return replace(self.result, replay=True)

    class Writer:
        attempts = 0

        def create(self, path, content):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("synthetic report interruption")
            path.write_bytes(content)

    repository = Repository()
    writer = Writer()
    service = YandexQualificationConnectionBootstrap(
        authority=Authority(),
        repository=repository,
        artifact_writer=writer,
        context=context,
    )
    report_path = tmp_path / "report.json"

    with pytest.raises(OSError):
        await service.run(config, report_path=report_path)
    report = await service.run(config, report_path=report_path)

    assert repository.logical_effects == 1
    assert report.connection_replay is True
    assert report_path.exists()
