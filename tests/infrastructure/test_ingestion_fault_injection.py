from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.infrastructure.ingestion_fault_injection import (
    IngestionFaultBoundary,
    IngestionFaultController,
    IngestionFaultInjected,
    IngestionFaultInjectionProfile,
    IngestionFaultInjectionSettings,
    IngestionFaultReason,
)


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
PARSER_COMMAND = "api.document.upload." + "a" * 32
STORAGE_COMMAND = "api.document.upload." + "b" * 32
ACCEPTANCE_COMMAND = "api.document.upload." + "c" * 32


def _profile(**overrides: object) -> IngestionFaultInjectionProfile:
    values: dict[str, object] = {
        "profile_id": "qualification-q16-02",
        "profile_version": "v1",
        "deployment_profile_id": "spine-local-qa",
        "deployment_profile_version": "v1",
        "deployment_fingerprint": "a" * 64,
        "rules": (
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "command_identity": PARSER_COMMAND,
                "boundary": IngestionFaultBoundary.PARSER,
                "reason_code": IngestionFaultReason.PARSER_RETRYABLE,
            },
        ),
    }
    if "boundary" in overrides or "reason_code" in overrides:
        rule = dict(values["rules"][0])
        for name in ("boundary", "reason_code"):
            if name in overrides:
                rule[name] = overrides.pop(name)
        values["rules"] = (rule,)
    values.update(overrides)
    return IngestionFaultInjectionProfile.model_validate(values)


def test_fault_profile_rejects_reason_outside_closed_boundary_mapping() -> None:
    with pytest.raises(ValidationError):
        _profile(reason_code=IngestionFaultReason.ORIGINAL_STORAGE_UNAVAILABLE)


def test_fault_profile_rejects_non_server_command_targets() -> None:
    with pytest.raises(ValidationError):
        _profile(rules=({
            "workspace_id": WORKSPACE_ID,
            "environment_id": ENVIRONMENT_ID,
            "command_identity": "qualification.issue561.parser",
            "boundary": IngestionFaultBoundary.PARSER,
            "reason_code": IngestionFaultReason.PARSER_RETRYABLE,
        },))


def test_fault_profile_accepts_exact_real_deployment_profile_identity() -> None:
    profile = _profile(
        deployment_profile_id="spine-local-qa",
        deployment_profile_version="v1",
    )

    assert profile.profile_id == "qualification-q16-02"
    assert profile.deployment_profile_id == "spine-local-qa"


def test_fault_controller_rejects_enabled_profile_outside_isolated_development() -> None:
    controller = IngestionFaultController(_profile())

    with pytest.raises(ValueError) as raised:
        controller.assert_qualification_deployment(isolated_development=False)

    assert str(raised.value) == "INGESTION_FAULT_PROFILE_FORBIDDEN"


def test_fault_profile_empty_compose_binding_keeps_injection_disabled() -> None:
    settings = IngestionFaultInjectionSettings(profile_json="")

    assert settings.load_profile() is None


def test_fault_profile_settings_loads_exact_real_deployment_binding() -> None:
    expected = _profile(
        deployment_profile_id="spine-local-qa",
        deployment_profile_version="v1",
    )

    loaded = IngestionFaultInjectionSettings(
        profile_json=expected.model_dump_json()
    ).load_profile()

    assert loaded == expected


def test_fault_profile_settings_redacts_malformed_profile() -> None:
    protected = "must-not-appear"

    with pytest.raises(ValueError) as raised:
        IngestionFaultInjectionSettings(
            profile_json=f'{{"unexpected":"{protected}"}}'
        ).load_profile()

    assert str(raised.value) == "INGESTION_FAULT_PROFILE_INVALID"
    assert protected not in str(raised.value)


