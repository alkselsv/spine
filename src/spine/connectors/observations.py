"""Google v1 normalized connector observation compatibility contract."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBytes, StrictInt, model_validator

from spine.domain.connections.bitrix24_disk_scope import Bitrix24DiskMembershipEvidence
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources.google import GoogleSourceIdentity


CONNECTOR_OBSERVATION_SCHEMA = "connector-observation:v1"
DigestHex = Annotated[str, Field(pattern=r"[0-9a-f]{64}")]
BoundedIdentifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
]


class ConnectorCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class NormalizedConnectorObservation(DefinitionModel):
    """One authorized Google v1 observation ready for canonical ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[CONNECTOR_OBSERVATION_SCHEMA] = CONNECTOR_OBSERVATION_SCHEMA
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    source_kind: BoundedIdentifier
    external_identity: GoogleSourceIdentity
    original_bytes: StrictBytes
    media_type: str = Field(min_length=1, max_length=255)
    extraction_profile: VersionIdentifier
    locator_contract_version: VersionIdentifier
    coverage: ConnectorCoverage
    producer_reference: BoundedIdentifier
    event_identity: BoundedIdentifier
    event_digest: DigestHex
    order_scheme: VersionIdentifier | None = None
    order_token: BoundedIdentifier | None = None
    origin_locator_kind: BoundedIdentifier | None = None
    origin_locator_value: BoundedIdentifier | None = None
    origin_locator_schema: VersionIdentifier | None = None
    origin_locator_digest: DigestHex | None = None
    observer_service: BoundedIdentifier
    received_at: datetime
    observed_at: datetime
    provider_media_type: str | None = Field(default=None, max_length=255)
    provider_version_before: str | None = Field(default=None, min_length=1, max_length=128)
    provider_version_after: str | None = Field(default=None, min_length=1, max_length=128)
    scope_membership: Bitrix24DiskMembershipEvidence | None = None

    @model_validator(mode="after")
    def validate_scope_and_provenance(self) -> "NormalizedConnectorObservation":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.external_identity.provider_key.connection_id != self.connection_id:
            raise ValueError("external identity connection does not match observation")
        validate_observation_provenance(
            order_scheme=self.order_scheme,
            order_token=self.order_token,
            provider_version_before=self.provider_version_before,
            provider_version_after=self.provider_version_after,
            received_at=self.received_at,
            observed_at=self.observed_at,
        )
        return self


def validate_observation_provenance(
    *,
    order_scheme: str | None,
    order_token: str | None,
    provider_version_before: str | None,
    provider_version_after: str | None,
    received_at: datetime,
    observed_at: datetime,
) -> None:
    if (order_scheme is None) != (order_token is None):
        raise ValueError("order scheme and token must be supplied together")
    if (provider_version_before is None) != (provider_version_after is None):
        raise ValueError("provider version guard must contain both versions")
    if (
        provider_version_before is not None
        and provider_version_before != provider_version_after
    ):
        raise ValueError("provider version guard must be stable")
    for value in (received_at, observed_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation timestamps must be timezone-aware")
    if received_at < observed_at:
        raise ValueError("received_at cannot precede observed_at")


__all__ = [
    "BoundedIdentifier",
    "CONNECTOR_OBSERVATION_SCHEMA",
    "ConnectorCoverage",
    "DigestHex",
    "NormalizedConnectorObservation",
    "validate_observation_provenance",
]
