"""Strictly opt-in live qualification for the governed Google adapter.

The harness is deliberately separate from ordinary connector execution and CI.
Its report is metadata-only: provider payloads, source content, credentials and
raw exception messages are never serialized.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import hashlib
import time
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, StrictStr, model_validator

from spine.application.connections.google import (
    GoogleConnectionRepository,
    GoogleConnectionError,
    GoogleProviderContractError,
    GoogleProviderReadError,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.diagnostics import DiagnosticEventRegistry, DiagnosticSink
from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequestPolicy,
    GoogleApiService,
    GoogleResourceScope,
    google_api_configuration_fingerprint,
)
from spine.connectors.google_sheets import GoogleSheetsReadProfile
from spine.connectors.google_execution_envelope import (
    GoogleBoundedResponseTransport,
    GoogleExecutionApprovalVerifier,
    GoogleExecutionBudget,
    GoogleExecutionDeadlineExceeded,
    GoogleExecutionEnvelope,
    GoogleExecutionLimitExceeded,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


class GoogleLiveQualificationRefused(RuntimeError):
    """Live provider access was not explicitly and completely approved."""


class GoogleSharedDrivePreflightBinding(DefinitionModel):
    """Exact identity that an external approval and execution gate must bind."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    resource_scope_reference: OpaqueGoogleReference
    shared_drive_id: StrictStr = Field(min_length=1, max_length=1024)
    connection_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    allowed_file_ids: tuple[StrictStr, ...]
    allowed_spreadsheet_ids: tuple[StrictStr, ...]