def test_fault_controller_injects_once_for_bound_synthetic_command() -> None:
    controller = IngestionFaultController(_profile())

    controller.inject_if_bound(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        command_identity="api.document.upload." + "d" * 32,
        boundary=IngestionFaultBoundary.PARSER,
    )
    with pytest.raises(IngestionFaultInjected) as raised:
        controller.inject_if_bound(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            command_identity=PARSER_COMMAND,
            boundary=IngestionFaultBoundary.PARSER,
        )

    assert raised.value.reason_code is IngestionFaultReason.PARSER_RETRYABLE
    assert str(raised.value) == "parser_retryable"
    controller.inject_if_bound(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        command_identity=PARSER_COMMAND,
        boundary=IngestionFaultBoundary.PARSER,
    )


def test_fault_controller_isolates_workspace_and_environment() -> None:
    controller = IngestionFaultController(_profile())

    controller.inject_if_bound(
        workspace_id=UUID("10000000-0000-0000-0000-000000000003"),
        environment_id=ENVIRONMENT_ID,
        command_identity=PARSER_COMMAND,
        boundary=IngestionFaultBoundary.PARSER,
    )
    controller.inject_if_bound(
        workspace_id=WORKSPACE_ID,
        environment_id=UUID("20000000-0000-0000-0000-000000000004"),
        command_identity=PARSER_COMMAND,
        boundary=IngestionFaultBoundary.PARSER,
    )


@pytest.mark.parametrize(
    ("profile_id", "profile_version", "deployment_fingerprint"),
    (
        ("other-profile", "v1", "a" * 64),
        ("spine-local-qa", "v2", "a" * 64),
        ("spine-local-qa", "v1", "b" * 64),
    ),
)
def test_fault_controller_rejects_each_drifted_deployment_binding_without_values(
    profile_id: str,
    profile_version: str,
    deployment_fingerprint: str,
) -> None:
    controller = IngestionFaultController(
        _profile(
            deployment_profile_id="spine-local-qa",
            deployment_profile_version="v1",
        )
    )

    with pytest.raises(ValueError) as raised:
        controller.assert_deployment_binding(
            profile_id=profile_id,
            profile_version=profile_version,
            deployment_fingerprint=deployment_fingerprint,
        )

    assert str(raised.value) == "INGESTION_FAULT_PROFILE_MISMATCH"
    assert profile_id not in str(raised.value)


def test_fault_profile_allows_distinct_stage_commands_in_one_isolated_deployment() -> None:
    profile = _profile(
        rules=(
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "command_identity": PARSER_COMMAND,
                "boundary": IngestionFaultBoundary.PARSER,
                "reason_code": IngestionFaultReason.PARSER_RETRYABLE,
            },
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "command_identity": STORAGE_COMMAND,
                "boundary": IngestionFaultBoundary.ORIGINAL_STORAGE,
                "reason_code": IngestionFaultReason.ORIGINAL_STORAGE_UNAVAILABLE,
            },
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "command_identity": ACCEPTANCE_COMMAND,
                "boundary": IngestionFaultBoundary.PRE_ACCEPTANCE,
                "reason_code": IngestionFaultReason.PRE_ACCEPTANCE_RETRYABLE,
            },
        )
    )
    controller = IngestionFaultController(profile)

    for identity, boundary, reason in (
        (
            PARSER_COMMAND,
            IngestionFaultBoundary.PARSER,
            IngestionFaultReason.PARSER_RETRYABLE,
        ),
        (
            STORAGE_COMMAND,
            IngestionFaultBoundary.ORIGINAL_STORAGE,
            IngestionFaultReason.ORIGINAL_STORAGE_UNAVAILABLE,
        ),
        (
            ACCEPTANCE_COMMAND,
            IngestionFaultBoundary.PRE_ACCEPTANCE,
            IngestionFaultReason.PRE_ACCEPTANCE_RETRYABLE,
        ),
    ):
        with pytest.raises(IngestionFaultInjected) as raised:
            controller.inject_if_bound(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                command_identity=identity,
                boundary=boundary,
            )
        assert raised.value.reason_code is reason
