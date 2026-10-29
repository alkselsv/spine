"""Deterministic provider fake used by the R2 connection control-plane slice."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from spine.application.connections.management import ProviderRuntimeObservation
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderDiagnosticCode,
)


class FakeProviderConnectionObserver:
    """Return content-free qualification and health signals without network I/O."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    async def inspect_approved(
        self, approval: ProviderConnectionApproval
    ) -> ProviderRuntimeObservation:
        return self._observation(approval.approval_id)

    async def inspect_connection(
        self, connection: ProviderConnection
    ) -> ProviderRuntimeObservation:
        return self._observation(connection.approval_id)

    def _observation(self, qualification_reference) -> ProviderRuntimeObservation:
        return ProviderRuntimeObservation(
            capabilities=(
                ProviderCapability(
                    kind=ProviderCapabilityKind.HEALTH_DIAGNOSTICS,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=OpaqueProviderReference(
                        value=qualification_reference
                    ),
                ),
            ),
            health=ProviderConnectionHealth.HEALTHY,
            diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
            observed_at=self._clock(),
        )


__all__ = ["FakeProviderConnectionObserver"]
