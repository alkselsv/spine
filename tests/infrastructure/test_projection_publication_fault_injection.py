from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.projection_publication_faults import (
    ProjectionPublicationFaultBoundary,
    ProjectionPublicationFaultInjected,
    ProjectionPublicationFaultReason,
    ProjectionPublicationFaultRejected,
)
from spine.application.persistence.errors import PersistenceUnavailableError
from spine.infrastructure.projection_publication_fault_injection import (
    ProjectionPublicationFaultController,
    ProjectionPublicationFaultInjectionProfile,
    ProjectionPublicationFaultInjectionSettings,
)
from tests.application.test_projection_result_mapping import _command
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.test_idempotency_repository_contract import (
    persist_environment,
    persist_workspace,
)


WORKSPACE_ID = UUID(int=57801)
ENVIRONMENT_ID = UUID(int=57802)
COMMAND_IDENTITY = "successor-000000000000000000000578"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def _profile(**overrides: object) -> ProjectionPublicationFaultInjectionProfile:
    values: dict[str, object] = {
        "profile_id": "qualification-issue578",
        "profile_version": "v1",
        "commit_sha": "a" * 40,
        "deployment_fingerprint": "b" * 64,
        "provider_profile_digest": "c" * 64,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "projection_kind": "context",
        "successor_command_identity": COMMAND_IDENTITY,
        "rules": (
            {
                "boundary": "before_physical_validation",
                "reason_code": "projection.qualification.before_physical_validation",
            },
            {
                "boundary": "after_validation_before_activation",
                "reason_code": "projection.qualification.after_validation_before_activation",
            },
        ),
    }
    values.update(overrides)
    return ProjectionPublicationFaultInjectionProfile.model_validate(values)


def _controller(profile, adapter=None) -> ProjectionPublicationFaultController:
    return ProjectionPublicationFaultController(
        profile,
        uow_factory=None if adapter is None else adapter.uow_factory,
        context_factory=(
            None
            if adapter is None
            else lambda workspace_id, environment_id, operation, trace_id: (
                adapter.ingestion_context(workspace_id, environment_id, operation)
            )
        ),
        clock=lambda: NOW,
    )


def _successor_command(**updates: object):
    command = _command(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )
    return command.model_copy(
        update={"command_identity": COMMAND_IDENTITY, **updates}
    )


def test_projection_fault_profile_rejects_reason_outside_closed_boundary_mapping() -> None:
    with pytest.raises(ValidationError):
        _profile(
            rules=(
                {
                    "boundary": "before_physical_validation",
                    "reason_code": "projection.qualification.after_validation_before_activation",
                },
            )
        )


def test_projection_fault_profile_empty_binding_disables_and_malformed_is_redacted() -> None:
    assert ProjectionPublicationFaultInjectionSettings(profile_json="").load_profile() is None
    protected = "must-not-appear"

    with pytest.raises(ValueError) as raised:
        ProjectionPublicationFaultInjectionSettings(
            profile_json=f'{{"unexpected":"{protected}"}}'
        ).load_profile()

    assert str(raised.value) == "PROJECTION_FAULT_PROFILE_INVALID"
    assert protected not in str(raised.value)


@pytest.mark.parametrize(
    ("commit_sha", "deployment_fingerprint", "provider_profile_digest", "isolated"),
    (
        ("d" * 40, "b" * 64, "c" * 64, True),
        ("a" * 40, "d" * 64, "c" * 64, True),
        ("a" * 40, "b" * 64, "d" * 64, True),
        ("a" * 40, "b" * 64, "c" * 64, False),
    ),
)
def test_projection_fault_profile_rejects_runtime_mismatch_without_values(
    commit_sha: str,
    deployment_fingerprint: str,
    provider_profile_digest: str,
    isolated: bool,
) -> None:
    controller = _controller(_profile())

    with pytest.raises(ValueError) as raised:
        controller.assert_runtime_binding(
            commit_sha=commit_sha,
            deployment_fingerprint=deployment_fingerprint,
            provider_profile_digest=provider_profile_digest,
            isolated_development=isolated,
        )

    assert str(raised.value) == "PROJECTION_FAULT_PROFILE_MISMATCH"
    assert commit_sha not in str(raised.value)


def test_projection_fault_guard_rejects_cross_workspace_before_provider_io() -> None:
    controller = _controller(_profile())

    with pytest.raises(ProjectionPublicationFaultRejected) as raised:
        controller.assert_command_allowed(
            _successor_command(workspace_id=UUID(int=57803))
        )

    assert raised.value.reason_code == "projection.qualification.binding_rejected"


@pytest.mark.asyncio
async def test_projection_fault_is_durable_one_shot_across_controller_restart() -> None:
    adapter = await create_in_memory_adapter()
    await persist_workspace(adapter, WORKSPACE_ID)
    await persist_environment(adapter, WORKSPACE_ID, ENVIRONMENT_ID)
    command = _successor_command()
    first = _controller(_profile(), adapter)

    with pytest.raises(ProjectionPublicationFaultInjected) as raised:
        await first.inject_if_bound(
            command,
            ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION,
        )

    assert (
        raised.value.reason
        is ProjectionPublicationFaultReason.BEFORE_PHYSICAL_VALIDATION
    )
    restarted = _controller(_profile(), adapter)
    await restarted.inject_if_bound(
        command,
        ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION,
    )
    context = adapter.ingestion_context(
        WORKSPACE_ID, ENVIRONMENT_ID, "projection_qualification_fault"
    )
    audit = await adapter.read_audit_event(context, raised.value.audit_event_id)

    assert audit is not None
    assert audit.reason == raised.value.reason_code


@pytest.mark.asyncio
async def test_projection_fault_retry_after_audit_failure_has_one_durable_effect() -> None:
    adapter = await create_in_memory_adapter()
    await persist_workspace(adapter, WORKSPACE_ID)
    await persist_environment(adapter, WORKSPACE_ID, ENVIRONMENT_ID)
    command = _successor_command()
    controller = _controller(_profile(), adapter)
    adapter.fail_next_audit_append()

    with pytest.raises(PersistenceUnavailableError):
        await controller.inject_if_bound(
            command,
            ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION,
        )

    with pytest.raises(ProjectionPublicationFaultInjected) as injected:
        await controller.inject_if_bound(
            command,
            ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION,
        )
    restarted = _controller(_profile(), adapter)
    await restarted.inject_if_bound(
        command,
        ProjectionPublicationFaultBoundary.BEFORE_PHYSICAL_VALIDATION,
    )
    context = adapter.ingestion_context(
        WORKSPACE_ID, ENVIRONMENT_ID, "projection_qualification_fault"
    )
    audit = await adapter.read_audit_event(
        context, injected.value.audit_event_id
    )

    assert audit is not None
    assert audit.reason == injected.value.reason_code
