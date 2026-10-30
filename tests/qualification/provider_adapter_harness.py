"""Real-adapter provider inputs for the issue #677 qualification seam."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.connections.google import GoogleReadCommand
from spine.application.connections.provider import (
    ProviderReadAdmission,
    ProviderReadCommand,
)
from spine.connectors.bitrix24_crm_material import (
    BITRIX24_CRM_REQUIRED_CAPABILITIES,
    Bitrix24CrmActivity,
    Bitrix24CrmAttachment,
    Bitrix24CrmMaterialAdapter,
    Bitrix24CrmMaterialReadCommand,
    Bitrix24CrmMaterialReadProfile,
    bitrix24_crm_normalized_observation,
)
from spine.connectors.bitrix24_disk import (
    BITRIX24_DISK_REQUIRED_CAPABILITIES,
    Bitrix24DiskFile,
    Bitrix24DiskFileContent,
    Bitrix24DiskFilePage,
    Bitrix24DiskProviderAdapter,
    Bitrix24DiskReadProfile,
)
from spine.connectors.files.mounted_filesystem import (
    MountedFilesystemObservationReader,
    MountedFilesystemRoot,
)
from spine.connectors.google_drive import (
    DriveFileContent,
    DriveFileMetadata,
    GoogleDriveProviderAdapter,
)
from spine.connectors.google_observation_compatibility import (
    google_v1_observation_to_v2,
)
from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsProviderAdapter,
    GoogleSheetsReadProfile,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
)
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderStructuredObjectKey,
    StructuredObservationRepresentation,
)
from spine.connectors.yandex_disk import (
    YandexDiskFileContent,
    YandexDiskProviderAdapter,
    YandexDiskReadProfile,
    YandexDiskResource,
    YandexDiskResourcePage,
)
from spine.domain.connections.google import OpaqueGoogleReference
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources.google import GoogleSheetCellValue, GoogleSheetNumberFormat

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
EQUIVALENT_BINARY_CONTENT = b"%PDF synthetic equivalent proposal material"


def stable_id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"issue-677:{value}")


@dataclass(frozen=True, slots=True)
class BinaryProviderCase:
    name: str
    provider_kind: ProviderKind
    provider_object_id: str
    provider_revision: str
    resource_name: str
    resource_path: str | None
    identity_assurance: ExternalIdentityAssurance


BINARY_PROVIDER_CASES = (
    BinaryProviderCase(
        "google_compatibility",
        ProviderKind.GOOGLE,
        "drive-file-677",
        "7",
        "proposal.pdf",
        None,
        ExternalIdentityAssurance.PROVIDER_STABLE,
    ),
    BinaryProviderCase(
        "yandex_disk",
        ProviderKind.YANDEX_DISK,
        "disk:/approved/proposal.pdf",
        "1700000000000677",
        "proposal.pdf",
        "disk:/approved/proposal.pdf",
        ExternalIdentityAssurance.PATH_SCOPED,
    ),
    BinaryProviderCase(
        "bitrix24",
        ProviderKind.BITRIX24,
        "9043",
        "42",
        "proposal.pdf",
        None,
        ExternalIdentityAssurance.PROVIDER_STABLE,
    ),
    BinaryProviderCase(
        "mounted_filesystem",
        ProviderKind.MOUNTED_FILESYSTEM,
        "approved/proposal.pdf",
        "",
        "proposal.pdf",
        "approved/proposal.pdf",
        ExternalIdentityAssurance.PATH_SCOPED,
    ),
)


class _Provider(Protocol):
    async def read(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> NormalizedExternalObservationV2: ...


class AdmittedConnectionRepository:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.admissions = 0

    @asynccontextmanager
    async def admit_read(
        self, command: ProviderReadCommand, context: object
    ) -> AsyncIterator[ProviderConnection]:
        del context
        assert command.connection_id == self.connection.connection_id
        assert command.connection_version_id == self.connection.connection_version_id
        self.admissions += 1
        yield self.connection


class GoogleV2Adapter:
    def __init__(self, adapter: GoogleDriveProviderAdapter) -> None:
        self.adapter = adapter

    async def read(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> NormalizedExternalObservationV2:
        drive = await self.adapter.read(connection, command)
        return google_v1_observation_to_v2(
            drive.to_normalized_observation(),
            connection_version_id=connection.connection_version_id,
        )


class GoogleService:
    def __init__(self, case: BinaryProviderCase) -> None:
        self.case = case
        self.calls: list[str] = []

    async def get_metadata(
        self, connection: object, resource_scope_reference: object
    ) -> DriveFileMetadata:
        del connection, resource_scope_reference
        self.calls.append("metadata")
        return DriveFileMetadata(
            file_id=self.case.provider_object_id,
            name=self.case.resource_name,
            mime_type="application/pdf",
            version=self.case.provider_revision,
        )

    async def get_metadata_for_file(
        self,
        connection: object,
        resource_scope_reference: object,
        file_id: str,
    ) -> DriveFileMetadata:
        assert file_id == self.case.provider_object_id
        return await self.get_metadata(connection, resource_scope_reference)

    async def download(
        self,
        connection: object,
        resource_scope_reference: object,
        file_id: str,
        version: str,
    ) -> DriveFileContent:
        del connection, resource_scope_reference
        assert file_id == self.case.provider_object_id
        assert version == self.case.provider_revision
        self.calls.append("download")
        return DriveFileContent(
            content=EQUIVALENT_BINARY_CONTENT, media_type="application/pdf"
        )

    async def export(self, *args: object, **kwargs: object) -> DriveFileContent:
        del args, kwargs
        raise AssertionError("binary qualification must not export")


class YandexService:
    def __init__(self, case: BinaryProviderCase) -> None:
        self.case = case
        self.calls: list[str] = []

    def resource(self) -> YandexDiskResource:
        digest = hashlib.sha256(EQUIVALENT_BINARY_CONTENT).hexdigest()
        return YandexDiskResource(
            name=self.case.resource_name,
            path=self.case.provider_object_id,
            type="file",
            mime_type="application/pdf",
            size=len(EQUIVALENT_BINARY_CONTENT),
            md5=hashlib.md5(EQUIVALENT_BINARY_CONTENT).hexdigest(),  # noqa: S324
            sha256=digest,
            revision=self.case.provider_revision,
            modified=NOW.isoformat(),
            resource_id="1573541:proposal-677",
        )

    async def list_resources(
        self,
        connection: object,
        scope: object,
        *,
        offset: int,
        limit: int,
    ) -> YandexDiskResourcePage:
        del connection, scope, limit
        self.calls.append("list")
        items = [self.resource().model_dump(mode="json")] if offset == 0 else []
        return YandexDiskResourcePage(
            _embedded={
                "path": "disk:/approved",
                "items": items,
                "limit": 100,
                "offset": offset,
                "total": 1,
            }
        )

    async def download(
        self,
        connection: object,
        scope: object,
        *,
        path: str,
        revision: str,
        max_bytes: int,
    ) -> YandexDiskFileContent:
        del connection, scope, max_bytes
        assert path == self.case.provider_object_id
        assert revision == self.case.provider_revision
        self.calls.append("download")
        return YandexDiskFileContent(
            content=EQUIVALENT_BINARY_CONTENT, media_type="application/pdf"
        )

    async def get_resource(
        self, connection: object, scope: object, *, path: str
    ) -> YandexDiskResource:
        del connection, scope
        assert path == self.case.provider_object_id
        self.calls.append("metadata")
        return self.resource()


class BitrixService:
    def __init__(self, case: BinaryProviderCase) -> None:
        self.case = case
        self.calls: list[str] = []

    def file(self) -> Bitrix24DiskFile:
        return Bitrix24DiskFile(
            ID=self.case.provider_object_id,
            NAME=self.case.resource_name,
            TYPE="file",
            STORAGE_ID="1357",
            PARENT_ID="8907",
            GLOBAL_CONTENT_VERSION=self.case.provider_revision,
            FILE_ID="32718",
            SIZE=len(EQUIVALENT_BINARY_CONTENT),
            CREATE_TIME=NOW,
            UPDATE_TIME=NOW,
            DOWNLOAD_URL="https://example.invalid/download/qualification",
        )

    async def authorize_read(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls.append("authorize")

    async def list_children(
        self, *args: object, **kwargs: object
    ) -> Bitrix24DiskFilePage:
        del args, kwargs
        self.calls.append("list")
        return Bitrix24DiskFilePage(result=[self.file()], total=1)

    async def get_file(self, *args: object, **kwargs: object) -> Bitrix24DiskFile:
        del args, kwargs
        self.calls.append("get")
        return self.file()

    async def download(
        self, *args: object, **kwargs: object
    ) -> Bitrix24DiskFileContent:
        del args, kwargs
        self.calls.append("download")
        return Bitrix24DiskFileContent(
            content=EQUIVALENT_BINARY_CONTENT, media_type="application/pdf"
        )


class GoogleSheetsService:
    async def get_provider_version(self, *args: object) -> str:
        del args
        return "sheet-v7"

    async def get_spreadsheet(self, *args: object) -> GoogleSheetsSnapshot:
        del args
        return GoogleSheetsSnapshot(
            spreadsheet_id="sheet-677",
            sheet_id=17,
            sheet_title="Proposals",
            locale="ru_RU",
            time_zone="Europe/Moscow",
            rows=(
                GoogleSheetsRowData(
                    row_number=2,
                    a1_range="A2:B2",
                    cells=(
                        GoogleSheetsCellData(
                            column_key="proposal_id",
                            coordinate="A2",
                            entered=GoogleSheetCellValue(
                                kind="string", value="proposal-677"
                            ),
                            effective=GoogleSheetCellValue(
                                kind="string", value="proposal-677"
                            ),
                            formatted="proposal-677",
                            number_format=GoogleSheetNumberFormat(type="TEXT"),
                        ),
                        GoogleSheetsCellData(
                            column_key="total",
                            coordinate="B2",
                            entered=GoogleSheetCellValue(kind="number", value="1250.5"),
                            effective=GoogleSheetCellValue(
                                kind="number", value="1250.5"
                            ),
                            formatted="1250.50",
                            number_format=GoogleSheetNumberFormat(
                                type="NUMBER", pattern="#0.00"
                            ),
                        ),
                    ),
                ),
            ),
        )


class BitrixCrmService:
    async def authorize_read(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def get_activity(
        self, *args: object, **kwargs: object
    ) -> Bitrix24CrmActivity:
        del args, kwargs
        return Bitrix24CrmActivity(
            ID="501",
            OWNER_TYPE_ID="deal",
            OWNER_ID="932",
            LAST_UPDATED=NOW,
            ACTIVITY_REVISION="activity-v7",
        )

    async def get_attachment(
        self, *args: object, **kwargs: object
    ) -> Bitrix24CrmAttachment:
        del args, kwargs
        return Bitrix24CrmAttachment(
            ID="attachment-1",
            ACTIVITY_ID="501",
            OWNER_TYPE_ID="deal",
            OWNER_ID="932",
            DISK_FILE_ID="9043",
            DISK_FILE_REVISION="42",
            RELATIONSHIP_REVISION="link-v3",
        )


class BitrixCrmV2Adapter:
    def __init__(
        self,
        adapter: Bitrix24CrmMaterialAdapter,
        crm_command: Bitrix24CrmMaterialReadCommand,
    ) -> None:
        self.adapter = adapter
        self.crm_command = crm_command

    async def read(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> NormalizedExternalObservationV2:
        assert command.connection_id == self.crm_command.connection_id
        observed = await self.adapter.read(connection, self.crm_command)
        return bitrix24_crm_normalized_observation(
            observed,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
        )


def provider_connection(case: BinaryProviderCase) -> ProviderConnection:
    qualification = OpaqueProviderReference(
        value=stable_id(f"{case.name}:qualification")
    )
    required = required_capabilities(case)
    declared = set(required)
    if case.identity_assurance is ExternalIdentityAssurance.PROVIDER_STABLE:
        declared.add(ProviderCapabilityKind.STABLE_IDENTITY)
    return ProviderConnection(
        connection_version_id=stable_id(f"{case.name}:connection-version"),
        connection_id=stable_id(f"{case.name}:connection"),
        approval_id=stable_id(f"{case.name}:approval"),
        version=1,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=stable_id("service"),
        provider_kind=case.provider_kind,
        credential_reference=OpaqueProviderReference(
            value=stable_id(f"{case.name}:credential")
        ),
        grant_reference=OpaqueProviderReference(value=stable_id(f"{case.name}:grant")),
        approved_resource_scope_references=(
            OpaqueProviderReference(value=stable_id(f"{case.name}:scope")),
        ),
        authorization_generation=1,
        configuration_generation=1,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=tuple(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=qualification,
            )
            for kind in sorted(declared, key=lambda item: item.value)
        ),
    )


def required_capabilities(
    case: BinaryProviderCase,
) -> tuple[ProviderCapabilityKind, ...]:
    if case.provider_kind is ProviderKind.BITRIX24:
        return BITRIX24_DISK_REQUIRED_CAPABILITIES
    if case.provider_kind is ProviderKind.YANDEX_DISK:
        return (
            ProviderCapabilityKind.ENUMERATION,
            ProviderCapabilityKind.METADATA_READ,
            ProviderCapabilityKind.BINARY_READ,
        )
    return (ProviderCapabilityKind.BINARY_READ,)


async def _no_sleep(delay: float) -> None:
    del delay


async def read_provider_observation(
    case: BinaryProviderCase, root: Path
) -> tuple[NormalizedExternalObservationV2, int]:
    connection = provider_connection(case)
    if case.provider_kind is ProviderKind.GOOGLE:
        provider: _Provider = GoogleV2Adapter(
            GoogleDriveProviderAdapter(service=GoogleService(case), clock=lambda: NOW)
        )
    elif case.provider_kind is ProviderKind.YANDEX_DISK:
        provider = YandexDiskProviderAdapter(
            service=YandexService(case),
            profile=YandexDiskReadProfile(page_size=100, max_attempts=1),
            clock=lambda: NOW,
            sleep=_no_sleep,
        )
    elif case.provider_kind is ProviderKind.BITRIX24:
        provider = Bitrix24DiskProviderAdapter(
            service=BitrixService(case),
            profile=Bitrix24DiskReadProfile(max_attempts=1),
            clock=lambda: NOW,
            sleep=_no_sleep,
        )
    else:
        path = root / case.provider_object_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(EQUIVALENT_BINARY_CONTENT)
        provider = MountedFilesystemObservationReader(
            roots={
                connection.connection_version_id: MountedFilesystemRoot(
                    resource_scope_reference=connection.approved_resource_scope_references[
                        0
                    ],
                    path=root,
                )
            },
            clock=lambda: NOW,
        )
    repository = AdmittedConnectionRepository(connection)
    observation = await ProviderReadAdmission(
        repository=repository, provider=provider
    ).read(
        ProviderReadCommand(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            resource_scope_reference=connection.approved_resource_scope_references[0],
            authorization_generation=1,
            configuration_generation=1,
            required_capabilities=required_capabilities(case),
            resource_object_reference=case.provider_object_id,
        ),
        object(),
    )
    return observation, repository.admissions


async def read_tabular_observation() -> NormalizedExternalObservationV2:
    case = BINARY_PROVIDER_CASES[0]
    connection = provider_connection(case)
    adapter = GoogleSheetsProviderAdapter(
        service=GoogleSheetsService(),
        profile=GoogleSheetsReadProfile(
            spreadsheet_id="sheet-677",
            sheet_id=17,
            business_id_column="proposal_id",
            columns=(
                {"column_key": "proposal_id", "coordinate": "A"},
                {"column_key": "total", "coordinate": "B"},
            ),
        ),
        clock=lambda: NOW,
    )
    observed = await adapter.read(
        connection,
        GoogleReadCommand(
            connection_id=connection.connection_id,
            resource_scope_reference=OpaqueGoogleReference(
                value=connection.approved_resource_scope_references[0].value
            ),
            authorization_generation=1,
            configuration_generation=1,
        ),
    )
    return google_v1_observation_to_v2(
        observed.to_normalized_observation(),
        connection_version_id=connection.connection_version_id,
    )


async def read_structured_observation() -> NormalizedExternalObservationV2:
    case = BINARY_PROVIDER_CASES[2]
    base = provider_connection(case)
    qualification = OpaqueProviderReference(
        value=stable_id("bitrix24:crm-qualification")
    )
    connection = base.model_copy(
        update={
            "approved_crm_entity_types": ("deal",),
            "capabilities": (
                *base.capabilities,
                ProviderCapability(
                    kind=ProviderCapabilityKind.STABLE_IDENTITY,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=qualification,
                ),
                ProviderCapability(
                    kind=ProviderCapabilityKind.CRM,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=qualification,
                ),
                ProviderCapability(
                    kind=ProviderCapabilityKind.STRUCTURED_READ,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=qualification,
                ),
            ),
        }
    )
    crm_command = Bitrix24CrmMaterialReadCommand(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        resource_scope_reference=connection.approved_resource_scope_references[0],
        authorization_generation=1,
        configuration_generation=1,
        entity_type="deal",
        entity_id="932",
        activity_id="501",
        disk_file_id="9043",
        target_source_object_id=stable_id("target-source"),
        target_source_revision_id=stable_id("target-revision"),
        approved_entity_types=("deal",),
        required_capabilities=BITRIX24_CRM_REQUIRED_CAPABILITIES,
    )
    repository = AdmittedConnectionRepository(connection)
    provider = BitrixCrmV2Adapter(
        Bitrix24CrmMaterialAdapter(
            service=BitrixCrmService(),
            profile=Bitrix24CrmMaterialReadProfile(),
            clock=lambda: NOW,
        ),
        crm_command,
    )
    return await ProviderReadAdmission(repository=repository, provider=provider).read(
        ProviderReadCommand(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            connection_id=connection.connection_id,
            connection_version_id=connection.connection_version_id,
            resource_scope_reference=connection.approved_resource_scope_references[0],
            authorization_generation=1,
            configuration_generation=1,
            required_capabilities=BITRIX24_CRM_REQUIRED_CAPABILITIES,
            resource_object_reference="501:attachment-1",
        ),
        object(),
    )
