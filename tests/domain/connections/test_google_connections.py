from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def test_google_connection_accepts_references_without_secret_values() -> None:
    connection = GoogleConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        version=1,
        workspace_id=_id(3),
        environment_id=_id(4),
        service_principal_id=_id(5),
        credential_reference=OpaqueGoogleReference(value="vault:google-r2-v1"),
        grant_reference=OpaqueGoogleReference(value="grant:google-r2-v1"),
        approved_resource_scope_references=(
            OpaqueGoogleReference(value="scope:estimate-register-v1"),
        ),
        authorization_generation=1,
        configuration_generation=1,
        status=GoogleConnectionStatus.ACTIVE,
    )

    payload = connection.model_dump(mode="json")
    assert payload["credential_reference"] == {"value": "vault:google-r2-v1"}
    assert "credential_value" not in payload
    assert "refresh_token" not in payload

    with pytest.raises(ValidationError):
        GoogleConnection.model_validate(
            {**payload, "refresh_token": "secret-refresh-token"}
        )


def test_google_connection_rejects_invalid_version_history() -> None:
    with pytest.raises(ValidationError, match="version 1"):
        GoogleConnection(
            connection_version_id=_id(1),
            connection_id=_id(2),
            version=1,
            predecessor_version_id=_id(9),
            workspace_id=_id(3),
            environment_id=_id(4),
            service_principal_id=_id(5),
            credential_reference=OpaqueGoogleReference(value="vault:google-r2-v1"),
            grant_reference=OpaqueGoogleReference(value="grant:google-r2-v1"),
            approved_resource_scope_references=(
                OpaqueGoogleReference(value="scope:estimate-register-v1"),
            ),
            authorization_generation=1,
            configuration_generation=1,
            status=GoogleConnectionStatus.ACTIVE,
        )
