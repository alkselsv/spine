"""Durably reconcile bounded configured Google Sheets ranges by business ID."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
)
from spine.application.connections.google import GoogleReadAdmission, GoogleReadCommand
from spine.application.google_drive_changes import DriveChangeCommand
from spine.application.persistence.drive_changes import DriveChangeItemRecord
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsIdentityDisposition,
    GoogleSheetsIdentityDispositionCommand,
    GoogleSheetsIdentityFailureCode,
    GoogleSheetsSyncItemState,
    GoogleSheetsSyncPage,
    GoogleSheetsSyncRepository,
    GoogleSheetsSyncRun,
    GoogleSheetsSyncRunCommand,
    GoogleSheetsSyncRunState,
)
from spine.connectors.observations import NormalizedConnectorObservation
from spine.domain.sources.google import GoogleRegisterRowKey
from spine.domain.connections.google import OpaqueGoogleReference


class GoogleSheetsPageObservation(Protocol):
    """Row observation exposed by the authorized Sheets provider port."""

    def to_normalized_observation(self) -> NormalizedConnectorObservation: ...


class GoogleSheetsProviderPageResult(Protocol):
    """Structural page contract consumed from an authorized Sheets read."""

    spreadsheet_id: str
    sheet_id: int
    a1_range: str
    page_token: str | None
    next_page_token: str | None
    complete: bool
    provider_version_before: str
    provider_version_after: str
    observations: tuple[GoogleSheetsPageObservation, ...]
    identity_rejection_codes: tuple[str, ...]


class GoogleSheetsReconciliationScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spreadsheet_id: str = Field(min_length=1, max_length=1024)
    sheet_id: int = Field(ge=0)
    a1_range: str = Field(min_length=1, max_length=1024)

    @property
    def identity(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"google-sheets-scope:v1:{hashlib.sha256(encoded).hexdigest()}"


class GoogleSheetsReconciliationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    access_policy_version_id: UUID
    command_identity: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    prior_committed_cursor: str | None = Field(default=None, max_length=4096)
    proposed_cursor: str | None = Field(default=None, max_length=4096)
    trigger: str = Field(pattern=r"^(drive_wakeup|scheduled_full)$")
    scopes: tuple[GoogleSheetsReconciliationScope, ...] = Field(
        min_length=1, max_length=128
    )
    max_pages: int = Field(default=100, gt=0, le=10_000)
    max_rows: int = Field(default=20_000, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_command(self) -> "GoogleSheetsReconciliationCommand":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "service_principal_id",
            "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if len({scope.identity for scope in self.scopes}) != len(self.scopes):
            raise ValueError("configured Google Sheets ranges must be unique")
        return self


class GoogleSheetsReconciliationPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_identity: str = Field(min_length=1, max_length=1024)
    page_token: str | None = Field(default=None, max_length=4096)
    next_page_token: str | None = Field(default=None, max_length=4096)
    provider_version_before: str = Field(min_length=1, max_length=128)
    provider_version_after: str = Field(min_length=1, max_length=128)
    complete: bool
    observations: tuple[NormalizedConnectorObservation, ...] = Field(max_length=4096)
    identity_rejection_codes: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def provider_version(self) -> str:
        return self.provider_version_after


class GoogleSheetsReconciliationReader(Protocol):
    async def read_page(
        self,
        command: GoogleSheetsReconciliationCommand,
        scope: GoogleSheetsReconciliationScope,
        page_token: str | None,
        context: TrustedPersistenceContext,
    ) -> GoogleSheetsReconciliationPage: ...


@dataclass(frozen=True, slots=True)
class GoogleSheetsAdmittedPageReader:
    """Authorize every configured range/page before its provider request."""

    admission: GoogleReadAdmission[GoogleSheetsProviderPageResult]

    async def read_page(
        self,
        command: GoogleSheetsReconciliationCommand,
        scope: GoogleSheetsReconciliationScope,
        page_token: str | None,
        context: TrustedPersistenceContext,
    ) -> GoogleSheetsReconciliationPage:
        provider_page = await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
                resource_object_reference=scope.spreadsheet_id,
                a1_range=scope.a1_range,
                provider_page_token=page_token,
                sheet_id=scope.sheet_id,
            ),
            context,
        )
        if (
            provider_page.spreadsheet_id != scope.spreadsheet_id
            or provider_page.sheet_id != scope.sheet_id
            or provider_page.a1_range != scope.a1_range
            or provider_page.page_token != page_token
        ):
            raise ValueError("admitted Sheets page does not match the requested scope")
        return GoogleSheetsReconciliationPage(
            scope_identity=scope.identity,
            page_token=provider_page.page_token,
            next_page_token=provider_page.next_page_token,
            provider_version_before=provider_page.provider_version_before,
            provider_version_after=provider_page.provider_version_after,
            complete=provider_page.complete,
            identity_rejection_codes=provider_page.identity_rejection_codes,
            observations=tuple(
                observation.to_normalized_observation()
                for observation in provider_page.observations
            ),
        )


class GoogleSheetsCanonicalIngestionPort(Protocol):
    async def ingest(
        self,
        observation: NormalizedConnectorObservation,
        *,
        command_identity: str,
        idempotency_key: str,
    ) -> UUID: ...


@dataclass(frozen=True, slots=True)
class ConnectorGoogleSheetsCanonicalIngestion:
    """Send each fully read row through Spine's existing canonical handoff."""

    orchestrator: ConnectorIngestionOrchestrator
    base_command: ConnectorIngestionCommand

    async def ingest(
        self,
        observation: NormalizedConnectorObservation,
        *,
        command_identity: str,
        idempotency_key: str,
    ) -> UUID:
        command = self.base_command.model_copy(
            update={
                "command_identity": command_identity,
                "idempotency_key": idempotency_key,
            }
        )
        result = await self.orchestrator.ingest_observation(command, observation)
        return result.revision.revision_id


