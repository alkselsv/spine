"""Bounded, disclosure-safe discovery of approved Yandex Mail folders."""

from __future__ import annotations

import base64
import binascii
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, StrictBool, StrictBytes, StrictStr, model_validator

from spine.application.connections.email_read import MailboxReadCommand
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope


class YandexMailFolderDiscoveryCode(str, Enum):
    COMPLETE = "complete"
    PARTIAL_RESPONSE = "partial_response"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_LIMIT_EXCEEDED = "response_limit_exceeded"
    APPROVED_FOLDER_MISSING = "approved_folder_missing"
    APPROVED_FOLDER_AMBIGUOUS = "approved_folder_ambiguous"
    DISCOVERY_UNAVAILABLE = "discovery_unavailable"


class YandexMailListedFolder(DefinitionModel):
    """One raw LIST/LSUB tuple. The provider name is never rendered in repr."""

    schema_version: Literal["yandex-mail-listed-folder:v1"] = (
        "yandex-mail-listed-folder:v1"
    )
    flags: StrictBytes = Field(repr=False)
    delimiter: StrictBytes | None = Field(repr=False)
    name: StrictBytes = Field(repr=False)


class YandexMailFolderListing(DefinitionModel):
    """A bounded page of server listing tuples with explicit completion evidence."""

    schema_version: Literal["yandex-mail-folder-listing:v1"] = (
        "yandex-mail-folder-listing:v1"
    )
    folders: tuple[YandexMailListedFolder, ...] = Field(repr=False)
    complete: StrictBool


class YandexMailFolderBinding(DefinitionModel):
    """Approved provider folder name pinned to one exact mailbox generation."""

    schema_version: Literal["yandex-mail-folder-binding:v1"] = (
        "yandex-mail-folder-binding:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    mailbox_subject_reference: UUID
    folder_reference: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    scope_generation: int = Field(ge=1)
    # Provider folder names are approval data, not identity or display output.
    folder_name: StrictStr = Field(repr=False, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_binding(self) -> "YandexMailFolderBinding":
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.mailbox_binding_id,
            self.mailbox_subject_reference,
            self.folder_reference,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("Yandex Mail folder binding identifiers must be non-zero")
        return self


class YandexMailFolderBindingRepository(Protocol):
    async def resolve_exact(
        self,
        scope: MailboxReadScope,
        context: TrustedPersistenceContext,
    ) -> tuple[YandexMailFolderBinding, ...] | None: ...


class YandexMailFolderDiscoveryProfile(DefinitionModel):
    """Hard bounds for server-controlled LIST/LSUB data."""

    schema_version: Literal["yandex-mail-folder-discovery-profile:v1"] = (
        "yandex-mail-folder-discovery-profile:v1"
    )
    max_folders: int = Field(default=500, ge=1, le=5000)
    max_response_bytes: int = Field(default=1_048_576, ge=1024, le=8_388_608)


class YandexMailFolderDiscoveryResult(DefinitionModel):
    """Safe discovery evidence; it contains approved opaque refs, never names."""

    schema_version: Literal["yandex-mail-folder-discovery-result:v1"] = (
        "yandex-mail-folder-discovery-result:v1"
    )
    complete: bool
    code: YandexMailFolderDiscoveryCode
    discovered_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    folders: tuple[UUID, ...] = ()


class YandexMailFolderDiscoveryDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("Yandex Mail folder discovery denied.")


class _AdmittedMailboxEnumerator(Protocol):
    async def read(
        self,
        command: MailboxReadCommand,
        context: TrustedPersistenceContext,
    ) -> object: ...


async def discover_approved_yandex_mail_folders(
    *,
    reader: _AdmittedMailboxEnumerator,
    bindings: YandexMailFolderBindingRepository,
    command: MailboxReadCommand,
    context: TrustedPersistenceContext,
    profile: YandexMailFolderDiscoveryProfile = YandexMailFolderDiscoveryProfile(),
) -> YandexMailFolderDiscoveryResult:
    """List folders through mailbox-enumeration admission and return approved refs.

    Folder names are compared only within the exact connection, mailbox and
    authorization/configuration/scope generation. A response that cannot prove
    complete coverage never returns a complete result.
    """

    if command.operation is not EmailOperation.ENUMERATE_MAILBOX:
        raise YandexMailFolderDiscoveryDeniedError()
    try:
        approved = await bindings.resolve_exact(command.scope, context)
        if approved is None:
            raise YandexMailFolderDiscoveryDeniedError()
        scope = command.scope
        approved_by_name: dict[str, list[UUID]] = {}
        approved_references: set[UUID] = set()
        for binding in approved:
            if not _binding_matches_scope(binding, scope):
                raise YandexMailFolderDiscoveryDeniedError()
            if binding.folder_reference not in scope.allowed_folder_references:
                continue
            if binding.folder_reference in scope.excluded_folder_references:
                continue
            if binding.folder_reference in approved_references:
                raise YandexMailFolderDiscoveryDeniedError()
            approved_references.add(binding.folder_reference)
            approved_by_name.setdefault(binding.folder_name, []).append(
                binding.folder_reference
            )
        missing_bindings = set(scope.allowed_folder_references) - approved_references
        if missing_bindings:
            return _result(
                complete=False,
                code=YandexMailFolderDiscoveryCode.APPROVED_FOLDER_MISSING,
                discovered_count=0,
                approved_count=len(approved_references),
            )
        command = command.model_copy(
            update={
                "enumeration_max_items": profile.max_folders,
                "enumeration_max_response_bytes": profile.max_response_bytes,
            }
        )
        listing = await reader.read(command, context)
    except YandexMailFolderDiscoveryDeniedError:
        raise
    except Exception:
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.DISCOVERY_UNAVAILABLE,
            discovered_count=0,
        )
    if not isinstance(listing, YandexMailFolderListing):
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.MALFORMED_RESPONSE,
            discovered_count=0,
        )
    if not isinstance(listing.folders, tuple) or not isinstance(listing.complete, bool):
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.MALFORMED_RESPONSE,
            discovered_count=0,
        )
    if len(listing.folders) > profile.max_folders:
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.RESPONSE_LIMIT_EXCEEDED,
            discovered_count=len(listing.folders),
        )
    response_size = 0
    decoded_names: list[str] = []
    malformed = False
    for folder in listing.folders:
        if not isinstance(folder, YandexMailListedFolder):
            malformed = True
            continue
        if not isinstance(folder.flags, bytes) or not isinstance(folder.name, bytes):
            malformed = True
            continue
        if folder.delimiter is not None and not isinstance(folder.delimiter, bytes):
            malformed = True
            continue
        response_size += len(folder.flags) + len(folder.name)
        if folder.delimiter is not None:
            response_size += len(folder.delimiter)
        if response_size > profile.max_response_bytes:
            return _result(
                complete=False,
                code=YandexMailFolderDiscoveryCode.RESPONSE_LIMIT_EXCEEDED,
                discovered_count=len(listing.folders),
            )
        try:
            name = _decode_folder_name(folder.name)
            _decode_delimiter(folder.delimiter)
        except (UnicodeError, ValueError, binascii.Error):
            malformed = True
            continue
        decoded_names.append(name)

    if malformed:
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.MALFORMED_RESPONSE,
            discovered_count=len(listing.folders),
        )
    if not listing.complete:
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.PARTIAL_RESPONSE,
            discovered_count=len(set(decoded_names)),
            approved_count=sum(
                1 for name in set(decoded_names) if approved_by_name.get(name)
            ),
        )

    # Duplicate wire tuples are harmless; conflicting matches are not.
    unique_names = set(decoded_names)
    matching: dict[str, UUID] = {}
    for name in unique_names:
        refs = approved_by_name.get(name, [])
        if len(refs) > 1:
            return _result(
                complete=False,
                code=YandexMailFolderDiscoveryCode.APPROVED_FOLDER_AMBIGUOUS,
                discovered_count=len(unique_names),
            )
        if refs:
            matching[name] = refs[0]

    missing = set(approved_by_name) - matching.keys()
    if missing:
        return _result(
            complete=False,
            code=YandexMailFolderDiscoveryCode.APPROVED_FOLDER_MISSING,
            discovered_count=len(unique_names),
            approved_count=len(matching),
        )
    return YandexMailFolderDiscoveryResult(
        complete=True,
        code=YandexMailFolderDiscoveryCode.COMPLETE,
        discovered_count=len(unique_names),
        approved_count=len(matching),
        folders=tuple(sorted(matching.values(), key=lambda value: value.int)),
    )


