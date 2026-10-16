"""Tenant runtime-role reader for safe quarantined delivery metadata."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from spine.application.outbox_inspection import (
    OutboxDeliveryInspectionItem,
    OutboxDeliveryInspectionPage,
)
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.infrastructure.db.engine import SessionFactory


_BIND_CONTEXT = text(
    "SELECT "
    "set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true), "
    "set_config('spine.acting_subject_id', :acting_subject_id, true), "
    "set_config('spine.service_principal_id', :service_principal_id, true), "
    "set_config('spine.purpose', :purpose, true), "
    "set_config('spine.operation', :operation, true), "
    "set_config('spine.trace_id', :trace_id, true)"
)
_LIST_QUARANTINED = text(
    "SELECT event_id, workspace_id, environment_id, status, attempt_count, "
    "created_at, updated_at, quarantined_at, last_error_code, correlation_id "
    "FROM spine.outbox_deliveries "
    "WHERE workspace_id = :workspace_id "
    "AND environment_id = :environment_id "
    "AND status = 'quarantined' "
    "ORDER BY quarantined_at DESC, event_id ASC LIMIT :fetch_limit"
)
_LIST_QUARANTINED_AFTER = text(
    "SELECT event_id, workspace_id, environment_id, status, attempt_count, "
    "created_at, updated_at, quarantined_at, last_error_code, correlation_id "
    "FROM spine.outbox_deliveries "
    "WHERE workspace_id = :workspace_id "
    "AND environment_id = :environment_id "
    "AND status = 'quarantined' "
    "AND (quarantined_at < :after_time "
    "OR (quarantined_at = :after_time AND event_id > :after_event_id)) "
    "ORDER BY quarantined_at DESC, event_id ASC LIMIT :fetch_limit"
)


class PostgreSQLOutboxDeliveryInspection:
    """Read a fixed column set using a verified interactive tenant context."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    async def list_quarantined(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> OutboxDeliveryInspectionPage:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        snapshot = self._context_verifier.verify(context)
        if (
            not isinstance(snapshot.scope, EnvironmentScope)
            or snapshot.origin is not ContextOrigin.INTERACTIVE
            or snapshot.purpose.value != "outbox_inspection"
            or snapshot.operation.value != "inspect_quarantined_deliveries"
        ):
            raise PermissionError("outbox inspection is denied")

        session = None
        try:
            session = self._session_factory()
            async with session.begin():
                await session.execute(
                    _BIND_CONTEXT,
                    {
                        "workspace_id": str(snapshot.scope.workspace_id),
                        "environment_id": str(snapshot.scope.environment_id),
                        "acting_subject_id": str(snapshot.acting_subject_id),
                        "service_principal_id": (
                            str(snapshot.service_principal_id)
                            if snapshot.service_principal_id is not None
                            else ""
                        ),
                        "purpose": snapshot.purpose.value,
                        "operation": snapshot.operation.value,
                        "trace_id": str(snapshot.trace_id),
                    },
                )
                parameters = {
                    "workspace_id": str(snapshot.scope.workspace_id),
                    "environment_id": str(snapshot.scope.environment_id),
                    "fetch_limit": limit + 1,
                }
                query = _LIST_QUARANTINED
                if after is not None:
                    query = _LIST_QUARANTINED_AFTER
                    parameters.update(
                        {"after_time": after[0], "after_event_id": after[1]}
                    )
                result = await session.execute(query, parameters)
                rows = result.mappings().all()
            has_more = len(rows) > limit
            items = tuple(
                OutboxDeliveryInspectionItem.model_validate(dict(row))
                for row in rows[:limit]
            )
            state = "empty" if not items else "partial" if has_more else "complete"
            return OutboxDeliveryInspectionPage(
                state=state,
                items=items,
                has_more=has_more,
            )
        except asyncio.CancelledError:
            raise
        except PermissionError:
            raise
        except Exception:
            return OutboxDeliveryInspectionPage(state="unavailable")
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass


__all__ = ["PostgreSQLOutboxDeliveryInspection"]