class GoogleSheetsReconciliationFenceError(RuntimeError):
    """A resumed reconciliation no longer matches its captured authority."""


@dataclass(frozen=True, slots=True)
class GoogleSheetsReconciliationResult:
    sync_run_id: UUID
    state: GoogleSheetsSyncRunState
    pages: tuple[GoogleSheetsSyncPage, ...]
    accepted_rows: int
    failure_code: str | None
    committed_cursor: str | None


@dataclass(frozen=True, slots=True)
class GoogleSheetsReconciliationCoordinator:
    repository: GoogleSheetsSyncRepository
    reader: GoogleSheetsReconciliationReader
    ingestion: GoogleSheetsCanonicalIngestionPort
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def run(
        self, command: GoogleSheetsReconciliationCommand
    ) -> GoogleSheetsReconciliationResult:
        sync_run_id = _sync_run_id(command)
        run = await self.repository.resolve_run(sync_run_id)
        if run is None:
            run = await self.repository.start_run(
                GoogleSheetsSyncRunCommand(
                    sync_run_id=sync_run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    resource_scope_reference=command.resource_scope_reference,
                    service_principal_id=command.service_principal_id,
                    access_policy_version_id=command.access_policy_version_id,
                    authorization_generation=command.authorization_generation,
                    configuration_generation=command.configuration_generation,
                    scope_set_digest=_scope_set_digest(command),
                    command_identity=command.command_identity,
                    idempotency_key=command.idempotency_key,
                    prior_committed_cursor=command.prior_committed_cursor,
                    proposed_cursor=command.proposed_cursor,
                    requested_at=self._now(),
                )
            )
        self._assert_fence(run, command)
        if run.state is GoogleSheetsSyncRunState.SUCCEEDED:
            return await self._result(
                run,
                accepted_rows=sum(
                    len(p.items) for p in await self.repository.list_pages(sync_run_id)
                ),
            )

        # Read and guard the entire configured scope before any canonical effect.
        candidates: list[
            tuple[GoogleSheetsReconciliationPage, NormalizedConnectorObservation]
        ] = []
        read_pages: list[GoogleSheetsReconciliationPage] = []
        page_counts: dict[str, int] = {}
        versions: dict[str, str] = {}
        seen_identities: dict[str, str] = {}
        identity_rejections: dict[str, GoogleSheetsIdentityFailureCode] = {}
        try:
            for scope in command.scopes:
                token: str | None = None
                visited: set[str] = set()
                while True:
                    page_counts[scope.identity] = page_counts.get(scope.identity, 0) + 1
                    if sum(page_counts.values()) > command.max_pages:
                        raise _IncompleteRead("sheets_page_limit")
                    context = self.context_factory(
                        PersistenceOperation("google_sheets_reconciliation_read"),
                        uuid5(
                            NAMESPACE_URL,
                            f"{sync_run_id}:{scope.identity}:{token or '__initial__'}",
                        ),
                    )
                    page = await self.reader.read_page(command, scope, token, context)
                    if (
                        page.scope_identity != scope.identity
                        or page.page_token != token
                    ):
                        raise _IncompleteRead("sheets_scope_or_page_mismatch")
                    if not page.complete:
                        raise _IncompleteRead("sheets_incomplete_page")
                    if page.provider_version_before != page.provider_version_after:
                        raise _IncompleteRead("sheets_version_changed")
                    read_pages.append(page)
                    if page.identity_rejection_codes:
                        identity_rejections.setdefault(
                            scope.identity,
                            _safe_identity_failure_code(
                                page.identity_rejection_codes[0]
                            ),
                        )
                    if page.next_page_token is not None:
                        if (
                            page.next_page_token in visited
                            or page.next_page_token == token
                        ):
                            raise _IncompleteRead("sheets_pagination_cycle")
                        visited.add(page.next_page_token)
                    previous_version = versions.setdefault(
                        scope.spreadsheet_id, page.provider_version
                    )
                    if previous_version != page.provider_version:
                        raise _IncompleteRead("sheets_version_changed")
                    for observation in page.observations:
                        _validate_observation(
                            command, scope, observation, page.provider_version
                        )
                        identity = observation.external_identity.external_object_id
                        previous_scope = seen_identities.get(identity)
                        if previous_scope is not None:
                            duplicate_code = (
                                GoogleSheetsIdentityFailureCode.SCOPE_DUPLICATE_BUSINESS_ID
                            )
                            identity_rejections.setdefault(scope.identity, duplicate_code)
                            identity_rejections.setdefault(previous_scope, duplicate_code)
                        else:
                            seen_identities[identity] = scope.identity
                        candidates.append((page, observation))
                    if len(candidates) > command.max_rows:
                        raise _IncompleteRead("sheets_row_limit")
                    if page.next_page_token is None:
                        break
                    token = page.next_page_token
        except _IncompleteRead as error:
            current = await self.repository.mark_run(
                sync_run_id,
                state=GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED,
                failure_code=error.code,
                now=self._now(),
            )
            return await self._result(current, accepted_rows=0)
        except Exception:
            current = await self.repository.mark_run(
                sync_run_id,
                state=GoogleSheetsSyncRunState.RESUMABLE,
                failure_code="sheets_read_failed",
                now=self._now(),
            )
            return await self._result(current, accepted_rows=0)

        if identity_rejections:
            for scope_identity, failure_code in sorted(identity_rejections.items()):
                await self.repository.record_identity_disposition(
                    GoogleSheetsIdentityDispositionCommand(
                        sync_run_id=sync_run_id,
                        workspace_id=command.workspace_id,
                        environment_id=command.environment_id,
                        connection_id=command.connection_id,
                        scope_identity=scope_identity,
                        disposition=GoogleSheetsIdentityDisposition.QUARANTINED,
                        failure_code=failure_code,
                        now=self._now(),
                    )
                )
            current = await self.repository.mark_run(
                sync_run_id,
                state=GoogleSheetsSyncRunState.RECONCILIATION_REQUIRED,
                failure_code="sheets_identity_quarantined",
                now=self._now(),
            )
            return await self._result(current, accepted_rows=0)

        stored_pages: dict[tuple[str, str | None], GoogleSheetsSyncPage] = {}
        try:
            for page in _unique_pages(read_pages):
                identities = tuple(
                    obs.external_identity.external_object_id
                    for candidate, obs in candidates
                    if (candidate.scope_identity, candidate.page_token)
                    == (page.scope_identity, page.page_token)
                )
                stored = await self.repository.open_page(
                    sync_run_id=sync_run_id,
                    scope_identity=page.scope_identity,
                    page_token=page.page_token,
                    next_page_token=page.next_page_token,
                    provider_version=page.provider_version,
                    identities=identities,
                    now=self._now(),
                )
                stored_pages[(page.scope_identity, page.page_token)] = stored
        except Exception:
            current = await self.repository.mark_run(
                sync_run_id,
                state=GoogleSheetsSyncRunState.RESUMABLE,
                failure_code="checkpoint_write_failed",
                now=self._now(),
            )
            return await self._result(current, accepted_rows=0)

        accepted = 0
        for page, observation in candidates:
            stored = stored_pages[(page.scope_identity, page.page_token)]
            identity = observation.external_identity.external_object_id
            item = next(item for item in stored.items if item.identity == identity)
            if item.state is GoogleSheetsSyncItemState.SUCCEEDED:
                accepted += 1
                continue
            key = _row_idempotency_key(command.idempotency_key, identity)
            try:
                revision_id = await self.ingestion.ingest(
                    observation,
                    command_identity=_row_command_identity(
                        command.command_identity, identity
                    ),
                    idempotency_key=key,
                )
                await self.repository.mark_item_succeeded(
                    item.item_id, revision_id=revision_id, now=self._now()
                )
                accepted += 1
            except Exception:
                current = await self.repository.mark_run(
                    sync_run_id,
                    state=GoogleSheetsSyncRunState.RESUMABLE,
                    failure_code="canonical_ingestion_failed",
                    now=self._now(),
                )
                return await self._result(current, accepted_rows=accepted)
        for stored in stored_pages.values():
            await self.repository.complete_page(stored.page_id, now=self._now())
        for scope in command.scopes:
            await self.repository.record_identity_disposition(
                GoogleSheetsIdentityDispositionCommand(
                    sync_run_id=sync_run_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    scope_identity=scope.identity,
                    disposition=GoogleSheetsIdentityDisposition.CLEARED,
                    failure_code=None,
                    now=self._now(),
                )
            )
        current = await self.repository.mark_run(
            sync_run_id,
            state=GoogleSheetsSyncRunState.SUCCEEDED,
            failure_code=None,
            now=self._now(),
        )
        return await self._result(current, accepted_rows=accepted)

    async def _result(
        self, run: GoogleSheetsSyncRun, *, accepted_rows: int
    ) -> GoogleSheetsReconciliationResult:
        return GoogleSheetsReconciliationResult(
            sync_run_id=run.sync_run_id,
            state=run.state,
            pages=await self.repository.list_pages(run.sync_run_id),
            accepted_rows=accepted_rows,
            failure_code=run.failure_code,
            committed_cursor=(
                run.proposed_cursor
                if run.state is GoogleSheetsSyncRunState.SUCCEEDED
                and run.proposed_cursor is not None
                else run.prior_committed_cursor
            ),
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Google Sheets reconciliation clock must be timezone-aware"
            )
        return value

    @staticmethod
    def _assert_fence(
        run: GoogleSheetsSyncRun, command: GoogleSheetsReconciliationCommand
    ) -> None:
        if run.scope_set_digest != _scope_set_digest(command):
            raise GoogleSheetsReconciliationFenceError(
                "Google Sheets reconciliation is fenced by changed configured ranges"
            )
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "service_principal_id",
            "access_policy_version_id",
            "authorization_generation",
            "configuration_generation",
            "prior_committed_cursor",
            "proposed_cursor",
        ):
            if getattr(run, name) != getattr(command, name):
                raise GoogleSheetsReconciliationFenceError(
                    "Google Sheets reconciliation is fenced by changed authority or scope"
                )


