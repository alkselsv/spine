"""Offline Bitrix24 OAuth admission, lifecycle and refresh contracts."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, TypeAlias
from uuid import UUID

from spine.domain.connections.bitrix24_oauth import (
    Bitrix24GrantAuthority,
    Bitrix24GrantState,
    Bitrix24Scope,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderCapabilityKind,
    ProviderKind,
)
from spine.application.connections.provider import ProviderReadCommand
from spine.connectors.observations_v2 import NormalizedExternalObservationV2


class Bitrix24OAuthError(RuntimeError):
    """Base class for disclosure-safe Bitrix24 OAuth failures."""


class ActivationCommitUnknown(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 pair activation commit outcome is unknown.")


class Bitrix24GrantDenied(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 grant access denied.")


class Bitrix24IdentityMismatch(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 acting-user identity mismatch.")


class Bitrix24ProfileUnavailable(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 identity profile is unavailable.")


class Bitrix24IdentityUncertain(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 acting-user identity is uncertain.")


class Bitrix24AccessTokenExpired(Bitrix24OAuthError):
    """A provider call proved that the pinned access token has expired."""

    def __init__(self) -> None:
        super().__init__("Bitrix24 access token expired.")
        self.pinned: Bitrix24PinnedGrant | None = None


class ObjectReadDenied(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 object read denied.")


class StaleReadFenced(Bitrix24OAuthError):
    def __init__(self) -> None:
        super().__init__("Bitrix24 read was fenced by newer authority.")


@dataclass(frozen=True, slots=True)
class Bitrix24ContainmentCommand:
    connection: ProviderConnection
    binding_id: UUID
    expected_security_fence: int
    incident_reference: OpaqueProviderReference
    command_id: UUID

    def __post_init__(self) -> None:
        if self.binding_id.int == 0 or self.command_id.int == 0:
            raise ValueError("containment IDs must be non-nil UUIDs")
        if self.expected_security_fence < 0:
            raise ValueError("expected_security_fence must be non-negative")


@dataclass(frozen=True, slots=True)
class GrantContained:
    binding_id: UUID
    security_fence: int
    incident_reference: OpaqueProviderReference


@dataclass(frozen=True, slots=True)
class ContainmentDenied:
    binding_id: UUID


class GrantContainmentAuthority(Protocol):
    async def authorize(self, command: Bitrix24ContainmentCommand) -> bool: ...


class GrantContainmentRepository(Protocol):
    async def contain(
        self, command: Bitrix24ContainmentCommand
    ) -> GrantContained | StaleReadFenced: ...


class Bitrix24GrantContainment:
    def __init__(
        self,
        *,
        repository: GrantContainmentRepository,
        authority: GrantContainmentAuthority,
    ) -> None:
        self._repository = repository
        self._authority = authority

    async def contain(
        self, command: Bitrix24ContainmentCommand
    ) -> GrantContained | ContainmentDenied | StaleReadFenced:
        if not await self._authority.authorize(command):
            return ContainmentDenied(binding_id=command.binding_id)
        return await self._repository.contain(command)


@dataclass(frozen=True, slots=True)
class Bitrix24RecoveryCommand:
    contained_connection: ProviderConnection
    replacement_connection: ProviderConnection
    replacement_authority: Bitrix24GrantAuthority
    recovery_reference: OpaqueProviderReference
    command_id: UUID

    def __post_init__(self) -> None:
        previous = self.contained_connection
        replacement = self.replacement_connection
        binding = self.replacement_authority.binding
        if self.command_id.int == 0:
            raise ValueError("command_id must be a non-nil UUID")
        if (
            previous.provider_kind is not ProviderKind.BITRIX24
            or replacement.provider_kind is not ProviderKind.BITRIX24
            or (
                replacement.workspace_id,
                replacement.environment_id,
                replacement.connection_id,
            )
            != (
                previous.workspace_id,
                previous.environment_id,
                previous.connection_id,
            )
            or replacement.version != previous.version + 1
            or replacement.predecessor_version_id != previous.connection_version_id
            or replacement.grant_reference == previous.grant_reference
            or replacement.authorization_generation <= previous.authorization_generation
            or replacement.configuration_generation < previous.configuration_generation
            or self.replacement_authority.state is not Bitrix24GrantState.READY
            or self.replacement_authority.security_fence != 0
            or self.replacement_authority.token_epoch != 1
            or (
                binding.workspace_id,
                binding.environment_id,
                binding.approval_id,
                binding.connection_id,
                binding.connection_version_id,
                binding.grant_reference,
                binding.authorization_generation,
                binding.configuration_generation,
            )
            != (
                replacement.workspace_id,
                replacement.environment_id,
                replacement.approval_id,
                replacement.connection_id,
                replacement.connection_version_id,
                replacement.grant_reference,
                replacement.authorization_generation,
                replacement.configuration_generation,
            )
        ):
            raise ValueError("recovery requires an exact fresh connection binding")


@dataclass(frozen=True, slots=True)
class RecoveryDenied:
    binding_id: UUID


@dataclass(frozen=True, slots=True)
class GrantRecovered:
    previous_binding_id: UUID
    binding_id: UUID
    token_epoch: int


class GrantRecoveryAuthority(Protocol):
    async def authorize(self, command: Bitrix24RecoveryCommand) -> bool: ...


class GrantRecoveryRepository(Protocol):
    async def current_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None: ...

    async def recover(
        self, command: Bitrix24RecoveryCommand
    ) -> GrantRecovered | RecoveryDenied: ...


class Bitrix24GrantRecovery:
    def __init__(
        self,
        *,
        repository: GrantRecoveryRepository,
        authority: GrantRecoveryAuthority,
        pair_store: TokenPairStore,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._pair_store = pair_store

    async def recover(
        self, command: Bitrix24RecoveryCommand
    ) -> GrantRecovered | RecoveryDenied:
        current = await self._repository.current_authority(
            command.contained_connection
        )
        if (
            current is None
            or current.state is not Bitrix24GrantState.CONTAINED
            or not await self._authority.authorize(command)
            or command.replacement_authority.active_pair_reference
            == current.active_pair_reference
            or not await self._pair_store.verify_candidate(
                command.replacement_authority.active_pair_reference
            )
        ):
            return RecoveryDenied(
                binding_id=(
                    current.binding.binding_id
                    if current is not None
                    else command.replacement_authority.binding.binding_id
                )
            )
        return await self._repository.recover(command)


@dataclass(frozen=True, slots=True, repr=False)
class SecretTokenPair:
    """Ephemeral pair material; never a domain, audit or response contract."""

    access_token: str
    refresh_token: str

    def __post_init__(self) -> None:
        if not self.access_token or not self.refresh_token:
            raise ValueError("token pair must be complete")


@dataclass(frozen=True, slots=True)
class Bitrix24PinnedGrant:
    binding_id: UUID
    acting_user_id: str
    token_epoch: int
    security_fence: int
    tokens: SecretTokenPair = field(repr=False)


class Bitrix24GrantAuthorityRepository(Protocol):
    async def current_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None: ...


class ActingUserGrantResolver(Protocol):
    async def resolve(self, authority: Bitrix24GrantAuthority) -> SecretTokenPair: ...


@dataclass(frozen=True, slots=True, repr=False)
class SecretApplicationCredential:
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("application credential must be complete")


@dataclass(frozen=True, slots=True)
class Bitrix24RefreshCommand:
    connection: ProviderConnection
    expected_token_epoch: int
    attempt_id: UUID

    def __post_init__(self) -> None:
        if self.expected_token_epoch < 1:
            raise ValueError("expected_token_epoch must be positive")
        if self.attempt_id.int == 0:
            raise ValueError("attempt_id must be a non-nil UUID")


@dataclass(frozen=True, slots=True)
class RefreshClaim:
    attempt_id: UUID
    authority: Bitrix24GrantAuthority


@dataclass(frozen=True, slots=True)
class RefreshBusy:
    binding_id: UUID
    token_epoch: int


@dataclass(frozen=True, slots=True)
class StaleRefreshFenced:
    binding_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PairActivated:
    binding_id: UUID
    pair_reference: OpaqueProviderReference
    token_epoch: int


@dataclass(frozen=True, slots=True)
class RefreshedTokenPair:
    tokens: SecretTokenPair = field(repr=False)


@dataclass(frozen=True, slots=True)
class RefreshTransportUncertain:
    """The provider may have accepted refresh but no complete response is known."""


@dataclass(frozen=True, slots=True)
class RefreshUncertain:
    binding_id: UUID
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class RefreshInvalidGrant:
    terminal_evidence: bool


@dataclass(frozen=True, slots=True)
class GrantUncertain:
    binding_id: UUID
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class ReauthorizationRequired:
    binding_id: UUID


class Bitrix24ConfirmedUninstall(str, Enum):
    DENIED = "denied"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True, slots=True)
class Bitrix24UninstallEvidence:
    """Qualified, refs-only evidence for one exact provider uninstall event."""

    connection: ProviderConnection
    binding_id: UUID
    portal_member_id: str
    application_id: str
    qualification_reference: OpaqueProviderReference
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.binding_id.int == 0:
            raise ValueError("binding_id must be a non-nil UUID")
        _require_provider_identity(self.portal_member_id, "portal_member_id")
        _require_provider_identity(self.application_id, "application_id")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


class Bitrix24UninstallAuthority(Protocol):
    async def authorize(self, evidence: Bitrix24UninstallEvidence) -> bool: ...


class Bitrix24GrantLossRepository(Protocol):
    async def confirm_uninstall(
        self, evidence: Bitrix24UninstallEvidence
    ) -> ReauthorizationRequired | Bitrix24ConfirmedUninstall: ...


class Bitrix24GrantLoss:
    """Apply terminal loss only from separately authorized uninstall evidence."""

    def __init__(
        self,
        *,
        repository: Bitrix24GrantLossRepository,
        authority: Bitrix24UninstallAuthority,
    ) -> None:
        self._repository = repository
        self._authority = authority

    async def confirm_uninstall(
        self, evidence: Bitrix24UninstallEvidence
    ) -> ReauthorizationRequired | Bitrix24ConfirmedUninstall:
        if not await self._authority.authorize(evidence):
            return Bitrix24ConfirmedUninstall.DENIED
        return await self._repository.confirm_uninstall(evidence)


class ApplicationCredentialResolver(Protocol):
    async def resolve(
        self, credential_reference: OpaqueProviderReference
    ) -> SecretApplicationCredential: ...


class TokenPairStore(Protocol):
    async def create_candidate(
        self, claim: RefreshClaim, tokens: SecretTokenPair
    ) -> OpaqueProviderReference: ...

    async def verify_candidate(self, reference: OpaqueProviderReference) -> bool: ...

    async def discard_candidate(
        self,
        reference: OpaqueProviderReference,
        *,
        active_references: frozenset[OpaqueProviderReference],
        incident_retained: bool,
    ) -> bool: ...


class Bitrix24RefreshRepository(Bitrix24GrantAuthorityRepository, Protocol):
    async def claim_refresh(
        self, command: Bitrix24RefreshCommand
    ) -> RefreshClaim | RefreshBusy | RefreshUncertain | StaleRefreshFenced: ...

    async def activate_candidate(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | StaleRefreshFenced: ...

    async def resolve_activation(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | RefreshUncertain | StaleRefreshFenced: ...

    async def mark_refresh_uncertain(
        self, claim: RefreshClaim
    ) -> RefreshUncertain | StaleRefreshFenced: ...

    async def mark_grant_uncertain(
        self, claim: RefreshClaim
    ) -> GrantUncertain | StaleRefreshFenced: ...

    async def require_reauthorization(
        self, claim: RefreshClaim
    ) -> ReauthorizationRequired | StaleRefreshFenced: ...


class Bitrix24RefreshTransport(Protocol):
    async def refresh(
        self,
        credential: SecretApplicationCredential,
        current_pair: SecretTokenPair,
    ) -> RefreshTransportOutcome: ...


RefreshTransportOutcome: TypeAlias = (
    RefreshedTokenPair | RefreshTransportUncertain | RefreshInvalidGrant
)
RefreshOutcome: TypeAlias = (
    PairActivated
    | RefreshBusy
    | RefreshUncertain
    | GrantUncertain
    | ReauthorizationRequired
    | StaleRefreshFenced
)


class Bitrix24RefreshCoordinator:
    """Coordinate one durable refresh attempt without assuming distributed commit."""

    def __init__(
        self,
        *,
        repository: Bitrix24RefreshRepository,
        credential_resolver: ApplicationCredentialResolver,
        grant_resolver: ActingUserGrantResolver,
        pair_store: TokenPairStore,
        transport: Bitrix24RefreshTransport,
    ) -> None:
        self._repository = repository
        self._credential_resolver = credential_resolver
        self._grant_resolver = grant_resolver
        self._pair_store = pair_store
        self._transport = transport

    async def refresh(self, command: Bitrix24RefreshCommand) -> RefreshOutcome:
        claim = await self._repository.claim_refresh(command)
        if not isinstance(claim, RefreshClaim):
            return claim
        credential = await self._credential_resolver.resolve(
            command.connection.credential_reference
        )
        current_pair = await self._grant_resolver.resolve(claim.authority)
        refreshed = await self._transport.refresh(credential, current_pair)
        if isinstance(refreshed, RefreshTransportUncertain):
            return await self._repository.mark_refresh_uncertain(claim)
        if isinstance(refreshed, RefreshInvalidGrant):
            if refreshed.terminal_evidence:
                return await self._repository.require_reauthorization(claim)
            return await self._repository.mark_grant_uncertain(claim)
        if not isinstance(refreshed, RefreshedTokenPair):
            raise Bitrix24OAuthError(
                "Bitrix24 refresh returned an unsupported outcome."
            )
        try:
            candidate = await self._pair_store.create_candidate(claim, refreshed.tokens)
            candidate_available = await self._pair_store.verify_candidate(candidate)
        except Exception:
            return await self._repository.mark_refresh_uncertain(claim)
        if not candidate_available:
            return await self._repository.mark_refresh_uncertain(claim)
        try:
            return await self._repository.activate_candidate(claim, candidate)
        except ActivationCommitUnknown:
            return await self._repository.resolve_activation(claim, candidate)


class Bitrix24RefreshExecutor(Protocol):
    async def refresh(self, command: Bitrix24RefreshCommand) -> RefreshOutcome: ...


class Bitrix24ExpiredGrantRefresh:
    """Bridge an expired pinned read to one durable refresh claim."""

    def __init__(
        self,
        *,
        coordinator: Bitrix24RefreshExecutor,
        attempt_id_factory: Callable[[], UUID],
    ) -> None:
        self._coordinator = coordinator
        self._attempt_id_factory = attempt_id_factory

    async def refresh_expired(
        self, connection: ProviderConnection, pinned: Bitrix24PinnedGrant
    ) -> RefreshOutcome:
        return await self._coordinator.refresh(
            Bitrix24RefreshCommand(
                connection=connection,
                expected_token_epoch=pinned.token_epoch,
                attempt_id=self._attempt_id_factory(),
            )
        )


@dataclass(frozen=True, slots=True)
class Bitrix24CallbackCommand:
    """Secret-free handle to one bounded callback staged by ingress."""

    workspace_id: UUID
    environment_id: UUID
    application_id: str
    portal_member_id_hint: str
    event_kind: str
    callback_reference: OpaqueProviderReference
    payload_fingerprint: str
    received_at: datetime

    def __post_init__(self) -> None:
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("callback scope IDs must be non-nil UUIDs")
        _require_provider_identity(self.application_id, "application_id")
        _require_provider_identity(self.portal_member_id_hint, "portal_member_id_hint")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", self.event_kind) is None:
            raise ValueError("event_kind must be a bounded provider event")
        if re.fullmatch(r"[0-9a-f]{64}", self.payload_fingerprint) is None:
            raise ValueError("payload_fingerprint must be a SHA-256 digest")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CallbackUnsupportedDropped:
    event_kind: str


class BootstrapAttemptState(str, Enum):
    PENDING = "pending"
    UNCERTAIN = "uncertain"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class Bitrix24BootstrapAttempt:
    attempt_id: UUID
    workspace_id: UUID
    environment_id: UUID
    approval_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    grant_reference: OpaqueProviderReference
    portal_member_id: str
    application_id: str
    installing_user_id: str
    approved_scopes: tuple[str, ...]
    authorization_generation: int
    configuration_generation: int
    expires_at: datetime
    state: BootstrapAttemptState = BootstrapAttemptState.PENDING

    def __post_init__(self) -> None:
        if any(
            item.int == 0
            for item in (
                self.attempt_id,
                self.workspace_id,
                self.environment_id,
                self.approval_id,
                self.connection_id,
                self.connection_version_id,
            )
        ):
            raise ValueError("bootstrap IDs must be non-nil UUIDs")
        _require_provider_identity(self.portal_member_id, "portal_member_id")
        _require_provider_identity(self.application_id, "application_id")
        _require_provider_identity(self.installing_user_id, "installing_user_id")
        if not self.approved_scopes or len(set(self.approved_scopes)) != len(
            self.approved_scopes
        ):
            raise ValueError("approved_scopes must be non-empty and unique")
        if self.approved_scopes != (Bitrix24Scope.DISK.value,):
            raise ValueError("offline bootstrap approves only the disk scope")
        if self.authorization_generation < 1 or self.configuration_generation < 1:
            raise ValueError("bootstrap generations must be positive")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BootstrapAttemptCreated:
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class BootstrapRetryBlocked:
    existing_attempt_id: UUID


@dataclass(frozen=True, slots=True)
class BootstrapMarkedUncertain:
    attempt_id: UUID


class Bitrix24BootstrapRepository(Protocol):
    async def create_attempt(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapAttemptCreated | BootstrapRetryBlocked: ...


class Bitrix24BootstrapCoordinator:
    def __init__(self, *, repository: Bitrix24BootstrapRepository) -> None:
        self._repository = repository

    async def start(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapAttemptCreated | BootstrapRetryBlocked:
        if attempt.state is not BootstrapAttemptState.PENDING:
            raise ValueError("a new bootstrap attempt must start pending")
        return await self._repository.create_attempt(attempt)


class CallbackVerificationStatus(str, Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class CallbackVerification:
    status: CallbackVerificationStatus
    portal_member_id: str | None = None
    application_id: str | None = None
    acting_user_id: str | None = None
    granted_scopes: tuple[str, ...] = ()
    pair_candidate_reference: OpaqueProviderReference | None = None

    def __post_init__(self) -> None:
        if self.status is CallbackVerificationStatus.VERIFIED:
            for name, value in (
                ("portal_member_id", self.portal_member_id),
                ("application_id", self.application_id),
                ("acting_user_id", self.acting_user_id),
            ):
                _require_provider_identity(value, name)
            if (
                self.pair_candidate_reference is None
                or not self.granted_scopes
                or len(set(self.granted_scopes)) != len(self.granted_scopes)
            ):
                raise ValueError("verified callback evidence is incomplete")
        elif (
            any(
                value is not None
                for value in (
                    self.portal_member_id,
                    self.application_id,
                    self.acting_user_id,
                    self.pair_candidate_reference,
                )
            )
            or self.granted_scopes
        ):
            raise ValueError("failed verification cannot carry provider evidence")


@dataclass(frozen=True, slots=True)
class CallbackAccepted:
    receipt_id: UUID
    binding_id: UUID


@dataclass(frozen=True, slots=True)
class CallbackDuplicate:
    receipt_id: UUID
    binding_id: UUID


@dataclass(frozen=True, slots=True)
class CallbackConflicting:
    callback_reference: OpaqueProviderReference


@dataclass(frozen=True, slots=True)
class CallbackAmbiguous:
    candidate_count: int


@dataclass(frozen=True, slots=True)
class CallbackLate:
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class CallbackIdentityMismatch:
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class CallbackIdentityUncertain:
    attempt_id: UUID


CallbackReceiptOutcome: TypeAlias = (
    CallbackAccepted | CallbackDuplicate | CallbackConflicting
)
CallbackOutcome: TypeAlias = (
    CallbackReceiptOutcome
    | CallbackAmbiguous
    | CallbackLate
    | CallbackIdentityMismatch
    | CallbackIdentityUncertain
    | CallbackUnsupportedDropped
)


@dataclass(frozen=True, slots=True)
class Bitrix24ActivationRequest:
    connection: ProviderConnection
    receipt_id: UUID
    command_id: UUID
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.receipt_id.int == 0 or self.command_id.int == 0:
            raise ValueError("activation IDs must be non-nil UUIDs")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if (
            self.connection.provider_kind is not ProviderKind.BITRIX24
            or self.connection.status is not ProviderConnectionStatus.ACTIVE
        ):
            raise ValueError("activation requires an active Bitrix24 connection")


@dataclass(frozen=True, slots=True)
class Bitrix24ActivationAdmission:
    binding_id: UUID
    pair_candidate_reference: OpaqueProviderReference


@dataclass(frozen=True, slots=True)
class ConnectionActivated:
    connection_version_id: UUID
    binding_id: UUID
    token_epoch: int


@dataclass(frozen=True, slots=True)
class ActivationDenied:
    receipt_id: UUID


ActivationOutcome: TypeAlias = ConnectionActivated | ActivationDenied


class Bitrix24ActivationAuthority(Protocol):
    async def authorize(self, request: Bitrix24ActivationRequest) -> bool: ...


class Bitrix24ActivationRepository(Protocol):
    async def admit_activation(
        self, request: Bitrix24ActivationRequest
    ) -> Bitrix24ActivationAdmission | ActivationDenied: ...

    async def activate(
        self,
        request: Bitrix24ActivationRequest,
        admission: Bitrix24ActivationAdmission,
    ) -> ActivationOutcome: ...


class Bitrix24ConnectionActivation:
    """Activate verified callback evidence only under separate control authority."""

    def __init__(
        self,
        *,
        repository: Bitrix24ActivationRepository,
        authority: Bitrix24ActivationAuthority,
        pair_store: TokenPairStore,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._pair_store = pair_store

    async def activate(self, request: Bitrix24ActivationRequest) -> ActivationOutcome:
        if not await self._authority.authorize(request):
            return ActivationDenied(receipt_id=request.receipt_id)
        admission = await self._repository.admit_activation(request)
        if isinstance(admission, ActivationDenied):
            return admission
        if not await self._pair_store.verify_candidate(
            admission.pair_candidate_reference
        ):
            return ActivationDenied(receipt_id=request.receipt_id)
        return await self._repository.activate(request, admission)


class InstallationCallbackRepository(Protocol):
    async def existing_receipt(
        self, command: Bitrix24CallbackCommand
    ) -> CallbackDuplicate | CallbackConflicting | None: ...

    async def find_pending(
        self, command: Bitrix24CallbackCommand
    ) -> tuple[Bitrix24BootstrapAttempt, ...]: ...

    async def mark_attempt_uncertain(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapMarkedUncertain: ...

    async def record_verified(
        self,
        command: Bitrix24CallbackCommand,
        attempt: Bitrix24BootstrapAttempt,
        verification: CallbackVerification,
    ) -> CallbackReceiptOutcome: ...


class InstallationCallbackVerifier(Protocol):
    async def verify(
        self,
        attempt: Bitrix24BootstrapAttempt,
        callback_reference: OpaqueProviderReference,
    ) -> CallbackVerification: ...


class Bitrix24CallbackDispatcher:
    """Discriminate callbacks before any grant or verification material is used."""

    def __init__(
        self,
        *,
        repository: InstallationCallbackRepository,
        verifier: InstallationCallbackVerifier,
    ) -> None:
        self._repository = repository
        self._verifier = verifier

    async def dispatch(self, command: Bitrix24CallbackCommand) -> CallbackOutcome:
        if command.event_kind != "ONAPPINSTALL":
            return CallbackUnsupportedDropped(event_kind=command.event_kind)
        existing = await self._repository.existing_receipt(command)
        if existing is not None:
            return existing
        candidates = await self._repository.find_pending(command)
        if len(candidates) != 1:
            return CallbackAmbiguous(candidate_count=len(candidates))
        attempt = candidates[0]
        if command.received_at > attempt.expires_at:
            await self._repository.mark_attempt_uncertain(attempt)
            return CallbackLate(attempt_id=attempt.attempt_id)
        verification = await self._verifier.verify(attempt, command.callback_reference)
        if verification.status is CallbackVerificationStatus.UNCERTAIN:
            return CallbackIdentityUncertain(attempt_id=attempt.attempt_id)
        if verification.status is CallbackVerificationStatus.MISMATCH or not (
            verification.portal_member_id == attempt.portal_member_id
            and verification.application_id == attempt.application_id
            and verification.acting_user_id == attempt.installing_user_id
            and frozenset(verification.granted_scopes)
            == frozenset(attempt.approved_scopes)
            and command.workspace_id == attempt.workspace_id
            and command.environment_id == attempt.environment_id
            and command.application_id == attempt.application_id
            and command.portal_member_id_hint == attempt.portal_member_id
        ):
            return CallbackIdentityMismatch(attempt_id=attempt.attempt_id)
        return await self._repository.record_verified(command, attempt, verification)


def _require_provider_identity(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,255}", value) is None
    ):
        raise ValueError(f"{field_name} must be a bounded provider identity")
    return value


class Bitrix24GrantAdmission:
    """Resolve secrets only after exact current reference authority is proven."""

    def __init__(
        self,
        *,
        repository: Bitrix24GrantAuthorityRepository,
        grant_resolver: ActingUserGrantResolver,
    ) -> None:
        self._repository = repository
        self._grant_resolver = grant_resolver

    async def resolve(
        self,
        connection: ProviderConnection,
        authorization: Bitrix24ReadAuthorization | None = None,
    ) -> Bitrix24PinnedGrant:
        authority = await self._repository.current_authority(connection)
        if authority is None or not _matches_connection(connection, authority):
            raise Bitrix24GrantDenied()
        if (
            authorization is not None
            and authorization.required_scope not in authority.binding.granted_scopes
        ):
            raise ObjectReadDenied()
        tokens = await self._grant_resolver.resolve(authority)
        return Bitrix24PinnedGrant(
            binding_id=authority.binding.binding_id,
            acting_user_id=authority.binding.acting_user_id,
            token_epoch=authority.token_epoch,
            security_fence=authority.security_fence,
            tokens=tokens,
        )


class Bitrix24ReadFenceRepository(Protocol):
    async def is_current(
        self,
        connection: ProviderConnection,
        pinned: Bitrix24PinnedGrant,
    ) -> bool: ...


class Bitrix24ProfileClient(Protocol):
    async def profile_user_id(
        self, connection: ProviderConnection, pinned: Bitrix24PinnedGrant
    ) -> str: ...


class Bitrix24PinnedObservationReader(Protocol):
    async def read_pinned(
        self,
        connection: ProviderConnection,
        command: ProviderReadCommand,
        pinned: Bitrix24PinnedGrant,
    ) -> NormalizedExternalObservationV2: ...


class Bitrix24ReadMethod(str, Enum):
    """Closed allowlist: this OAuth path cannot dispatch arbitrary REST methods."""

    DISK_READ = "disk.read"
    CRM_MATERIAL_READ = "crm.material.read"


@dataclass(frozen=True, slots=True)
class Bitrix24ReadAuthorization:
    method: Bitrix24ReadMethod
    required_scope: Bitrix24Scope

    def __post_init__(self) -> None:
        expected = {
            Bitrix24ReadMethod.DISK_READ: Bitrix24Scope.DISK,
            Bitrix24ReadMethod.CRM_MATERIAL_READ: Bitrix24Scope.CRM,
        }
        if expected[self.method] is not self.required_scope:
            raise ValueError("Bitrix24 read method and provider scope must match")


class Bitrix24ReadPolicy(Protocol):
    async def authorize(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> Bitrix24ReadAuthorization | None: ...


class ClosedBitrix24ReadPolicy:
    """Authorize only the two qualified read contracts and their exact scopes."""

    _DISK = (
        ProviderCapabilityKind.ENUMERATION,
        ProviderCapabilityKind.METADATA_READ,
        ProviderCapabilityKind.BINARY_READ,
        ProviderCapabilityKind.STABLE_IDENTITY,
    )
    _CRM = _DISK + (
        ProviderCapabilityKind.CRM,
        ProviderCapabilityKind.STRUCTURED_READ,
    )

    async def authorize(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> Bitrix24ReadAuthorization | None:
        requested = tuple(command.required_capabilities)
        if requested == self._DISK and all(
            connection.supports(capability) for capability in self._DISK
        ):
            return Bitrix24ReadAuthorization(
                method=Bitrix24ReadMethod.DISK_READ,
                required_scope=Bitrix24Scope.DISK,
            )
        return None


class Bitrix24ExpiredGrantRefresher(Protocol):
    async def refresh_expired(
        self, connection: ProviderConnection, pinned: Bitrix24PinnedGrant
    ) -> RefreshOutcome: ...


class Bitrix24GovernedObservationReader:
    """Pin profile and data to one pair and fence results before release."""

    def __init__(
        self,
        *,
        admission: Bitrix24GrantAdmission,
        repository: Bitrix24ReadFenceRepository,
        profile_client: Bitrix24ProfileClient,
        provider: Bitrix24PinnedObservationReader,
        policy: Bitrix24ReadPolicy,
        refresher: Bitrix24ExpiredGrantRefresher,
    ) -> None:
        self._admission = admission
        self._repository = repository
        self._profile_client = profile_client
        self._provider = provider
        self._policy = policy
        self._refresher = refresher

    async def read(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> NormalizedExternalObservationV2:
        try:
            return await self._read_once(connection, command)
        except Bitrix24AccessTokenExpired as expired:
            pinned = expired.pinned
            if pinned is None:
                raise Bitrix24IdentityUncertain() from None
            refreshed = await self._refresher.refresh_expired(connection, pinned)
            if not isinstance(refreshed, PairActivated):
                raise Bitrix24IdentityUncertain() from None
            try:
                return await self._read_once(connection, command)
            except Bitrix24AccessTokenExpired:
                raise Bitrix24IdentityUncertain() from None

    async def _read_once(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> NormalizedExternalObservationV2:
        authorization = await self._policy.authorize(connection, command)
        if authorization is None:
            raise ObjectReadDenied()
        pinned = await self._admission.resolve(connection, authorization)
        try:
            user_id = await self._profile_client.profile_user_id(connection, pinned)
        except Bitrix24AccessTokenExpired as error:
            error.pinned = pinned
            raise
        except Bitrix24ProfileUnavailable:
            raise Bitrix24IdentityUncertain() from None
        if user_id != pinned.acting_user_id:
            raise Bitrix24IdentityMismatch()
        if not await self._repository.is_current(connection, pinned):
            raise StaleReadFenced()
        try:
            observation = await self._provider.read_pinned(connection, command, pinned)
        except Bitrix24AccessTokenExpired as error:
            error.pinned = pinned
            raise
        if not await self._repository.is_current(connection, pinned):
            raise StaleReadFenced()
        return observation


def _matches_connection(
    connection: ProviderConnection, authority: Bitrix24GrantAuthority
) -> bool:
    binding = authority.binding
    return (
        connection.provider_kind is ProviderKind.BITRIX24
        and connection.status is ProviderConnectionStatus.ACTIVE
        and authority.state is Bitrix24GrantState.READY
        and authority.security_fence == 0
        and (
            binding.workspace_id,
            binding.environment_id,
            binding.approval_id,
            binding.connection_id,
            binding.connection_version_id,
            binding.grant_reference,
            binding.authorization_generation,
            binding.configuration_generation,
        )
        == (
            connection.workspace_id,
            connection.environment_id,
            connection.approval_id,
            connection.connection_id,
            connection.connection_version_id,
            connection.grant_reference,
            connection.authorization_generation,
            connection.configuration_generation,
        )
    )


class Bitrix24OAuthSafeCode(str, Enum):
    CONNECTION_ACTIVATED = "connection_activated"
    ACTIVATION_DENIED = "activation_denied"
    CALLBACK_ACCEPTED = "callback_accepted"
    CALLBACK_DUPLICATE = "callback_duplicate"
    CALLBACK_CONFLICTING = "callback_conflicting"
    CALLBACK_UNSUPPORTED_DROPPED = "callback_unsupported_dropped"
    CALLBACK_AMBIGUOUS = "callback_ambiguous"
    CALLBACK_LATE = "callback_late"
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_UNCERTAIN = "identity_uncertain"
    REFRESH_BUSY = "refresh_busy"
    PAIR_ACTIVATED = "pair_activated"
    REFRESH_UNCERTAIN = "refresh_uncertain"
    GRANT_UNCERTAIN = "grant_uncertain"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    GRANT_CONTAINED = "grant_contained"
    CONTAINMENT_DENIED = "containment_denied"
    RECOVERY_DENIED = "recovery_denied"
    GRANT_RECOVERED = "grant_recovered"
    STALE_FENCED = "stale_fenced"
    OBJECT_READ_DENIED = "object_read_denied"
    UNINSTALL_DENIED = "uninstall_denied"


def safe_oauth_outcome_code(outcome: object) -> Bitrix24OAuthSafeCode:
    """Map public outcomes to a closed diagnostic vocabulary without provider text."""

    if outcome is Bitrix24ConfirmedUninstall.DENIED:
        return Bitrix24OAuthSafeCode.UNINSTALL_DENIED
    if outcome is Bitrix24ConfirmedUninstall.IDENTITY_MISMATCH:
        return Bitrix24OAuthSafeCode.IDENTITY_MISMATCH

    mappings: tuple[tuple[type[object], Bitrix24OAuthSafeCode], ...] = (
        (ConnectionActivated, Bitrix24OAuthSafeCode.CONNECTION_ACTIVATED),
        (ActivationDenied, Bitrix24OAuthSafeCode.ACTIVATION_DENIED),
        (CallbackAccepted, Bitrix24OAuthSafeCode.CALLBACK_ACCEPTED),
        (CallbackDuplicate, Bitrix24OAuthSafeCode.CALLBACK_DUPLICATE),
        (CallbackConflicting, Bitrix24OAuthSafeCode.CALLBACK_CONFLICTING),
        (
            CallbackUnsupportedDropped,
            Bitrix24OAuthSafeCode.CALLBACK_UNSUPPORTED_DROPPED,
        ),
        (CallbackAmbiguous, Bitrix24OAuthSafeCode.CALLBACK_AMBIGUOUS),
        (CallbackLate, Bitrix24OAuthSafeCode.CALLBACK_LATE),
        (CallbackIdentityMismatch, Bitrix24OAuthSafeCode.IDENTITY_MISMATCH),
        (CallbackIdentityUncertain, Bitrix24OAuthSafeCode.IDENTITY_UNCERTAIN),
        (RefreshBusy, Bitrix24OAuthSafeCode.REFRESH_BUSY),
        (PairActivated, Bitrix24OAuthSafeCode.PAIR_ACTIVATED),
        (RefreshUncertain, Bitrix24OAuthSafeCode.REFRESH_UNCERTAIN),
        (GrantUncertain, Bitrix24OAuthSafeCode.GRANT_UNCERTAIN),
        (ReauthorizationRequired, Bitrix24OAuthSafeCode.REAUTHORIZATION_REQUIRED),
        (GrantContained, Bitrix24OAuthSafeCode.GRANT_CONTAINED),
        (ContainmentDenied, Bitrix24OAuthSafeCode.CONTAINMENT_DENIED),
        (RecoveryDenied, Bitrix24OAuthSafeCode.RECOVERY_DENIED),
        (GrantRecovered, Bitrix24OAuthSafeCode.GRANT_RECOVERED),
        (StaleRefreshFenced, Bitrix24OAuthSafeCode.STALE_FENCED),
        (StaleReadFenced, Bitrix24OAuthSafeCode.STALE_FENCED),
        (ObjectReadDenied, Bitrix24OAuthSafeCode.OBJECT_READ_DENIED),
    )
    for outcome_type, code in mappings:
        if isinstance(outcome, outcome_type):
            return code
    raise ValueError("Bitrix24 OAuth outcome is not allowlisted.")


__all__ = [
    "ActivationDenied",
    "ActivationOutcome",
    "ActivationCommitUnknown",
    "ActingUserGrantResolver",
    "Bitrix24AccessTokenExpired",
    "Bitrix24BootstrapAttempt",
    "Bitrix24BootstrapCoordinator",
    "Bitrix24BootstrapRepository",
    "Bitrix24CallbackCommand",
    "Bitrix24CallbackDispatcher",
    "Bitrix24ActivationAdmission",
    "Bitrix24ActivationAuthority",
    "Bitrix24ActivationRepository",
    "Bitrix24ActivationRequest",
    "Bitrix24ConnectionActivation",
    "Bitrix24ContainmentCommand",
    "Bitrix24RefreshCommand",
    "Bitrix24RefreshCoordinator",
    "Bitrix24RefreshExecutor",
    "Bitrix24RefreshRepository",
    "Bitrix24RefreshTransport",
    "BootstrapAttemptCreated",
    "BootstrapMarkedUncertain",
    "BootstrapAttemptState",
    "BootstrapRetryBlocked",
    "Bitrix24GrantAdmission",
    "Bitrix24GrantLoss",
    "Bitrix24GrantLossRepository",
    "Bitrix24GrantAuthorityRepository",
    "Bitrix24GrantDenied",
    "Bitrix24GrantContainment",
    "Bitrix24GrantRecovery",
    "Bitrix24GovernedObservationReader",
    "Bitrix24ConfirmedUninstall",
    "Bitrix24ExpiredGrantRefresh",
    "Bitrix24ExpiredGrantRefresher",
    "Bitrix24IdentityMismatch",
    "Bitrix24IdentityUncertain",
    "Bitrix24OAuthError",
    "Bitrix24OAuthSafeCode",
    "Bitrix24PinnedGrant",
    "Bitrix24PinnedObservationReader",
    "Bitrix24ProfileClient",
    "Bitrix24ProfileUnavailable",
    "Bitrix24ReadAuthorization",
    "Bitrix24ReadMethod",
    "Bitrix24ReadPolicy",
    "Bitrix24ReadFenceRepository",
    "Bitrix24RecoveryCommand",
    "Bitrix24Scope",
    "Bitrix24UninstallAuthority",
    "Bitrix24UninstallEvidence",
    "CallbackAccepted",
    "CallbackAmbiguous",
    "CallbackConflicting",
    "CallbackDuplicate",
    "CallbackIdentityMismatch",
    "CallbackIdentityUncertain",
    "CallbackLate",
    "CallbackOutcome",
    "CallbackReceiptOutcome",
    "CallbackUnsupportedDropped",
    "CallbackVerification",
    "CallbackVerificationStatus",
    "ContainmentDenied",
    "ConnectionActivated",
    "ClosedBitrix24ReadPolicy",
    "GrantUncertain",
    "GrantContained",
    "GrantContainmentRepository",
    "GrantContainmentAuthority",
    "GrantRecovered",
    "GrantRecoveryAuthority",
    "GrantRecoveryRepository",
    "ApplicationCredentialResolver",
    "InstallationCallbackRepository",
    "InstallationCallbackVerifier",
    "PairActivated",
    "ObjectReadDenied",
    "RefreshedTokenPair",
    "RecoveryDenied",
    "ReauthorizationRequired",
    "RefreshBusy",
    "RefreshClaim",
    "RefreshInvalidGrant",
    "RefreshOutcome",
    "RefreshTransportOutcome",
    "RefreshTransportUncertain",
    "RefreshUncertain",
    "SecretApplicationCredential",
    "SecretTokenPair",
    "StaleRefreshFenced",
    "StaleReadFenced",
    "TokenPairStore",
    "safe_oauth_outcome_code",
]
