"""Private, fail-closed runtime for one approved Google qualification."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from spine.application.connections.google import (
    GoogleConnectionConflictError,
    GoogleConnectionDeniedError,
    GoogleReadCommand,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.connectors.google_api import (
    EnvironmentGoogleCredentialResolver,
    GoogleApiEndpoints,
    GoogleApiRequest,
    GoogleApiRequestPolicy,
    GoogleApiResponse,
    GoogleApiRetryEvent,
    GoogleApiService,
    GoogleOAuthMaterial,
    GoogleResourceScope,
    HttpxGoogleApiTransport,
)
from spine.connectors.google_execution_envelope import GoogleExecutionEnvelope
from spine.connectors.google_live_composition import GoogleLiveQualificationBindings
from spine.connectors.google_live_qualification import (
    GoogleLiveCredentialEnvironment,
    GoogleLiveFileTarget,
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    GoogleLiveSheetTarget,
)
from spine.connectors.google_sheets import GoogleSheetsColumn, GoogleSheetsReadProfile
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink


_MANIFEST_KEYS = (
    "GOOGLE_QUALIFICATION_ACCOUNT",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_OAUTH_CLIENT_FILE",
    "GOOGLE_ROOT_FOLDER_ID",
    "GOOGLE_NESTED_FOLDER_IDS",
    "GOOGLE_ALLOWED_FILE_IDS",
    "GOOGLE_SPREADSHEET_IDS",
    "GOOGLE_NUMERIC_SHEET_IDS",
    "GOOGLE_SHARED_DRIVE_STATUS",
    "GOOGLE_RESOURCE_SCOPE_VERSION",
)
_NAMESPACE_PREFIX = "https://spine.local/qualification/google/"


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise GoogleLiveQualificationRefused(
            "Google private qualification environment is incomplete"
        )
    return value


def _csv(environment: Mapping[str, str], name: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in _required(environment, name).split(","))
    if not values or any(not value for value in values) or len(set(values)) != len(values):
        raise GoogleLiveQualificationRefused(
            "Google private qualification environment is incomplete"
        )
    return values


def _manifest_digest(environment: Mapping[str, str]) -> str:
    material = "".join(f"{key}={_required(environment, key)}\n" for key in _MANIFEST_KEYS)
    digest = hashlib.sha256(material.encode()).hexdigest()
    if digest != _required(environment, "SPINE_GOOGLE_QUALIFICATION_MANIFEST_SHA256"):
        raise GoogleLiveQualificationRefused(
            "Google private qualification manifest is not approved"
        )
    return digest


def _id(digest: str, label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{_NAMESPACE_PREFIX}{digest}/{label}")


def build_google_live_config_from_environment(
    environment: Mapping[str, str],
) -> GoogleLiveQualificationConfig:
    """Build the private non-secret manifest from exact approved env values."""

    digest = _manifest_digest(environment)
    allowed_files = _csv(environment, "GOOGLE_ALLOWED_FILE_IDS")
    spreadsheets = _csv(environment, "GOOGLE_SPREADSHEET_IDS")
    sheet_ids_text = _csv(environment, "GOOGLE_NUMERIC_SHEET_IDS")
    if len(allowed_files) != 3 or len(spreadsheets) != 1 or len(sheet_ids_text) != 2:
        raise GoogleLiveQualificationRefused(
            "Google private qualification allowlist is unsupported"
        )
    try:
        sheet_ids = tuple(int(value) for value in sheet_ids_text)
    except ValueError:
        raise GoogleLiveQualificationRefused(
            "Google private qualification allowlist is unsupported"
        ) from None
    if any(value < 0 for value in sheet_ids):
        raise GoogleLiveQualificationRefused(
            "Google private qualification allowlist is unsupported"
        )
    pdf_file = _required(environment, "GOOGLE_PDF_FILE_ID")
    docx_file = _required(environment, "GOOGLE_DOCX_FILE_ID")
    native_doc = _required(environment, "GOOGLE_NATIVE_DOC_FILE_ID")
    if {pdf_file, docx_file, native_doc} != set(allowed_files):
        raise GoogleLiveQualificationRefused(
            "Google private qualification file roles do not match the allowlist"
        )
    if _required(environment, "GOOGLE_SHARED_DRIVE_STATUS") != "NOT_EXERCISED":
        raise GoogleLiveQualificationRefused(
            "Google Phase 1 private environment must remain My Drive only"
        )

    workspace_id = _id(digest, "workspace")
    environment_id = _id(digest, "environment")
    connection_id = _id(digest, "connection")
    service_principal_id = _id(digest, "service-principal")
    scope_reference = OpaqueGoogleReference(value=_id(digest, "resource-scope"))
    credential_reference = OpaqueGoogleReference(value=_id(digest, "credential"))
    grant_reference = OpaqueGoogleReference(value=_id(digest, "grant"))
    connection = GoogleConnection(
        connection_version_id=_id(digest, "connection-version-1"),
        connection_id=connection_id,
        approval_id=_id(digest, "approval"),
        version=1,
        workspace_id=workspace_id,
        environment_id=environment_id,
        service_principal_id=service_principal_id,
        credential_reference=credential_reference,
        grant_reference=grant_reference,
        approved_resource_scope_references=(scope_reference,),
        authorization_generation=1,
        configuration_generation=1,
        status=GoogleConnectionStatus.ACTIVE,
    )
    spreadsheet_id = spreadsheets[0]
    columns = (
        GoogleSheetsColumn(column_key="immutable_business_id", coordinate="A"),
        GoogleSheetsColumn(column_key="formula_value", coordinate="B"),
        GoogleSheetsColumn(column_key="date_value", coordinate="C"),
        GoogleSheetsColumn(column_key="currency_value", coordinate="D"),
        GoogleSheetsColumn(column_key="formatted_number", coordinate="E"),
    )
    sheet_targets = tuple(
        GoogleLiveSheetTarget(
            profile=GoogleSheetsReadProfile(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                business_id_column="immutable_business_id",
                columns=columns,
                developer_metadata_key="spine_immutable_business_id",
            ),
            a1_ranges=("A2:E5000",),
        )
        for sheet_id in sheet_ids
    )
    return GoogleLiveQualificationConfig(
        opt_in=environment.get("SPINE_GOOGLE_LIVE_CONFIG_OPT_IN") == "1",
        connection=connection,
        resource_scope=GoogleResourceScope(
            reference=scope_reference,
            workspace_id=workspace_id,
            environment_id=environment_id,
            connection_id=connection_id,
            authorization_generation=1,
            configuration_generation=1,
            allowed_file_ids=allowed_files,
            allowed_spreadsheet_ids=spreadsheets,
            folder_id=_required(environment, "GOOGLE_ROOT_FOLDER_ID"),
            supports_all_drives=False,
        ),
        endpoints=GoogleApiEndpoints(
            drive_base_url="https://www.googleapis.com/drive/v3",
            sheets_base_url="https://sheets.googleapis.com/v4",
            oauth_token_url="https://oauth2.googleapis.com/token",
            approved_oauth_scopes=(
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
            ),
            provider_api_version="google-drive:v3+google-sheets:v4",
            configuration_version="google-615-phase1-web-oauth:v1",
        ),
        request_policy=GoogleApiRequestPolicy(
            timeout_seconds=30.0,
            max_attempts=4,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(1.0, 2.0, 4.0),
            drive_page_size=100,
            sheets_page_size=1000,
        ),
        credential_environment=GoogleLiveCredentialEnvironment(
            client_id="SPINE_GOOGLE_OAUTH_CLIENT_ID",
            client_secret="SPINE_GOOGLE_OAUTH_CLIENT_SECRET",
            refresh_token="SPINE_GOOGLE_OAUTH_REFRESH_TOKEN",
        ),
        file_targets=(
            GoogleLiveFileTarget(
                file_id=pdf_file, download=True, export_mime_types=()
            ),
            GoogleLiveFileTarget(
                file_id=docx_file, download=True, export_mime_types=()
            ),
            GoogleLiveFileTarget(
                file_id=native_doc,
                download=False,
                export_mime_types=(
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            ),
        ),
        sheet_targets=sheet_targets,
        shared_drive_approved=False,
        execution_envelope=GoogleExecutionEnvelope(
            max_run_duration_seconds=1800.0,
            max_response_body_bytes=10_485_760,
            max_drive_content_bytes=10_485_760,
            max_aggregate_response_bytes=104_857_600,
            max_drive_file_targets=10,
            max_exports=10,
            max_spreadsheets=3,
            max_numeric_sheets=10,
            max_aggregate_rows=10_000,
            max_api_pages=200,
            max_configured_ranges=50,
        ),
    )


@dataclass(frozen=True, slots=True)
class _ApprovedFingerprint:
    value: str

    def is_approved(self, envelope_fingerprint: str) -> bool:
        return envelope_fingerprint == self.value


@dataclass(frozen=True, slots=True)
class _NoOpRetryObserver:
    async def record_retry(self, event: GoogleApiRetryEvent) -> None:
        del event


@dataclass(frozen=True, slots=True)
class _OneShotHttpxTransport:
    async def request(self, request: GoogleApiRequest) -> GoogleApiResponse:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await HttpxGoogleApiTransport(client).request(request)


@dataclass(frozen=True, slots=True)
class _PinnedConnectionRepository:
    connection: GoogleConnection
    context: TrustedPersistenceContext

    @asynccontextmanager
    async def admit_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> AsyncIterator[GoogleConnection]:
        if (
            context != self.context
            or command.connection_id != self.connection.connection_id
            or command.resource_scope_reference
            not in self.connection.approved_resource_scope_references
            or command.authorization_generation
            != self.connection.authorization_generation
            or command.configuration_generation
            != self.connection.configuration_generation
        ):
            raise GoogleConnectionDeniedError()
        yield self.connection

    async def get_current(self, connection_id, context):
        if connection_id != self.connection.connection_id or context != self.context:
            raise GoogleConnectionConflictError()
        return self.connection

    async def register(self, command, context):
        del command, context
        raise GoogleConnectionConflictError()

    async def suspend_revoked(self, connection, context, *, occurred_at):
        del connection, context, occurred_at
        raise GoogleConnectionConflictError()


@dataclass(frozen=True, slots=True)
class _AdmittedCredentialPreflight:
    repository: _PinnedConnectionRepository
    resolver: EnvironmentGoogleCredentialResolver

    async def verify(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> None:
        async with self.repository.admit_read(command, context) as connection:
            material = await self.resolver.resolve(
                connection.credential_reference, connection.grant_reference
            )
            if not isinstance(material, GoogleOAuthMaterial):
                raise GoogleLiveQualificationRefused(
                    "Google live credentials could not be verified"
                )


class GooglePrivateLiveRuntime:
    """Build the one-run composition from an exact private environment."""

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        transport=None,
    ) -> None:
        self._environment = environment
        self._transport = transport or _OneShotHttpxTransport()

    def build_runner(
        self, config: GoogleLiveQualificationConfig
    ) -> GoogleLiveQualificationRunner:
        expected = build_google_live_config_from_environment(self._environment)
        if config != expected:
            raise GoogleLiveQualificationRefused(
                "Google live config does not match the private environment"
            )
        approved_fingerprint = _required(
            self._environment, "SPINE_GOOGLE_LIVE_APPROVED_EFFECTIVE_FINGERPRINT"
        )
        if approved_fingerprint != config.effective_configuration_fingerprint:
            raise GoogleLiveQualificationRefused(
                "Google live effective configuration is not approved"
            )
        digest = _manifest_digest(self._environment)
        context = TrustedPersistenceContext(
            scope=EnvironmentScope(
                config.connection.workspace_id, config.connection.environment_id
            ),
            origin=ContextOrigin.WORKER,
            purpose=PersistencePurpose("google_connector"),
            operation=PersistenceOperation("google_connection_read"),
            trace_id=_id(digest, "trace"),
            acting_subject_id=None,
            service_principal_id=config.connection.service_principal_id,
            provenance=TrustedContextProvenance(
                _id(digest, "context-issuer"), hashlib.sha256(digest.encode()).digest()
            ),
        )
        repository = _PinnedConnectionRepository(config.connection, context)
        resolver = EnvironmentGoogleCredentialResolver(
            credential_reference=config.connection.credential_reference,
            grant_reference=config.connection.grant_reference,
            client_id_variable=config.credential_environment.client_id,
            client_secret_variable=config.credential_environment.client_secret,
            refresh_token_variable=config.credential_environment.refresh_token,
            environment=self._environment,
        )
        registry = DiagnosticEventRegistry.with_default_families()
        bindings = GoogleLiveQualificationBindings(
            credential_resolver=resolver,
            transport=self._transport,
            retry_observer=_NoOpRetryObserver(),
            connection_repository=repository,
            diagnostic_sink=JsonLoggingDiagnosticSink(
                registry, logger=logging.getLogger("spine.google.qualification")
            ),
            diagnostic_registry=registry,
            clock=lambda: datetime.now(timezone.utc),
            sleep=asyncio.sleep,
            context=context,
            approval_verifier=_ApprovedFingerprint(approved_fingerprint),
            credential_preflight=_AdmittedCredentialPreflight(repository, resolver),
        )
        return bindings.build_runner(config)


def build_live_runner(
    config: GoogleLiveQualificationConfig,
) -> GoogleLiveQualificationRunner:
    """Deployment-owned ``module:function`` entry point used by the live CLI."""

    return GooglePrivateLiveRuntime(os.environ).build_runner(config)


__all__ = [
    "GooglePrivateLiveRuntime",
    "build_google_live_config_from_environment",
    "build_live_runner",
]
