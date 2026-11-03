"""Private, fail-closed runtime for one approved Google qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.connections.google import (
    GoogleConnectionRepository,
    GoogleReadCommand,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.connectors.google_api import (
    EnvironmentGoogleCredentialResolver,
    GoogleApiEndpoints,
    GoogleApiRequest,
    GoogleApiRequestPolicy,
    GoogleApiResponse,
    GoogleApiRetryEvent,
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
from spine.connectors.google_qualification_policy import (
    GOOGLE_PHASE1_READONLY_SCOPES,
)
from spine.connectors.google_sheets import GoogleSheetsColumn, GoogleSheetsReadProfile
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


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
    "SPINE_GOOGLE_CONNECTION_VERSION_ID",
    "SPINE_GOOGLE_CONNECTION_ID",
    "SPINE_GOOGLE_APPROVAL_ID",
    "SPINE_GOOGLE_CONNECTION_VERSION",
    "SPINE_GOOGLE_PREDECESSOR_VERSION_ID",
    "SPINE_GOOGLE_WORKSPACE_ID",
    "SPINE_GOOGLE_ENVIRONMENT_ID",
    "SPINE_GOOGLE_SERVICE_PRINCIPAL_ID",
    "SPINE_GOOGLE_CREDENTIAL_REFERENCE",
    "SPINE_GOOGLE_GRANT_REFERENCE",
    "SPINE_GOOGLE_RESOURCE_SCOPE_REFERENCE",
    "SPINE_GOOGLE_AUTHORIZATION_GENERATION",
    "SPINE_GOOGLE_CONFIGURATION_GENERATION",
)

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


def _uuid(environment: Mapping[str, str], name: str) -> UUID:
    try:
        value = UUID(_required(environment, name))
        if value.int == 0:
            raise ValueError
        return value
    except ValueError:
        raise GoogleLiveQualificationRefused(
            "Google private qualification authority is invalid"
        ) from None


def _positive_int(environment: Mapping[str, str], name: str) -> int:
    try:
        value = int(_required(environment, name))
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        raise GoogleLiveQualificationRefused(
            "Google private qualification authority is invalid"
        ) from None


def build_google_live_config_from_environment(
    environment: Mapping[str, str],
) -> GoogleLiveQualificationConfig:
    """Build the private non-secret manifest from exact approved env values."""

    _manifest_digest(environment)
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

    workspace_id = _uuid(environment, "SPINE_GOOGLE_WORKSPACE_ID")
    environment_id = _uuid(environment, "SPINE_GOOGLE_ENVIRONMENT_ID")
    connection_id = _uuid(environment, "SPINE_GOOGLE_CONNECTION_ID")
    service_principal_id = _uuid(environment, "SPINE_GOOGLE_SERVICE_PRINCIPAL_ID")
    scope_reference = OpaqueGoogleReference(
        value=_uuid(environment, "SPINE_GOOGLE_RESOURCE_SCOPE_REFERENCE")
    )
    credential_reference = OpaqueGoogleReference(
        value=_uuid(environment, "SPINE_GOOGLE_CREDENTIAL_REFERENCE")
    )
    grant_reference = OpaqueGoogleReference(
        value=_uuid(environment, "SPINE_GOOGLE_GRANT_REFERENCE")
    )
    version = _positive_int(environment, "SPINE_GOOGLE_CONNECTION_VERSION")
    predecessor_text = _required(
        environment, "SPINE_GOOGLE_PREDECESSOR_VERSION_ID"
    )
    predecessor_version_id = (
        None
        if predecessor_text == "NONE"
        else _uuid(environment, "SPINE_GOOGLE_PREDECESSOR_VERSION_ID")
    )
    connection = GoogleConnection(
        connection_version_id=_uuid(
            environment, "SPINE_GOOGLE_CONNECTION_VERSION_ID"
        ),
        connection_id=connection_id,
        approval_id=_uuid(environment, "SPINE_GOOGLE_APPROVAL_ID"),
        version=version,
        predecessor_version_id=predecessor_version_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        service_principal_id=service_principal_id,
        credential_reference=credential_reference,
        grant_reference=grant_reference,
        approved_resource_scope_references=(scope_reference,),
        authorization_generation=_positive_int(
            environment, "SPINE_GOOGLE_AUTHORIZATION_GENERATION"
        ),
        configuration_generation=_positive_int(
            environment, "SPINE_GOOGLE_CONFIGURATION_GENERATION"
        ),
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
            authorization_generation=connection.authorization_generation,
            configuration_generation=connection.configuration_generation,
            allowed_file_ids=allowed_files,
            allowed_spreadsheet_ids=spreadsheets,
            folder_id=_required(environment, "GOOGLE_ROOT_FOLDER_ID"),
            supports_all_drives=False,
        ),
        endpoints=GoogleApiEndpoints(
            drive_base_url="https://www.googleapis.com/drive/v3",
            sheets_base_url="https://sheets.googleapis.com/v4",
            oauth_token_url="https://oauth2.googleapis.com/token",
            approved_oauth_scopes=GOOGLE_PHASE1_READONLY_SCOPES,
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
class JsonLoggingGoogleApiRetryObserver:
    """Emit disclosure-safe tenant retry evidence to an operator-owned logger."""

    logger: logging.Logger

    async def record_retry(self, event: GoogleApiRetryEvent) -> None:
        self.logger.info(
            json.dumps(
                {
                    "schema_version": event.schema_version,
                    "workspace_id": str(event.workspace_id),
                    "environment_id": str(event.environment_id),
                    "connection_id": str(event.connection_id),
                    "operation": event.operation,
                    "reason": event.reason,
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


@dataclass(frozen=True, slots=True)
class _OneShotHttpxTransport:
    async def request(self, request: GoogleApiRequest) -> GoogleApiResponse:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await HttpxGoogleApiTransport(client).request(request)


@dataclass(frozen=True, slots=True)
class _AdmittedCredentialPreflight:
    repository: GoogleConnectionRepository
    resolver: EnvironmentGoogleCredentialResolver
    expected_connection: GoogleConnection

    async def verify(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> None:
        async with self.repository.admit_read(command, context) as connection:
            if connection != self.expected_connection:
                raise GoogleLiveQualificationRefused(
                    "Google current connection does not match the approved authority"
                )
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
        connection_repository: GoogleConnectionRepository | None = None,
        context_boundary: TrustedContextBoundary | None = None,
    ) -> None:
        self._environment = environment
        self._transport = transport or _OneShotHttpxTransport()
        self._connection_repository = connection_repository
        self._context_boundary = context_boundary or TrustedContextBoundary.create()
        self._retry_observer = JsonLoggingGoogleApiRetryObserver(
            logging.getLogger("spine.google.qualification.retry")
        )

    def _repository(self):
        if self._connection_repository is not None:
            return self._connection_repository
        from spine.infrastructure.connections.postgresql_google import (
            PostgreSQLGoogleConnectionRepository,
        )

        settings = RuntimeDatabaseSettings()
        engine = create_async_engine(
            settings.url.get_secret_value(),
            pool_size=1,
            max_overflow=0,
            pool_timeout=settings.pool_timeout_seconds,
            pool_pre_ping=settings.pool_pre_ping,
            pool_reset_on_return="rollback",
            isolation_level="READ COMMITTED",
            connect_args={
                "connect_timeout": settings.connect_timeout_seconds,
                "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
            },
            hide_parameters=True,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        return PostgreSQLGoogleConnectionRepository(
            session_factory=sessions,
            context_verifier=self._context_boundary,
        )

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
        context = self._context_boundary.worker(
            scope=EnvironmentScope(
                config.connection.workspace_id, config.connection.environment_id
            ),
            purpose=PersistencePurpose("google_connector"),
            operation=PersistenceOperation("google_connection_read"),
            trace_id=uuid4(),
            service_principal_id=config.connection.service_principal_id,
        )
        repository = self._repository()
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
            retry_observer=self._retry_observer,
            connection_repository=repository,
            diagnostic_sink=JsonLoggingDiagnosticSink(
                registry, logger=logging.getLogger("spine.google.qualification")
            ),
            diagnostic_registry=registry,
            clock=lambda: datetime.now(timezone.utc),
            sleep=asyncio.sleep,
            context=context,
            approval_verifier=_ApprovedFingerprint(approved_fingerprint),
            credential_preflight=_AdmittedCredentialPreflight(
                repository, resolver, config.connection
            ),
        )
        return bindings.build_runner(config)


def build_live_runner(
    config: GoogleLiveQualificationConfig,
) -> GoogleLiveQualificationRunner:
    """Deployment-owned ``module:function`` entry point used by the live CLI."""

    return GooglePrivateLiveRuntime(os.environ).build_runner(config)


__all__ = [
    "GooglePrivateLiveRuntime",
    "JsonLoggingGoogleApiRetryObserver",
    "build_google_live_config_from_environment",
    "build_live_runner",
]
