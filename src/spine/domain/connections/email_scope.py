"""Exact, immutable mailbox read scopes for governed email providers."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


class EmailProviderOperation(str, Enum):
    """Provider-side reads admitted independently for a mailbox scope."""

    ENUMERATE_MAILBOX = "enumerate_mailbox"
    READ_MESSAGE_METADATA = "read_message_metadata"
    READ_RAW_MESSAGE = "read_raw_message"
    READ_ATTACHMENT = "read_attachment"
    SYNCHRONIZE = "synchronize"


class EmailOperation(str, Enum):
    """Requested email actions, including explicitly separate content authority."""

    ENUMERATE_MAILBOX = "enumerate_mailbox"
    READ_MESSAGE_METADATA = "read_message_metadata"
    READ_RAW_MESSAGE = "read_raw_message"
    READ_ATTACHMENT = "read_attachment"
    SYNCHRONIZE = "synchronize"
    PROCESS_CONTENT = "process_content"
    READ_CONTENT = "read_content"


class MailboxReadScope(DefinitionModel):
    """One immutable authorization binding, containing opaque refs only."""

    schema_version: Literal["mailbox-read-scope:v1"] = "mailbox-read-scope:v1"
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    provider_kind: Literal["google", "yandex_mail"]
    resource_scope_reference: UUID
    mailbox_subject_reference: UUID
    mailbox_binding_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    scope_generation: int = Field(ge=1)
    allowed_folder_references: tuple[UUID, ...] = ()
    excluded_folder_references: tuple[UUID, ...] = ()
    allowed_label_references: tuple[UUID, ...] = ()
    excluded_label_references: tuple[UUID, ...] = ()
    allowed_operations: tuple[EmailProviderOperation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "MailboxReadScope":
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.resource_scope_reference,
            self.mailbox_subject_reference,
            self.mailbox_binding_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("mailbox scope identifiers must be non-zero")
        for field_name in (
            "allowed_folder_references",
            "excluded_folder_references",
            "allowed_label_references",
            "excluded_label_references",
        ):
            values = getattr(self, field_name)
            if any(value.int == 0 for value in values) or len(set(values)) != len(values):
                raise ValueError(f"{field_name} must contain unique non-zero references")
            object.__setattr__(self, field_name, tuple(sorted(values, key=lambda item: item.int)))
        if set(self.allowed_folder_references) & set(self.excluded_folder_references):
            raise ValueError("folder references cannot be both allowed and excluded")
        if set(self.allowed_label_references) & set(self.excluded_label_references):
            raise ValueError("label references cannot be both allowed and excluded")
        if self.provider_kind == "google" and self.allowed_folder_references:
            raise ValueError("Google mailbox scopes use label references")
        if self.provider_kind == "google" and self.excluded_folder_references:
            raise ValueError("Google mailbox scopes use label references")
        if self.provider_kind == "yandex_mail" and self.allowed_label_references:
            raise ValueError("Yandex Mail scopes use folder references")
        if self.provider_kind == "yandex_mail" and self.excluded_label_references:
            raise ValueError("Yandex Mail scopes use folder references")
        if len(set(self.allowed_operations)) != len(self.allowed_operations):
            raise ValueError("mailbox scope operations must be unique")
        return self

    def allows(self, operation: EmailOperation) -> bool:
        """Return whether this scope authorizes one provider-side operation."""

        try:
            provider_operation = EmailProviderOperation(operation.value)
        except ValueError:
            return False
        return provider_operation in self.allowed_operations

    def allows_location(self, reference: UUID | None) -> bool:
        """Check an exact folder/label ref against allow and exclusion sets."""

        if self.provider_kind == "google":
            allowed, excluded = self.allowed_label_references, self.excluded_label_references
        else:
            allowed, excluded = self.allowed_folder_references, self.excluded_folder_references
        if reference is None or reference in excluded:
            return False
        return reference in allowed


__all__ = ["EmailOperation", "EmailProviderOperation", "MailboxReadScope"]
