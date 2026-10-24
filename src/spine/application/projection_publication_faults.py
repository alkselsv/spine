"""Qualification-only fault boundary contracts for projection publication."""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID

from spine.memory.contracts import ProjectionCommand


class ProjectionPublicationFaultBoundary(str, Enum):
    BEFORE_PHYSICAL_VALIDATION = "before_physical_validation"
    AFTER_VALIDATION_BEFORE_ACTIVATION = "after_validation_before_activation"


class ProjectionPublicationFaultReason(str, Enum):
    BEFORE_PHYSICAL_VALIDATION = (
        "projection.qualification.before_physical_validation"
    )
    AFTER_VALIDATION_BEFORE_ACTIVATION = (
        "projection.qualification.after_validation_before_activation"
    )


class ProjectionPublicationFaultInjected(RuntimeError):
    """One bounded qualification fault was durably consumed."""

    def __init__(
        self,
        reason: ProjectionPublicationFaultReason,
        *,
        audit_event_id: UUID,
    ) -> None:
        self.reason = reason
        self.reason_code = reason.value
        self.audit_event_id = audit_event_id
        super().__init__(reason.value)


class ProjectionPublicationFaultRejected(RuntimeError):
    """The enabled profile does not authorize this successor command."""

    reason_code = "projection.qualification.binding_rejected"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


class ProjectionPublicationQualificationGuard(Protocol):
    def assert_command_allowed(self, command: ProjectionCommand) -> None: ...


class ProjectionPublicationFaultInjector(
    ProjectionPublicationQualificationGuard, Protocol
):
    async def inject_if_bound(
        self,
        command: ProjectionCommand,
        boundary: ProjectionPublicationFaultBoundary,
    ) -> None: ...


__all__ = [
    "ProjectionPublicationFaultBoundary",
    "ProjectionPublicationFaultInjected",
    "ProjectionPublicationFaultInjector",
    "ProjectionPublicationFaultReason",
    "ProjectionPublicationFaultRejected",
    "ProjectionPublicationQualificationGuard",
]
