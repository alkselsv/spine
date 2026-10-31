"""Offline-safe ports and use cases for manual Google grant lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionMutationResult,
    GoogleConnectionRepository,
    confirmed_google_revocation_successor,
    revoked_connection_successor,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


class GoogleManualAuthorizationError(RuntimeError):
    """Disclosure-safe failure from a manual authorization lifecycle step."""

    def __init__(self) -> None:
        super().__init__("Google manual authorization could not be completed.")


@dataclass(frozen=True, slots=True)
class GoogleReauthorizationCommand:
    """A one-shot request for an exact successor connection version."""

    connection_id: UUID
    expected_current_version_id: UUID
    approval_id: UUID
    credential_reference: OpaqueGoogleReference
    grant_reference: OpaqueGoogleReference
    approved_resource_scope_references: tuple[OpaqueGoogleReference, ...]
    authorization_generation: int
    configuration_generation: int
    idempotency_key: str
    audit_event_id: UUID
    occurred_at: datetime

    def __post_init__(self) -> None:
        ids = (
            self.connection_id,
            self.expected_current_version_id,
            self.approval_id,
            self.audit_event_id,
        )
        if any(value.int == 0 for value in ids):
            raise ValueError("Google reauthorization identifiers must be non-nil")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.idempotency_key):
            raise ValueError("idempotency_key must be a bounded identifier")
        if self.authorization_generation < 1 or self.configuration_generation < 1:
            raise ValueError("Google connection generations must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if (
            not isinstance(self.approved_resource_scope_references, tuple)
            or not self.approved_resource_scope_references
            or any(
                not isinstance(reference, OpaqueGoogleReference)
                for reference in self.approved_resource_scope_references
            )
        ):
            raise ValueError("approved resource scopes are required")
        if len({item.value for item in self.approved_resource_scope_references}) != len(
            self.approved_resource_scope_references
        ):
            raise ValueError("approved resource scopes must be unique")

    def digest(self) -> str:
        payload = {
            "schema": "google-reauthorization-command:v1",
            "connection_id": str(self.connection_id),
            "expected_current_version_id": str(self.expected_current_version_id),
            "approval_id": str(self.approval_id),
            "credential_reference": str(self.credential_reference.value),
            "grant_reference": str(self.grant_reference.value),
            "scopes": sorted(str(item.value) for item in self.approved_resource_scope_references),
            "authorization_generation": self.authorization_generation,
            "configuration_generation": self.configuration_generation,
            "idempotency_key": self.idempotency_key,
            "audit_event_id": str(self.audit_event_id),
            "occurred_at": self.occurred_at.isoformat(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class GoogleManualReauthorization:
    """Build and append one approved successor through the canonical repository."""

    def __init__(self, repository: GoogleConnectionRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        current: GoogleConnection,
        command: GoogleReauthorizationCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnectionMutationResult:
        if (
            current.connection_id != command.connection_id
            or current.connection_version_id != command.expected_current_version_id
            or current.status is GoogleConnectionStatus.REVOKED
            or command.authorization_generation != current.authorization_generation + 1
            or command.configuration_generation != current.configuration_generation
            or command.credential_reference == current.credential_reference
            or command.grant_reference == current.grant_reference
        ):
            raise GoogleConnectionConflictError()
        successor = current.model_copy(
            update={
                "connection_version_id": uuid5(
                    NAMESPACE_URL,
                    f"spine:google-reauthorization:v1:{current.connection_id}:"
                    f"{command.idempotency_key}",
                ),
                "approval_id": command.approval_id,
                "version": current.version + 1,
                "predecessor_version_id": current.connection_version_id,
                "credential_reference": command.credential_reference,
                "grant_reference": command.grant_reference,
                "approved_resource_scope_references": command.approved_resource_scope_references,
                "authorization_generation": command.authorization_generation,
                "configuration_generation": command.configuration_generation,
                "status": GoogleConnectionStatus.ACTIVE,
            }
        )
        registration = GoogleConnectionCommand(
            connection=successor,
            idempotency_key=command.idempotency_key,
            audit_event_id=command.audit_event_id,
            occurred_at=command.occurred_at,
        )
        return await self._repository.register(registration, context)


@dataclass(frozen=True, slots=True)
class GoogleConsentResult:
    """Secret-bearing consent output; callers must persist it immediately."""

    client_configuration: SecretStr
    refresh_token: SecretStr


class GoogleDesktopConsentPort(Protocol):
    async def authorize(self, *, client_configuration: SecretStr) -> GoogleConsentResult: ...


class GoogleGrantRevocationPort(Protocol):
    async def revoke(self, *, refresh_token: SecretStr) -> bool: ...


class GoogleCredentialStoragePort(Protocol):
    async def read_refresh_token(self, reference: OpaqueGoogleReference) -> SecretStr: ...

    async def delete(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> bool: ...


class GoogleManualConsent:
    """Manual consent coordinator whose external effect is supplied by a port."""

    def __init__(self, consent: GoogleDesktopConsentPort) -> None:
        self._consent = consent

    async def authorize(self, *, client_configuration: SecretStr) -> GoogleConsentResult:
        try:
            result = await self._consent.authorize(client_configuration=client_configuration)
            if not result.client_configuration.get_secret_value() or not result.refresh_token.get_secret_value():
                raise GoogleManualAuthorizationError()
            return result
        except asyncio.CancelledError:
            raise
        except GoogleManualAuthorizationError:
            raise
        except Exception:
            raise GoogleManualAuthorizationError() from None


class GoogleGrantLifecycle:
    """Perform explicit remote revocation and separately verify local cleanup."""

    def __init__(
        self,
        *,
        credentials: GoogleCredentialStoragePort,
        revocation: GoogleGrantRevocationPort,
    ) -> None:
        self._credentials = credentials
        self._revocation = revocation

    async def revoke_remote(self, grant_reference: OpaqueGoogleReference) -> bool:
        try:
            token = await self._credentials.read_refresh_token(grant_reference)
            return await self._revocation.revoke(refresh_token=token)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def delete_local(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> bool:
        try:
            return await self._credentials.delete(credential_reference, grant_reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class GoogleManualRevocationResult:
    connection: GoogleConnection
    remote_revocation_confirmed: bool
    local_cleanup_verified: bool
    replay: bool


class GoogleManualRevocation:
    """Suspend canonical admission before explicit remote revoke and local cleanup."""

    def __init__(
        self,
        *,
        repository: GoogleConnectionRepository,
        grant_lifecycle: GoogleGrantLifecycle,
    ) -> None:
        self._repository = repository
        self._grant_lifecycle = grant_lifecycle

    async def execute(
        self,
        current: GoogleConnection,
        context: TrustedPersistenceContext,
        *,
        occurred_at: datetime,
    ) -> GoogleManualRevocationResult:
        canonical = await self._repository.get_current(current.connection_id, context)
        if canonical != current:
            raise GoogleConnectionConflictError()
        admitted_connection = current
        replay = False
        if current.status is GoogleConnectionStatus.ACTIVE:
            mutation = await self._repository.register(
                revoked_connection_successor(current, occurred_at=occurred_at), context
            )
            admitted_connection = mutation.connection
            replay = mutation.replay
        elif current.status is GoogleConnectionStatus.SUSPENDED:
            pass
        elif current.status is not GoogleConnectionStatus.REVOKED:
            raise GoogleConnectionConflictError()

        remote_confirmed = current.status is GoogleConnectionStatus.REVOKED
        if not remote_confirmed:
            remote_confirmed = await self._grant_lifecycle.revoke_remote(
                current.grant_reference
            )
            if not remote_confirmed:
                return GoogleManualRevocationResult(
                    connection=admitted_connection,
                    remote_revocation_confirmed=False,
                    local_cleanup_verified=False,
                    replay=replay,
                )
            revoked = await self._repository.register_revoked(
                confirmed_google_revocation_successor(
                    admitted_connection, occurred_at=occurred_at
                ),
                context,
            )
            admitted_connection = revoked.connection
            replay = replay or revoked.replay

        cleanup_verified = await self._grant_lifecycle.delete_local(
            current.credential_reference, current.grant_reference
        )
        return GoogleManualRevocationResult(
            connection=admitted_connection,
            remote_revocation_confirmed=remote_confirmed,
            local_cleanup_verified=cleanup_verified,
            replay=replay,
        )


__all__ = [
    "GoogleConsentResult",
    "GoogleCredentialStoragePort",
    "GoogleDesktopConsentPort",
    "GoogleGrantLifecycle",
    "GoogleGrantRevocationPort",
    "GoogleManualAuthorizationError",
    "GoogleManualConsent",
    "GoogleManualReauthorization",
    "GoogleManualRevocation",
    "GoogleManualRevocationResult",
    "GoogleReauthorizationCommand",
]
