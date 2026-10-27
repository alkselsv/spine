"""Content-free operational snapshots for governed Google synchronization."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

GOOGLE_PROVIDER_RETRY_LIMIT = 3


class GoogleSyncDiagnosticSnapshot(BaseModel):
    """Allowlisted sync status; provider cursors and source content are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "google-sync-diagnostic:v1"
    correlation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    sync_run_id: UUID
    authorization_generation: StrictInt = Field(ge=1)
    state: Literal[
        "running",
        "succeeded",
        "resumable",
        "reconciliation_required",
        "suspended",
        "policy_blocked",
    ]
    failure_code: str | None = Field(default=None, max_length=64)
    retryable: StrictBool = False
    provider_retry_attempts: StrictInt = Field(ge=0, le=GOOGLE_PROVIDER_RETRY_LIMIT)
    provider_retry_limit: StrictInt = Field(ge=1, le=GOOGLE_PROVIDER_RETRY_LIMIT)
    page_count: StrictInt = Field(ge=0)
    item_count: StrictInt = Field(ge=0)
    accepted_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    quarantined_count: StrictInt = Field(ge=0)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "google-sync-diagnostic:v1":
            raise ValueError("unsupported Google sync diagnostic schema")
        return value

    @field_validator("failure_code")
    @classmethod
    def validate_failure_code(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
            raise ValueError("failure_code must be a safe identifier")
        return value

    @classmethod
    def from_run(
        cls,
        run: Any,
        pages: tuple[Any, ...],
        *,
        quarantined_count: int | None = None,
    ) -> "GoogleSyncDiagnosticSnapshot":
        items = tuple(item for page in pages for item in page.items)
        state = getattr(run.state, "value", run.state)
        failure_code = getattr(run, "failure_code", None)
        if failure_code is None:
            failure_code = getattr(run, "suspension_reason", None)
        if failure_code is None:
            failure_code = getattr(run, "reconciliation_reason", None)
        retryable = state == "resumable"
        return cls(
            correlation_id=run.sync_run_id,
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            connection_id=run.connection_id,
            sync_run_id=run.sync_run_id,
            authorization_generation=run.authorization_generation,
            state=state,
            failure_code=failure_code,
            retryable=retryable,
            provider_retry_attempts=getattr(run, "provider_retry_attempts", 0),
            provider_retry_limit=GOOGLE_PROVIDER_RETRY_LIMIT,
            page_count=len(pages),
            item_count=len(items),
            accepted_count=sum(_item_state(item) in {"succeeded", "reconciled"} for item in items),
            failed_count=sum(_item_state(item) == "failed" for item in items),
            quarantined_count=(
                sum(_item_state(item) == "quarantined" for item in items)
                if quarantined_count is None
                else quarantined_count
            ),
        )


def _item_state(item: Any) -> str | None:
    value = getattr(item, "state", None)
    return getattr(value, "value", value)


__all__ = ["GOOGLE_PROVIDER_RETRY_LIMIT", "GoogleSyncDiagnosticSnapshot"]