class _IncompleteRead(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code


def _safe_identity_failure_code(value: str) -> GoogleSheetsIdentityFailureCode:
    try:
        return GoogleSheetsIdentityFailureCode(value)
    except ValueError:
        return GoogleSheetsIdentityFailureCode.INVALID_IDENTITY


class GoogleSheetsDriveScopeResolver(Protocol):
    async def resolve_approved_scopes(
        self, command: DriveChangeCommand, spreadsheet_id: str
    ) -> tuple[GoogleSheetsReconciliationScope, ...] | None: ...


@dataclass(frozen=True, slots=True)
class GoogleSheetsDriveWakeupAdapter:
    """Turn a configured spreadsheet Drive change into a bounded Sheets run."""

    coordinator: GoogleSheetsReconciliationCoordinator
    scopes: GoogleSheetsDriveScopeResolver

    async def reconcile_change(
        self, command: DriveChangeCommand, item: DriveChangeItemRecord
    ) -> UUID | None:
        configured_scopes = await self.scopes.resolve_approved_scopes(
            command, item.file_id
        )
        if configured_scopes is None:
            return None
        if not configured_scopes:
            raise ValueError("approved Sheets scope set cannot be empty")
        change_digest = hashlib.sha256(item.change_key.encode("utf-8")).hexdigest()[:32]
        wakeup = GoogleSheetsReconciliationCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            resource_scope_reference=command.resource_scope_reference,
            service_principal_id=command.service_principal_id,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            access_policy_version_id=command.access_policy_version_id,
            command_identity="google.sheets.drive-wakeup",
            idempotency_key=f"{command.idempotency_key}:sheet:{change_digest}"[:255],
            prior_committed_cursor=command.last_committed_cursor,
            proposed_cursor=None,
            trigger="drive_wakeup",
            scopes=configured_scopes,
        )
        result = await self.coordinator.run(wakeup)
        if result.state is not GoogleSheetsSyncRunState.SUCCEEDED:
            raise RuntimeError("Google Sheets change reconciliation did not complete")
        return result.sync_run_id


