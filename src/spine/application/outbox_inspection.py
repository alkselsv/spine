"""Read-only, tenant-scoped inspection of quarantined outbox deliveries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.persistence.context import TrustedPersistenceContext


class OutboxDeliveryInspectionItem(BaseModel):
    """Allowlisted operational metadata for one quarantined delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    workspace_id: UUID
    environment_id: UUID | None
    status: Literal["quarantined"]
    attempt_count: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    quarantined_at: datetime
    last_error_code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    correlation_id: UUID | None = None


class OutboxDeliveryInspectionPage(BaseModel):
    """One bounded page. ``partial`` means more tenant-visible rows exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["empty", "complete", "partial", "unavailable"]
    items: tuple[OutboxDeliveryInspectionItem, ...] = ()
    has_more: bool = False

    @model_validator(mode="after")
    def validate_state(self) -> "OutboxDeliveryInspectionPage":
        if self.state == "empty" and (self.items or self.has_more):
            raise ValueError("empty inspection page cannot contain results")
        if self.state == "complete" and (not self.items or self.has_more):
            raise ValueError("complete inspection page must contain a final result page")
        if self.state == "partial" and (not self.items or not self.has_more):
            raise ValueError("partial inspection page must contain a continuation")
        if self.state == "unavailable" and (self.items or self.has_more):
            raise ValueError("unavailable inspection page cannot contain results")
        return self


class OutboxDeliveryInspectionService(Protocol):
    """Administrative read port; it has no delivery mutation operations."""

    async def list_quarantined(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> OutboxDeliveryInspectionPage: ...


class UnavailableOutboxDeliveryInspectionService:
    """Safe fallback when tenant-scoped runtime inspection is not composed."""

    async def list_quarantined(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> OutboxDeliveryInspectionPage:
        del context, limit, after
        return OutboxDeliveryInspectionPage(state="unavailable")


__all__ = [
    "OutboxDeliveryInspectionItem",
    "OutboxDeliveryInspectionPage",
    "OutboxDeliveryInspectionService",
    "UnavailableOutboxDeliveryInspectionService",
]
