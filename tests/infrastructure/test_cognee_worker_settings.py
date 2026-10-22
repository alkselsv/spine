from __future__ import annotations

import json

import pytest

from spine.infrastructure.cognee_worker_settings import (
    CogneeWorkerBindingSettings,
    CogneeWorkerConfigurationError,
)
from spine.memory.cognee.worker_profile import CogneeWorkerProfile

from tests.memory.cognee.test_projection_profile_binding import (
    approval,
    runtime_identity,
)
from tests.memory.cognee.worker_profile_fixtures import cognee_worker_profile_values


def binding_payload() -> dict[str, object]:
    values = cognee_worker_profile_values()
    values["profile_digest"] = CogneeWorkerProfile.digest_for(values)
    selected_profile = CogneeWorkerProfile.model_validate(values)
    return {
        "schema_version": "cognee-projection-binding:v1",
        "profile": selected_profile.model_dump(mode="json"),
        "approval": approval(selected_profile).model_dump(mode="json"),
        "runtime_identity": runtime_identity().model_dump(mode="json"),
        "deployment_id": selected_profile.deployment_id,
        "release_id": selected_profile.release_id,
    }


def test_worker_binding_settings_load_strict_versioned_configuration() -> None:
    settings = CogneeWorkerBindingSettings(
        binding_json=json.dumps(binding_payload())
    )

    configuration = settings.load_configuration()

    assert configuration.profile.profile_id == "cognee-approved-production"
    assert configuration.approval.approved is True
    assert configuration.runtime_identity.llm_model == "openai/gpt-5.4-nano"


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        (None, "profile_binding_missing"),
        ({**binding_payload(), "unexpected": "secret-value"}, "profile_binding_invalid"),
        (
            {
                **binding_payload(),
                "runtime_identity": {
                    **binding_payload()["runtime_identity"],
                    "llm_model": "openai/fallback-model",
                },
            },
            "runtime_identity_mismatch",
        ),
    ],
)
def test_worker_binding_settings_fail_closed_with_bounded_reason(
    payload: dict[str, object] | None,
    reason_code: str,
) -> None:
    settings = CogneeWorkerBindingSettings(
        binding_json=None if payload is None else json.dumps(payload)
    )

    with pytest.raises(CogneeWorkerConfigurationError) as error:
        settings.load_configuration()

    assert error.value.reason_code == reason_code
    assert "secret-value" not in str(error.value)