def _validate_observation(
    command: GoogleSheetsReconciliationCommand,
    scope: GoogleSheetsReconciliationScope,
    observation: NormalizedConnectorObservation,
    version: str,
) -> None:
    if (
        observation.workspace_id != command.workspace_id
        or observation.environment_id != command.environment_id
        or observation.connection_id != command.connection_id
        or observation.resource_scope_reference != command.resource_scope_reference
        or observation.authorization_generation != command.authorization_generation
        or observation.configuration_generation != command.configuration_generation
    ):
        raise _IncompleteRead("sheets_observation_scope_mismatch")
    key = observation.external_identity.provider_key
    if not isinstance(key, GoogleRegisterRowKey):
        raise _IncompleteRead("sheets_observation_identity_mismatch")
    if key.spreadsheet_id != scope.spreadsheet_id or key.sheet_id != scope.sheet_id:
        raise _IncompleteRead("sheets_observation_scope_mismatch")
    if (
        observation.provider_version_before != version
        or observation.provider_version_after != version
    ):
        raise _IncompleteRead("sheets_version_changed")


def _unique_pages(
    pages: list[GoogleSheetsReconciliationPage],
) -> tuple[GoogleSheetsReconciliationPage, ...]:
    result: list[GoogleSheetsReconciliationPage] = []
    seen = set()
    for page in pages:
        key = (page.scope_identity, page.page_token)
        if key not in seen:
            seen.add(key)
            result.append(page)
    return tuple(result)


