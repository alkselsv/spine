"""Strictly opt-in live qualification for the governed Google adapter.

The harness is deliberately separate from ordinary connector execution and CI.
Its report is metadata-only: provider payloads, source content, credentials and
raw exception messages are never serialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, model_validator

from spine.application.connections.google import (
    GoogleConnectionError,
    GoogleProviderReadError,
)
from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequestPolicy,
    GoogleApiService,
    GoogleResourceScope,
)
from spine.connectors.google_sheets import GoogleSheetsReadProfile
from spine.domain.common import DefinitionModel
from spine.domain.connections.google import GoogleConnection, GoogleConnectionStatus


class GoogleLiveQualificationRefused(RuntimeError):
    """Live provider access was not explicitly and completely approved."""


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
        if {
            target.profile.spreadsheet_id for target in self.sheet_targets
        } != set(self.resource_scope.allowed_spreadsheet_ids):
            raise ValueError(
                "live sheet targets must equal the exact spreadsheet allowlist"
            )
        if self.resource_scope.drive_id is not None and not self.shared_drive_approved:
            raise ValueError(
                "a Shared Drive requires a separately approved live target"
            )
        return self


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
    failure_code: Literal[
        "authorization_revoked",
        "provider_quota_exhausted",
        "provider_policy_denied",
        "provider_not_found",
        "provider_unavailable",
        "provider_contract_rejected",
        "harness_failure",
    ] | None = None
    credentials_recorded: Literal[False] = False
    provider_payloads_recorded: Literal[False] = False
    source_content_recorded: Literal[False] = False
    raw_errors_recorded: Literal[False] = False


@dataclass(frozen=True, slots=True)
class GoogleLiveQualificationRunner:
    config: GoogleLiveQualificationConfig
    service: GoogleApiService

    async def run(self, *, environment_opt_in: bool) -> GoogleLiveQualificationReport:
        self.preflight(environment_opt_in=environment_opt_in)
        metadata_reads = downloads = exports = 0
        snapshot_pages = change_pages = sheet_pages = 0
        try:
            for target in self.config.file_targets:
                metadata = await self.service.get_metadata_for_file(
                    self.config.connection,
                    self.config.resource_scope.reference,
                    target.file_id,
                )
                metadata_reads += 1
                if target.download:
                    await self.service.download(
                        self.config.connection,
                        self.config.resource_scope.reference,
                        target.file_id,
                        metadata.version,
                    )
                    downloads += 1
                for media_type in target.export_mime_types:
                    await self.service.export(
                        self.config.connection,
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
                    self.config.connection, self.config.resource_scope.reference
                )
                await self.service.list_page(
                    self.config.connection,
                    self.config.resource_scope.reference,
                    None,
                )
                snapshot_pages += 1
                await self.service.list_changes(
                    self.config.connection,
                    self.config.resource_scope.reference,
                    start_token,
                )
                change_pages += 1
            for target in self.config.sheet_targets:
                for a1_range in target.a1_ranges:
                    token: str | None = None
                    while True:
                        page = await self.service.get_spreadsheet_range_page(
                            self.config.connection,
                            self.config.resource_scope.reference,
                            target.profile.spreadsheet_id,
                            target.profile.sheet_id,
                            a1_range,
                            token,
                        )
                        sheet_pages += 1
                        if page.complete:
                            break
                        if page.next_page_token is None:
                            raise GoogleLiveQualificationRefused(
                                "incomplete live Sheets page has no continuation"
                            )
                        token = page.next_page_token
        except GoogleProviderReadError as error:
            return self._report(
                status="failed",
                failure_code=error.failure_code,
                metadata_reads=metadata_reads,
                downloads=downloads,
                exports=exports,
                snapshot_pages=snapshot_pages,
                change_pages=change_pages,
                sheet_pages=sheet_pages,
            )
        except GoogleLiveQualificationRefused:
            raise
        except GoogleConnectionError:
            return self._report(
                status="failed",
                failure_code="provider_contract_rejected",
                metadata_reads=metadata_reads,
                downloads=downloads,
                exports=exports,
                snapshot_pages=snapshot_pages,
                change_pages=change_pages,
                sheet_pages=sheet_pages,
            )
        except Exception:
            return self._report(
                status="failed",
                failure_code="harness_failure",
                metadata_reads=metadata_reads,
                downloads=downloads,
                exports=exports,
                snapshot_pages=snapshot_pages,
                change_pages=change_pages,
                sheet_pages=sheet_pages,
            )
        return self._report(
            status="passed",
            failure_code=None,
            metadata_reads=metadata_reads,
            downloads=downloads,
            exports=exports,
            snapshot_pages=snapshot_pages,
            change_pages=change_pages,
            sheet_pages=sheet_pages,
        )

    def preflight(self, *, environment_opt_in: bool) -> None:
        if not self.config.opt_in or not environment_opt_in:
            raise GoogleLiveQualificationRefused(
                "live Google qualification requires both config and environment opt-in"
            )

    def _report(self, **values: object) -> GoogleLiveQualificationReport:
        return GoogleLiveQualificationReport(
            provider_api_version=self.config.endpoints.provider_api_version,
            configuration_version=self.config.endpoints.configuration_version,
            authorization_generation=self.config.connection.authorization_generation,
            configuration_generation=self.config.connection.configuration_generation,
            shared_drive_status=(
                "EXERCISED"
                if self.config.resource_scope.drive_id is not None
                else "NOT_EXERCISED"
            ),
            **values,
        )


def write_google_live_report(
    path: Path, report: GoogleLiveQualificationReport
) -> None:
    """Write only the closed, sanitized report schema."""

    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "GoogleLiveCredentialEnvironment",
    "GoogleLiveFileTarget",
    "GoogleLiveQualificationConfig",
    "GoogleLiveQualificationRefused",
    "GoogleLiveQualificationReport",
    "GoogleLiveQualificationRunner",
    "GoogleLiveSheetTarget",
    "write_google_live_report",
]
