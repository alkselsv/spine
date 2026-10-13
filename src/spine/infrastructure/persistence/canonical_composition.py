"""Production composition seam for canonical source acceptance."""

from __future__ import annotations

from collections.abc import Callable

from spine.application.canonical_acceptance import (
    CanonicalAcceptanceCoordinator,
    build_canonical_acceptance_coordinator,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.infrastructure.persistence.canonical_audit import (
    UnitOfWorkCanonicalAcceptanceFailedAttemptAudit,
    build_canonical_failed_attempt_audit_event,
)


def build_production_canonical_acceptance_coordinator(
    *,
    uow_factory: UnitOfWorkFactory,
    context_factory: Callable[
        [CanonicalAcceptanceCommand], TrustedPersistenceContext
    ],
) -> CanonicalAcceptanceCoordinator:
    """Return the fully audited canonical acceptance application entry point."""

    failed_attempt_audit = UnitOfWorkCanonicalAcceptanceFailedAttemptAudit(
        uow_factory=uow_factory,
        context_factory=context_factory,
        event_factory=build_canonical_failed_attempt_audit_event,
    )
    return build_canonical_acceptance_coordinator(
        uow_factory=uow_factory,
        context_factory=context_factory,
        failed_attempt_audit=failed_attempt_audit,
    )


__all__ = ["build_production_canonical_acceptance_coordinator"]
