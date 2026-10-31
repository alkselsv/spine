from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.yandex_qualification_connection import (
    YANDEX_QUALIFICATION_APPROVAL_ID,
    YANDEX_QUALIFICATION_CONNECTION_ID,
    YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
    YANDEX_QUALIFICATION_GRANT_REFERENCE,
    YANDEX_QUALIFICATION_REFERENCE,
    YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
    YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
    YandexQualificationConnectionConfig,
    YandexQualificationConnectionBootstrap,
    build_yandex_qualification_connection_plan,
)
from spine.domain.connections.provider import (
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
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