class GoogleSharedDriveApproval(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding: GoogleSharedDrivePreflightBinding
    approved: StrictBool
    decision_reference: StrictStr = Field(min_length=1, max_length=256)


class GoogleLiveExecutionAuthorization(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding: GoogleSharedDrivePreflightBinding
    authorization_reference: StrictStr = Field(min_length=1, max_length=256)


class GoogleSharedDrivePreflightPort(Protocol):
    """Trusted external lookup for owner decision and separate run authorization."""

    async def resolve_approval(
        self, binding: GoogleSharedDrivePreflightBinding
    ) -> GoogleSharedDriveApproval | None: ...

    async def resolve_execution_authorization(
        self, binding: GoogleSharedDrivePreflightBinding
    ) -> GoogleLiveExecutionAuthorization | None: ...


class GoogleLiveFileTarget(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_id: StrictStr = Field(min_length=1, max_length=256)
    download: StrictBool
    export_mime_types: tuple[StrictStr, ...] = ()


class GoogleLiveSheetTarget(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: GoogleSheetsReadProfile
    a1_ranges: tuple[StrictStr, ...] = Field(min_length=1)


class GoogleLiveCredentialEnvironment(DefinitionModel):
    """Names of environment variables, never their secret values."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    client_id: StrictStr = Field(min_length=1, max_length=128)
    client_secret: StrictStr = Field(min_length=1, max_length=128)
    refresh_token: StrictStr = Field(min_length=1, max_length=128)


class GoogleLiveQualificationConfig(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["google-live-qualification:v1"] = (
        "google-live-qualification:v1"
    )
    opt_in: StrictBool
    connection: GoogleConnection
    resource_scope: GoogleResourceScope
    endpoints: GoogleApiEndpoints
    request_policy: GoogleApiRequestPolicy
    credential_environment: GoogleLiveCredentialEnvironment
    file_targets: tuple[GoogleLiveFileTarget, ...] = ()
    sheet_targets: tuple[GoogleLiveSheetTarget, ...] = ()
    shared_drive_approved: StrictBool
    execution_envelope: GoogleExecutionEnvelope

    @model_validator(mode="after")
    def validate_exact_scope(self) -> "GoogleLiveQualificationConfig":
        if self.connection.status is not GoogleConnectionStatus.ACTIVE:
            raise ValueError("live qualification requires an active connection")
        if self.resource_scope.reference not in (
            self.connection.approved_resource_scope_references
        ):
            raise ValueError("live resource scope is not approved by the connection")
        if (
            self.resource_scope.workspace_id != self.connection.workspace_id
            or self.resource_scope.environment_id != self.connection.environment_id
            or self.resource_scope.connection_id != self.connection.connection_id
            or self.resource_scope.authorization_generation
            != self.connection.authorization_generation
            or self.resource_scope.configuration_generation
            != self.connection.configuration_generation
        ):
            raise ValueError("live resource scope generations do not match connection")
        file_ids = tuple(target.file_id for target in self.file_targets)
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("live file targets must be unique")
        if set(file_ids) != set(self.resource_scope.allowed_file_ids):
            raise ValueError("live file targets must equal the exact file allowlist")
        profile_keys = tuple(
            (target.profile.spreadsheet_id, target.profile.sheet_id)
            for target in self.sheet_targets
        )
        if len(set(profile_keys)) != len(profile_keys):
            raise ValueError("live sheet targets must be unique")
        if {target.profile.spreadsheet_id for target in self.sheet_targets} != set(
            self.resource_scope.allowed_spreadsheet_ids
        ):
            raise ValueError(
                "live sheet targets must equal the exact spreadsheet allowlist"
            )
        if self.resource_scope.drive_id is not None and not self.shared_drive_approved:
            raise ValueError(
                "a Shared Drive requires a separately approved live target"
            )
        return self

    @property
    def api_configuration_fingerprint(self) -> str:
        return google_api_configuration_fingerprint(
            endpoints=self.endpoints,
            request_policy=self.request_policy,
            resource_scopes=(self.resource_scope,),
            sheet_profiles=tuple(target.profile for target in self.sheet_targets),
        )

    @property
    def effective_configuration_fingerprint(self) -> str:
        material = {
            "api_configuration_fingerprint": self.api_configuration_fingerprint,
            "execution_envelope": self.execution_envelope.model_dump(mode="json"),
            "file_targets": [target.model_dump(mode="json") for target in self.file_targets],
            "sheet_targets": [target.model_dump(mode="json") for target in self.sheet_targets],
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class GoogleLiveQualificationReport(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["google-live-qualification-report:v1"] = (
        "google-live-qualification-report:v1"
    )
    status: Literal["passed", "failed"]
    provider: Literal["google"] = "google"
    provider_api_version: StrictStr
    configuration_version: StrictStr
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    metadata_reads: int = Field(ge=0)
    downloads: int = Field(ge=0)
    exports: int = Field(ge=0)
    snapshot_pages: int = Field(ge=0)
    change_pages: int = Field(ge=0)
    sheet_pages: int = Field(ge=0)
    shared_drive_status: Literal["EXERCISED", "NOT_EXERCISED"]
    failure_code: (
        Literal[
            "authorization_revoked",
            "provider_quota_exhausted",
            "provider_policy_denied",
            "provider_not_found",
            "provider_unavailable",
            "provider_contract_rejected",
            "harness_failure",
            "resource_limit_exceeded",
            "deadline_exceeded",
            "cancelled",
        ]
        | None
    ) = None
    credentials_recorded: Literal[False] = False
    provider_payloads_recorded: Literal[False] = False
    source_content_recorded: Literal[False] = False
    raw_errors_recorded: Literal[False] = False


@dataclass(frozen=True, slots=True)
class GoogleLiveProbeCounts:
    metadata_reads: int = 0
    downloads: int = 0
    exports: int = 0
    snapshot_pages: int = 0
    change_pages: int = 0
    sheet_pages: int = 0


@dataclass(frozen=True, slots=True)
class GoogleLiveQualificationProvider:
    """Execute a live probe only with the repository-admitted connection version."""

    config: GoogleLiveQualificationConfig
    service: GoogleApiService
    monotonic: Callable[[], float]
    wait_for: Callable[
        [Awaitable[GoogleLiveProbeCounts], float], Awaitable[GoogleLiveProbeCounts]
    ] = asyncio.wait_for

    async def read(
        self, connection: GoogleConnection, command: GoogleReadCommand
    ) -> GoogleLiveProbeCounts:
        duration = self.config.execution_envelope.max_run_duration_seconds
        deadline = self.monotonic() + duration
        run_task = asyncio.create_task(self._read_with_budget(connection, command, deadline))
        try:
            return await self.wait_for(run_task, duration)
        except asyncio.TimeoutError:
            if run_task.cancelled():
                raise GoogleExecutionDeadlineExceeded(
                    "Google qualification deadline exceeded."
                ) from None
            raise

    async def _read_with_budget(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
        deadline: float,
    ) -> GoogleLiveProbeCounts:
        if (
            self.service.configuration_fingerprint
            != self.config.api_configuration_fingerprint
            or connection != self.config.connection
            or command
            != GoogleReadCommand(
                connection_id=self.config.connection.connection_id,
                resource_scope_reference=self.config.resource_scope.reference,
                authorization_generation=self.config.connection.authorization_generation,
                configuration_generation=self.config.connection.configuration_generation,
            )
        ):
            raise GoogleProviderContractError()
        _validate_configured_limits(self.config)
        budget = GoogleExecutionBudget(self.config.execution_envelope)
        bounded_transport = GoogleBoundedResponseTransport(
            self.service.transport,
            budget,
            deadline=deadline,
            monotonic=self.monotonic,
        )
        base_sleep = self.service.sleep

        async def bounded_sleep(delay: float) -> None:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise GoogleExecutionDeadlineExceeded(
                    "Google qualification deadline exceeded."
                )
            try:
                sleep_task = asyncio.create_task(base_sleep(delay))
                await asyncio.wait_for(sleep_task, timeout=remaining)
            except asyncio.TimeoutError:
                if sleep_task.cancelled():
                    raise GoogleExecutionDeadlineExceeded(
                        "Google qualification deadline exceeded."
                    ) from None
                raise
        metadata_reads = downloads = exports = 0
        snapshot_pages = change_pages = sheet_pages = 0
        with self.service.using_transport(bounded_transport, sleep=bounded_sleep):
            for target in self.config.file_targets:
                metadata = await self.service.get_metadata_for_file(
                    connection,
                    self.config.resource_scope.reference,
                    target.file_id,
                )
                metadata_reads += 1
                if target.download:
                    await self.service.download(
                        connection,
                        self.config.resource_scope.reference,
                        target.file_id,
                        metadata.version,
                    )
                    downloads += 1
                for media_type in target.export_mime_types:
                    await self.service.export(
                        connection,
                        self.config.resource_scope.reference,
                        target.file_id,
                        metadata.version,
                        media_type,
                    )
                    exports += 1
            if (
                self.config.resource_scope.folder_id is not None
                or self.config.resource_scope.drive_id is not None
            ):
                start_token = await self.service.get_start_page_token(
                    connection, self.config.resource_scope.reference
                )
                budget.reserve_page()
                await self.service.list_page(
                    connection,
                    self.config.resource_scope.reference,
                    None,
                )
                snapshot_pages += 1
                budget.reserve_page()
                await self.service.list_changes(
                    connection,
                    self.config.resource_scope.reference,
                    start_token,
                )
                change_pages += 1
            for target in self.config.sheet_targets:
                for a1_range in target.a1_ranges:
                    token: str | None = None
                    while True:
                        budget.reserve_page()
                        page = await self.service.get_spreadsheet_range_page(
                            connection,
                            self.config.resource_scope.reference,
                            target.profile.spreadsheet_id,
                            target.profile.sheet_id,
                            a1_range,
                            token,
                        )
                        budget.add_rows(len(page.rows))
                        sheet_pages += 1
                        if page.complete:
                            break
                        if page.next_page_token is None:
                            raise GoogleProviderContractError()
                        token = page.next_page_token
        return GoogleLiveProbeCounts(
            metadata_reads=metadata_reads,
            downloads=downloads,
            exports=exports,
            snapshot_pages=snapshot_pages,
            change_pages=change_pages,
            sheet_pages=sheet_pages,
        )


@dataclass(frozen=True, slots=True)
class GoogleLiveQualificationRunner:
    config: GoogleLiveQualificationConfig
    admission: GoogleReadAdmission[GoogleLiveProbeCounts]
    context: TrustedPersistenceContext
    approval_verifier: GoogleExecutionApprovalVerifier
    monotonic: Callable[[], float] = time.monotonic
    wait_for: Callable[
        [Awaitable[GoogleLiveProbeCounts], float], Awaitable[GoogleLiveProbeCounts]
    ] = asyncio.wait_for
    shared_drive_preflight: GoogleSharedDrivePreflightPort | None = None

    async def run(self, *, environment_opt_in: bool) -> GoogleLiveQualificationReport:
        counts = GoogleLiveProbeCounts()
        try:
            run_started = self.monotonic()
            await self.preflight(environment_opt_in=environment_opt_in)
            remaining = (
                run_started
                + self.config.execution_envelope.max_run_duration_seconds
                - self.monotonic()
            )
            if remaining <= 0:
                raise GoogleExecutionDeadlineExceeded(
                    "Google qualification deadline exceeded."
                )
            admission_task = asyncio.create_task(
                self.admission.read(
                    GoogleReadCommand(
                        connection_id=self.config.connection.connection_id,
                        resource_scope_reference=self.config.resource_scope.reference,
                        authorization_generation=(
                            self.config.connection.authorization_generation
                        ),
                        configuration_generation=(
                            self.config.connection.configuration_generation
                        ),
                    ),
                    self.context,
                )
            )
            try:
                counts = await self.wait_for(admission_task, remaining)
            except asyncio.TimeoutError:
                if admission_task.cancelled():
                    raise GoogleExecutionDeadlineExceeded(
                        "Google qualification deadline exceeded."
                    ) from None
                raise
        except GoogleProviderReadError as error:
            return self._report(
                status="failed", failure_code=error.failure_code, counts=counts
            )
        except GoogleExecutionLimitExceeded:
            return self._report(
                status="failed", failure_code="resource_limit_exceeded", counts=counts
            )
        except GoogleExecutionDeadlineExceeded:
            return self._report(
                status="failed", failure_code="deadline_exceeded", counts=counts
            )
        except asyncio.CancelledError:
            return self._report(status="failed", failure_code="cancelled", counts=counts)
        except GoogleLiveQualificationRefused:
            raise
        except GoogleConnectionError:
            return self._report(
                status="failed",
                failure_code="provider_contract_rejected",
                counts=counts,
            )
        except Exception:
            return self._report(
                status="failed", failure_code="harness_failure", counts=counts
            )
        return self._report(status="passed", failure_code=None, counts=counts)

    async def preflight(self, *, environment_opt_in: bool) -> None:
        if not self.config.opt_in or not environment_opt_in:
            raise GoogleLiveQualificationRefused(
                "live Google qualification requires both config and environment opt-in"
            )
        _validate_configured_limits(self.config)
        if not self.approval_verifier.is_approved(
            self.config.effective_configuration_fingerprint
        ):
            raise GoogleLiveQualificationRefused(
                "the effective Google execution envelope is not approved"
            )
        scope = self.config.resource_scope
        if scope.drive_id is None:
            return
        preflight = self.shared_drive_preflight
        if preflight is None:
            raise GoogleLiveQualificationRefused(
                "Shared Drive qualification requires an externally approved preflight"
            )
        binding = GoogleSharedDrivePreflightBinding(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            resource_scope_reference=scope.reference,
            shared_drive_id=scope.drive_id,
            connection_id=scope.connection_id,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            allowed_file_ids=scope.allowed_file_ids,
            allowed_spreadsheet_ids=scope.allowed_spreadsheet_ids,
        )
        try:
            approval = await preflight.resolve_approval(binding)
        except Exception:
            raise GoogleLiveQualificationRefused(
                "Shared Drive qualification preflight could not be verified"
            ) from None
        if (
            approval is None
            or not approval.approved
            or approval.binding != binding
        ):
            raise GoogleLiveQualificationRefused(
                "Shared Drive qualification lacks exact external approval"
            )
        try:
            authorization = await preflight.resolve_execution_authorization(binding)
        except Exception:
            raise GoogleLiveQualificationRefused(
                "Shared Drive execution authorization could not be verified"
            ) from None
        if authorization is None or authorization.binding != binding:
            raise GoogleLiveQualificationRefused(
                "Shared Drive qualification lacks exact live execution authorization"
            )

    def _report(
        self,
        *,
        status: Literal["passed", "failed"],
        failure_code: str | None,
        counts: GoogleLiveProbeCounts,
    ) -> GoogleLiveQualificationReport:
        return GoogleLiveQualificationReport(
            provider_api_version=self.config.endpoints.provider_api_version,
            configuration_version=self.config.endpoints.configuration_version,
            authorization_generation=self.config.connection.authorization_generation,
            configuration_generation=self.config.connection.configuration_generation,
            # This task prepares offline conformance assets. A live-looking config
            # or fake-backed run is never evidence that a Shared Drive was exercised.
            shared_drive_status="NOT_EXERCISED",
            status=status,
            failure_code=failure_code,
            **asdict(counts),
        )


def compose_google_live_qualification_runner(
    *,
    config: GoogleLiveQualificationConfig,
    service: GoogleApiService,
    repository: GoogleConnectionRepository,
    diagnostic_sink: DiagnosticSink,
    diagnostic_registry: DiagnosticEventRegistry,
    clock: Callable[[], datetime],
    context: TrustedPersistenceContext,
    approval_verifier: GoogleExecutionApprovalVerifier,
    monotonic: Callable[[], float] = time.monotonic,
    wait_for: Callable[
        [Awaitable[GoogleLiveProbeCounts], float], Awaitable[GoogleLiveProbeCounts]
    ] = asyncio.wait_for,
    shared_drive_preflight: GoogleSharedDrivePreflightPort | None = None,
) -> GoogleLiveQualificationRunner:
    """Wire the manual probe through the same trusted read admission as production."""

    admission: GoogleReadAdmission[GoogleLiveProbeCounts] = GoogleReadAdmission(
        repository=repository,
        provider=GoogleLiveQualificationProvider(
            config=config, service=service, monotonic=monotonic, wait_for=wait_for
        ),
        diagnostic_sink=diagnostic_sink,
        diagnostic_registry=diagnostic_registry,
        clock=clock,
    )
    return GoogleLiveQualificationRunner(
        config=config,
        admission=admission,
        context=context,
        approval_verifier=approval_verifier,
        monotonic=monotonic,
        shared_drive_preflight=shared_drive_preflight,
    )


def write_google_live_report(path: Path, report: GoogleLiveQualificationReport) -> None:
    """Write only the closed, sanitized report schema."""

    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "GoogleLiveCredentialEnvironment",
    "GoogleLiveFileTarget",
    "GoogleLiveQualificationConfig",
    "GoogleLiveQualificationProvider",
    "GoogleLiveExecutionAuthorization",
    "GoogleLiveQualificationRefused",
    "GoogleLiveQualificationReport",
    "GoogleLiveQualificationRunner",
    "GoogleLiveProbeCounts",
    "GoogleSharedDriveApproval",
    "GoogleSharedDrivePreflightBinding",
    "GoogleSharedDrivePreflightPort",
    "GoogleLiveSheetTarget",
    "compose_google_live_qualification_runner",
    "write_google_live_report",
]


def _validate_configured_limits(config: GoogleLiveQualificationConfig) -> None:
    envelope = config.execution_envelope
    files = {target.file_id for target in config.file_targets}
    exports = sum(len(target.export_mime_types) for target in config.file_targets)
    spreadsheets = {target.profile.spreadsheet_id for target in config.sheet_targets}
    numeric_sheets = {
        (target.profile.spreadsheet_id, target.profile.sheet_id)
        for target in config.sheet_targets
    }
    ranges = {
        (target.profile.spreadsheet_id, target.profile.sheet_id, value)
        for target in config.sheet_targets
        for value in target.a1_ranges
    }
    if (
        len(files) > envelope.max_drive_file_targets
        or exports > envelope.max_exports
        or len(spreadsheets) > envelope.max_spreadsheets
        or len(numeric_sheets) > envelope.max_numeric_sheets
        or len(ranges) > envelope.max_configured_ranges
    ):
        raise GoogleExecutionLimitExceeded("Google qualification limit exceeded.")