def _sync_run_id(command: GoogleSheetsReconciliationCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:google-sheets-sync:v1:{command.workspace_id}:{command.environment_id}:{command.connection_id}:{command.idempotency_key}",
    )


def _scope_set_digest(command: GoogleSheetsReconciliationCommand) -> str:
    scopes = sorted(
        (scope.model_dump(mode="json") for scope in command.scopes),
        key=lambda scope: (
            scope["spreadsheet_id"],
            scope["sheet_id"],
            scope["a1_range"],
        ),
    )
    return hashlib.sha256(
        json.dumps(scopes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _row_idempotency_key(prefix: str, identity: str) -> str:
    return f"{prefix}:row:{hashlib.sha256(identity.encode()).hexdigest()[:32]}"[:255]


def _row_command_identity(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{prefix[:50]}.r{digest}"[:64]


__all__ = [
    "ConnectorGoogleSheetsCanonicalIngestion",
    "GoogleSheetsCanonicalIngestionPort",
    "GoogleSheetsReconciliationCommand",
    "GoogleSheetsReconciliationCoordinator",
    "GoogleSheetsReconciliationFenceError",
    "GoogleSheetsReconciliationPage",
    "GoogleSheetsReconciliationReader",
    "GoogleSheetsAdmittedPageReader",
    "GoogleSheetsReconciliationResult",
    "GoogleSheetsReconciliationScope",
    "GoogleSheetsDriveScopeResolver",
    "GoogleSheetsDriveWakeupAdapter",
]
