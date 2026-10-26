"""Versioned, provider-neutral connector observations.

The provider adapter produces this contract after read admission.  The
application ingestion seam consumes it without importing a provider SDK or a
memory backend.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBytes, StrictInt, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources.google import GoogleSourceIdentity


CONNECTOR_OBSERVATION_SCHEMA = "connector-observation:v1"
DigestHex = Annotated[str, Field(pattern=r"[0-9a-f]{64}")]
BoundedIdentifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
]


class ConnectorCoverage(str, Enum):
    """Coverage of the normalized provider observation."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class NormalizedConnectorObservation(DefinitionModel):
    """One authorized external observation ready for canonical ingestion.

    ``original_bytes`` are the exact normalized representation selected by the
    connector.  The immutable storage reference is intentionally assigned by
    the existing original-upload boundary, never accepted from the provider.
    """

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
        provider_key = self.external_identity.provider_key
        if provider_key.connection_id != self.connection_id:
            raise ValueError("external identity connection does not match observation")
        if (self.order_scheme is None) != (self.order_token is None):
            raise ValueError("order scheme and token must be supplied together")
        for value in (self.received_at, self.observed_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("observation timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at cannot precede observed_at")
        return self


__all__ = [
    "CONNECTOR_OBSERVATION_SCHEMA",
    "ConnectorCoverage",
    "NormalizedConnectorObservation",
]