def _result(
    *,
    complete: bool,
    code: YandexMailFolderDiscoveryCode,
    discovered_count: int,
    approved_count: int = 0,
) -> YandexMailFolderDiscoveryResult:
    return YandexMailFolderDiscoveryResult(
        complete=complete,
        code=code,
        discovered_count=discovered_count,
        approved_count=approved_count,
    )


def _binding_matches_scope(
    binding: YandexMailFolderBinding,
    scope: MailboxReadScope,
) -> bool:
    return (
        binding.workspace_id == scope.workspace_id
        and binding.environment_id == scope.environment_id
        and binding.connection_id == scope.connection_id
        and binding.connection_version_id == scope.connection_version_id
        and binding.mailbox_binding_id == scope.mailbox_binding_id
        and binding.mailbox_subject_reference == scope.mailbox_subject_reference
        and binding.authorization_generation == scope.authorization_generation
        and binding.configuration_generation == scope.configuration_generation
        and binding.scope_generation == scope.scope_generation
    )


def _decode_delimiter(value: bytes | None) -> str | None:
    if value is None:
        return None
    decoded = value.decode("utf-8", errors="strict")
    if len(decoded) != 1 or decoded in "\r\n\x00":
        raise ValueError("malformed folder delimiter")
    return decoded


def _decode_folder_name(value: bytes) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501) and UTF-8 response names."""

    try:
        source = value.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        decoded = value.decode("utf-8", errors="strict")
        if any(character in decoded for character in "\x00\r\n"):
            raise ValueError("malformed folder name")
        return decoded
    output: list[str] = []
    index = 0
    while index < len(source):
        if source[index] != "&":
            output.append(source[index])
            index += 1
            continue
        end = source.find("-", index)
        if end < 0:
            raise ValueError("unterminated modified UTF-7 sequence")
        encoded = source[index + 1 : end]
        if not encoded:
            output.append("&")
        else:
            if "=" in encoded:
                raise ValueError("padded modified UTF-7 sequence")
            padded = encoded.replace(",", "/")
            padded += "=" * (-len(padded) % 4)
            raw = base64.b64decode(padded, validate=True)
            if len(raw) % 2:
                raise ValueError("invalid modified UTF-7 sequence")
            output.append(raw.decode("utf-16-be", errors="strict"))
        index = end + 1
    decoded = "".join(output)
    if any(character in decoded for character in "\x00\r\n"):
        raise ValueError("malformed folder name")
    return decoded


__all__ = [
    "YandexMailFolderBinding",
    "YandexMailFolderBindingRepository",
    "YandexMailFolderDiscoveryCode",
    "YandexMailFolderDiscoveryDeniedError",
    "YandexMailFolderDiscoveryProfile",
    "YandexMailFolderDiscoveryResult",
    "YandexMailFolderListing",
    "YandexMailListedFolder",
    "discover_approved_yandex_mail_folders",
]
