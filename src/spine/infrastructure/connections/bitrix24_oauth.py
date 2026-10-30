"""Deterministic offline Bitrix24 OAuth authority used by tests and composition."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.connections.bitrix24_oauth import (
    ActivationDenied,
    Bitrix24ActivationAdmission,
    Bitrix24ActivationRequest,
    Bitrix24BootstrapAttempt,
    Bitrix24CallbackCommand,
    Bitrix24ContainmentCommand,
    Bitrix24ConfirmedUninstall,
    Bitrix24PinnedGrant,
    Bitrix24RecoveryCommand,
    Bitrix24RefreshCommand,
    Bitrix24UninstallEvidence,
    BootstrapAttemptCreated,
    BootstrapMarkedUncertain,
    BootstrapAttemptState,
    BootstrapRetryBlocked,
    CallbackAccepted,
    CallbackConflicting,
    CallbackDuplicate,
    CallbackVerification,
    ConnectionActivated,
    GrantContained,
    GrantRecovered,
    GrantUncertain,
    PairActivated,
    ReauthorizationRequired,
    RecoveryDenied,
    RefreshBusy,
    RefreshClaim,
    RefreshUncertain,
    SecretTokenPair,
    StaleRefreshFenced,
)
from spine.domain.connections.bitrix24_oauth import (
    Bitrix24GrantAuthority,
    Bitrix24GrantBinding,
    Bitrix24GrantState,
    Bitrix24Scope,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
)


class InMemoryBitrix24OAuthRepository:
    """Bounded callback receipts with exact tuple selection and stable replay."""

    def __init__(
        self,
        *,
        attempts: tuple[Bitrix24BootstrapAttempt, ...] = (),
        authorities: tuple[Bitrix24GrantAuthority, ...] = (),
    ) -> None:
        self._lock = asyncio.Lock()
        self._attempts = list(attempts)
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ValueError("bootstrap attempt identities must be unique")
        self._receipts: dict[object, tuple[str, CallbackAccepted]] = {}
        self._receipt_fingerprints: dict[tuple[UUID, UUID, str], CallbackAccepted] = {}
        self._activation_evidence: dict[
            UUID,
            tuple[
                Bitrix24BootstrapAttempt,
                OpaqueProviderReference,
                UUID,
            ],
        ] = {}
        self._quarantined: set[object] = set()
        self._authorities = {item.binding.binding_id: item for item in authorities}
        if len(self._authorities) != len(authorities):
            raise ValueError("grant binding identities must be unique")
        self._claims: dict[UUID, UUID] = {}
        self._uncertain_attempts: dict[UUID, UUID] = {}
        self._containment_receipts: dict[UUID, GrantContained] = {}
        self._recovery_receipts: dict[UUID, GrantRecovered] = {}

    async def create_attempt(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapAttemptCreated | BootstrapRetryBlocked:
        key = _bootstrap_identity(attempt)
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self._attempts
                    if _bootstrap_identity(item) == key
                    and item.state
                    in {BootstrapAttemptState.PENDING, BootstrapAttemptState.UNCERTAIN}
                ),
                None,
            )
            if existing is not None:
                return BootstrapRetryBlocked(existing_attempt_id=existing.attempt_id)
            self._attempts.append(attempt)
            return BootstrapAttemptCreated(attempt_id=attempt.attempt_id)

    async def existing_receipt(
        self, command: Bitrix24CallbackCommand
    ) -> CallbackDuplicate | CallbackConflicting | None:
        key = command.callback_reference.value
        async with self._lock:
            replay = self._receipt_fingerprints.get(
                (
                    command.workspace_id,
                    command.environment_id,
                    command.payload_fingerprint,
                )
            )
            if replay is not None:
                return CallbackDuplicate(
                    receipt_id=replay.receipt_id,
                    binding_id=replay.binding_id,
                )
            existing = self._receipts.get(key)
            if existing is None:
                return None
            fingerprint, accepted = existing
            if fingerprint == command.payload_fingerprint:
                return CallbackDuplicate(
                    receipt_id=accepted.receipt_id,
                    binding_id=accepted.binding_id,
                )
            self._quarantined.add(key)
            return CallbackConflicting(callback_reference=command.callback_reference)

    async def find_pending(
        self, command: Bitrix24CallbackCommand
    ) -> tuple[Bitrix24BootstrapAttempt, ...]:
        async with self._lock:
            return tuple(
                attempt
                for attempt in self._attempts
                if attempt.workspace_id == command.workspace_id
                and attempt.environment_id == command.environment_id
                and attempt.application_id == command.application_id
                and attempt.portal_member_id == command.portal_member_id_hint
                and attempt.state is BootstrapAttemptState.PENDING
            )

    async def mark_attempt_uncertain(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapMarkedUncertain:
        async with self._lock:
            self._attempts = [
                (
                    replace(item, state=BootstrapAttemptState.UNCERTAIN)
                    if item.attempt_id == attempt.attempt_id
                    and item.state is BootstrapAttemptState.PENDING
                    else item
                )
                for item in self._attempts
            ]
            return BootstrapMarkedUncertain(attempt_id=attempt.attempt_id)

    async def record_verified(
        self,
        command: Bitrix24CallbackCommand,
        attempt: Bitrix24BootstrapAttempt,
        verification: CallbackVerification,
    ) -> CallbackAccepted | CallbackDuplicate | CallbackConflicting:
        key = command.callback_reference.value
        async with self._lock:
            replay = self._receipt_fingerprints.get(
                (
                    command.workspace_id,
                    command.environment_id,
                    command.payload_fingerprint,
                )
            )
            if replay is not None:
                return CallbackDuplicate(
                    receipt_id=replay.receipt_id,
                    binding_id=replay.binding_id,
                )
            existing = self._receipts.get(key)
            if existing is not None:
                fingerprint, accepted = existing
                if fingerprint == command.payload_fingerprint:
                    return CallbackDuplicate(
                        receipt_id=accepted.receipt_id,
                        binding_id=accepted.binding_id,
                    )
                self._quarantined.add(key)
                return CallbackConflicting(
                    callback_reference=command.callback_reference
                )
            accepted = CallbackAccepted(
                receipt_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:bitrix24-callback-receipt:v1:{attempt.attempt_id}:{key}",
                ),
                binding_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:bitrix24-grant-binding:v1:{attempt.attempt_id}:{key}",
                ),
            )
            if verification.pair_candidate_reference is None:
                raise ValueError("verified callback requires a pair candidate")
            self._activation_evidence[accepted.receipt_id] = (
                attempt,
                verification.pair_candidate_reference,
                accepted.binding_id,
            )
            self._receipts[key] = (command.payload_fingerprint, accepted)
            self._receipt_fingerprints[
                (
                    command.workspace_id,
                    command.environment_id,
                    command.payload_fingerprint,
                )
            ] = accepted
            self._attempts = [
                (
                    replace(item, state=BootstrapAttemptState.ACCEPTED)
                    if item.attempt_id == attempt.attempt_id
                    else item
                )
                for item in self._attempts
            ]
            return accepted

    async def admit_activation(
        self, request: Bitrix24ActivationRequest
    ) -> Bitrix24ActivationAdmission | ActivationDenied:
        async with self._lock:
            evidence = self._activation_evidence.get(request.receipt_id)
            if evidence is None:
                return ActivationDenied(receipt_id=request.receipt_id)
            attempt, candidate, binding_id = evidence
            if not _attempt_matches_connection(attempt, request.connection):
                return ActivationDenied(receipt_id=request.receipt_id)
            return Bitrix24ActivationAdmission(
                binding_id=binding_id,
                pair_candidate_reference=candidate,
            )

    async def activate(
        self,
        request: Bitrix24ActivationRequest,
        admission: Bitrix24ActivationAdmission,
    ) -> ConnectionActivated | ActivationDenied:
        async with self._lock:
            evidence = self._activation_evidence.get(request.receipt_id)
            if evidence is None:
                existing = self._authorities.get(admission.binding_id)
                if existing is not None and _matches_activation(
                    existing, request, admission
                ):
                    return ConnectionActivated(
                        connection_version_id=request.connection.connection_version_id,
                        binding_id=existing.binding.binding_id,
                        token_epoch=existing.token_epoch,
                    )
                return ActivationDenied(receipt_id=request.receipt_id)
            attempt, candidate, binding_id = evidence
            if (
                binding_id != admission.binding_id
                or candidate != admission.pair_candidate_reference
                or not _attempt_matches_connection(attempt, request.connection)
            ):
                return ActivationDenied(receipt_id=request.receipt_id)
            binding = Bitrix24GrantBinding(
                binding_id=binding_id,
                workspace_id=attempt.workspace_id,
                environment_id=attempt.environment_id,
                approval_id=attempt.approval_id,
                connection_id=attempt.connection_id,
                connection_version_id=attempt.connection_version_id,
                grant_reference=attempt.grant_reference,
                portal_member_id=attempt.portal_member_id,
                application_id=attempt.application_id,
                acting_user_id=attempt.installing_user_id,
                granted_scopes=tuple(
                    Bitrix24Scope(scope) for scope in attempt.approved_scopes
                ),
                authorization_generation=attempt.authorization_generation,
                configuration_generation=attempt.configuration_generation,
            )
            authority = Bitrix24GrantAuthority(
                binding=binding,
                active_pair_reference=candidate,
                token_epoch=1,
                security_fence=0,
                state=Bitrix24GrantState.READY,
            )
            self._authorities[binding_id] = authority
            del self._activation_evidence[request.receipt_id]
            return ConnectionActivated(
                connection_version_id=request.connection.connection_version_id,
                binding_id=binding_id,
                token_epoch=1,
            )

    async def current_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None:
        async with self._lock:
            return self._exact_authority(connection)

    async def confirm_uninstall(
        self, evidence: Bitrix24UninstallEvidence
    ) -> ReauthorizationRequired | Bitrix24ConfirmedUninstall:
        async with self._lock:
            authority = self._exact_authority(evidence.connection)
            if authority is None or (
                authority.binding.binding_id,
                authority.binding.portal_member_id,
                authority.binding.application_id,
            ) != (
                evidence.binding_id,
                evidence.portal_member_id,
                evidence.application_id,
            ):
                return Bitrix24ConfirmedUninstall.IDENTITY_MISMATCH
            self._authorities[evidence.binding_id] = authority.model_copy(
                update={"state": Bitrix24GrantState.REAUTHORIZATION_REQUIRED}
            )
            self._claims.pop(evidence.binding_id, None)
            return ReauthorizationRequired(binding_id=evidence.binding_id)

    async def is_current(
        self, connection: ProviderConnection, pinned: Bitrix24PinnedGrant
    ) -> bool:
        async with self._lock:
            authority = self._exact_authority(connection)
            return (
                authority is not None
                and authority.state is Bitrix24GrantState.READY
                and authority.security_fence == pinned.security_fence == 0
                and authority.binding.binding_id == pinned.binding_id
                and authority.token_epoch == pinned.token_epoch
            )

    async def contain(
        self, command: Bitrix24ContainmentCommand
    ) -> GrantContained | StaleReadFenced:
        async with self._lock:
            replay = self._containment_receipts.get(command.command_id)
            if replay is not None:
                return replay
            authority = self._exact_authority(command.connection)
            if (
                authority is None
                or authority.binding.binding_id != command.binding_id
                or authority.security_fence != command.expected_security_fence
            ):
                return StaleReadFenced()
            contained = authority.model_copy(
                update={
                    "security_fence": authority.security_fence + 1,
                    "state": Bitrix24GrantState.CONTAINED,
                }
            )
            self._authorities[command.binding_id] = contained
            self._claims.pop(command.binding_id, None)
            outcome = GrantContained(
                binding_id=command.binding_id,
                security_fence=contained.security_fence,
                incident_reference=command.incident_reference,
            )
            self._containment_receipts[command.command_id] = outcome
            return outcome

    async def recover(
        self, command: Bitrix24RecoveryCommand
    ) -> GrantRecovered | RecoveryDenied:
        async with self._lock:
            replay = self._recovery_receipts.get(command.command_id)
            if replay is not None:
                return replay
            current = self._exact_authority(command.contained_connection)
            if (
                current is None
                or current.state is not Bitrix24GrantState.CONTAINED
                or command.replacement_authority.active_pair_reference
                == current.active_pair_reference
            ):
                return RecoveryDenied(
                    binding_id=(
                        current.binding.binding_id
                        if current is not None
                        else command.replacement_authority.binding.binding_id
                    )
                )
            replacement = command.replacement_authority
            if replacement.binding.binding_id in self._authorities:
                return RecoveryDenied(binding_id=current.binding.binding_id)
            self._authorities[replacement.binding.binding_id] = replacement
            outcome = GrantRecovered(
                previous_binding_id=current.binding.binding_id,
                binding_id=replacement.binding.binding_id,
                token_epoch=replacement.token_epoch,
            )
            self._recovery_receipts[command.command_id] = outcome
            return outcome

    async def claim_refresh(
        self, command: Bitrix24RefreshCommand
    ) -> RefreshClaim | RefreshBusy | RefreshUncertain | StaleRefreshFenced:
        async with self._lock:
            authority = self._exact_authority(command.connection)
            if (
                authority is None
                or authority.token_epoch != command.expected_token_epoch
            ):
                return StaleRefreshFenced(
                    binding_id=(authority.binding.binding_id if authority else None)
                )
            binding_id = authority.binding.binding_id
            if authority.state is Bitrix24GrantState.UNCERTAIN:
                return RefreshUncertain(
                    binding_id=binding_id,
                    attempt_id=self._uncertain_attempts[binding_id],
                )
            if authority.state is Bitrix24GrantState.REFRESH_CLAIMED:
                if self._claims.get(binding_id) == command.attempt_id:
                    self._authorities[binding_id] = authority.model_copy(
                        update={"state": Bitrix24GrantState.UNCERTAIN}
                    )
                    self._uncertain_attempts[binding_id] = command.attempt_id
                    del self._claims[binding_id]
                    return RefreshUncertain(
                        binding_id=binding_id,
                        attempt_id=command.attempt_id,
                    )
                return RefreshBusy(
                    binding_id=binding_id,
                    token_epoch=authority.token_epoch,
                )
            if (
                authority.state is not Bitrix24GrantState.READY
                or authority.security_fence
            ):
                return StaleRefreshFenced(binding_id=binding_id)
            self._claims[binding_id] = command.attempt_id
            self._authorities[binding_id] = authority.model_copy(
                update={"state": Bitrix24GrantState.REFRESH_CLAIMED}
            )
            return RefreshClaim(attempt_id=command.attempt_id, authority=authority)

    async def activate_candidate(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | StaleRefreshFenced:
        binding_id = claim.authority.binding.binding_id
        async with self._lock:
            current = self._authorities.get(binding_id)
            if (
                current is None
                or self._claims.get(binding_id) != claim.attempt_id
                or current.state is not Bitrix24GrantState.REFRESH_CLAIMED
                or current.token_epoch != claim.authority.token_epoch
                or current.security_fence != claim.authority.security_fence
            ):
                return StaleRefreshFenced(binding_id=binding_id)
            activated = current.model_copy(
                update={
                    "active_pair_reference": candidate,
                    "token_epoch": current.token_epoch + 1,
                    "state": Bitrix24GrantState.READY,
                }
            )
            self._authorities[binding_id] = activated
            del self._claims[binding_id]
            return PairActivated(
                binding_id=binding_id,
                pair_reference=candidate,
                token_epoch=activated.token_epoch,
            )

    async def resolve_activation(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | RefreshUncertain | StaleRefreshFenced:
        binding_id = claim.authority.binding.binding_id
        async with self._lock:
            current = self._authorities.get(binding_id)
            if (
                current is not None
                and current.state is Bitrix24GrantState.READY
                and current.active_pair_reference == candidate
                and current.token_epoch == claim.authority.token_epoch + 1
            ):
                return PairActivated(
                    binding_id=binding_id,
                    pair_reference=candidate,
                    token_epoch=current.token_epoch,
                )
            can_mark_uncertain = (
                current is not None
                and current.state is Bitrix24GrantState.REFRESH_CLAIMED
                and self._claims.get(binding_id) == claim.attempt_id
            )
        if can_mark_uncertain:
            return await self.mark_refresh_uncertain(claim)
        return StaleRefreshFenced(binding_id=binding_id)

    async def mark_refresh_uncertain(
        self, claim: RefreshClaim
    ) -> RefreshUncertain | StaleRefreshFenced:
        binding_id = claim.authority.binding.binding_id
        async with self._lock:
            current = self._authorities.get(binding_id)
            if (
                current is None
                or self._claims.get(binding_id) != claim.attempt_id
                or current.state is not Bitrix24GrantState.REFRESH_CLAIMED
                or current.token_epoch != claim.authority.token_epoch
                or current.security_fence != claim.authority.security_fence
            ):
                return StaleRefreshFenced(binding_id=binding_id)
            self._authorities[binding_id] = current.model_copy(
                update={"state": Bitrix24GrantState.UNCERTAIN}
            )
            self._uncertain_attempts[binding_id] = claim.attempt_id
            del self._claims[binding_id]
            return RefreshUncertain(
                binding_id=binding_id,
                attempt_id=claim.attempt_id,
            )

    async def mark_grant_uncertain(
        self, claim: RefreshClaim
    ) -> GrantUncertain | StaleRefreshFenced:
        transitioned = await self._transition_claim(claim, Bitrix24GrantState.UNCERTAIN)
        if not transitioned:
            return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)
        self._uncertain_attempts[claim.authority.binding.binding_id] = claim.attempt_id
        return GrantUncertain(
            binding_id=claim.authority.binding.binding_id,
            attempt_id=claim.attempt_id,
        )

    async def require_reauthorization(
        self, claim: RefreshClaim
    ) -> ReauthorizationRequired | StaleRefreshFenced:
        transitioned = await self._transition_claim(
            claim, Bitrix24GrantState.REAUTHORIZATION_REQUIRED
        )
        if not transitioned:
            return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)
        return ReauthorizationRequired(binding_id=claim.authority.binding.binding_id)

    async def _transition_claim(
        self, claim: RefreshClaim, state: Bitrix24GrantState
    ) -> bool:
        binding_id = claim.authority.binding.binding_id
        async with self._lock:
            current = self._authorities.get(binding_id)
            if (
                current is None
                or self._claims.get(binding_id) != claim.attempt_id
                or current.state is not Bitrix24GrantState.REFRESH_CLAIMED
                or current.token_epoch != claim.authority.token_epoch
                or current.security_fence != claim.authority.security_fence
            ):
                return False
            self._authorities[binding_id] = current.model_copy(update={"state": state})
            del self._claims[binding_id]
            return True

    def _exact_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None:
        matches = [
            item
            for item in self._authorities.values()
            if (
                item.binding.workspace_id,
                item.binding.environment_id,
                item.binding.approval_id,
                item.binding.connection_id,
                item.binding.connection_version_id,
                item.binding.grant_reference,
                item.binding.authorization_generation,
                item.binding.configuration_generation,
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
        ]
        return matches[0] if len(matches) == 1 else None


class InMemoryTokenPairStore:
    """Immutable pair candidates selected only by canonical authority references."""

    def __init__(
        self,
        *,
        initial_pairs: dict[OpaqueProviderReference, SecretTokenPair] | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._pairs = dict(initial_pairs or {})
        self._sequence = len(self._pairs)

    async def resolve(self, authority: Bitrix24GrantAuthority) -> SecretTokenPair:
        async with self._lock:
            pair = self._pairs.get(authority.active_pair_reference)
            if pair is None:
                raise KeyError("active token pair reference is unavailable")
            return pair

    async def create_candidate(
        self, claim: RefreshClaim, tokens: SecretTokenPair
    ) -> OpaqueProviderReference:
        async with self._lock:
            self._sequence += 1
            reference = OpaqueProviderReference(
                value=uuid5(
                    NAMESPACE_URL,
                    "spine:bitrix24-token-pair-candidate:v1:"
                    f"{claim.authority.binding.binding_id}:{claim.attempt_id}:{self._sequence}",
                )
            )
            self._pairs[reference] = tokens
            return reference

    async def verify_candidate(self, reference: OpaqueProviderReference) -> bool:
        async with self._lock:
            return reference in self._pairs

    async def discard_candidate(
        self,
        reference: OpaqueProviderReference,
        *,
        active_references: frozenset[OpaqueProviderReference],
        incident_retained: bool,
    ) -> bool:
        async with self._lock:
            if reference in active_references or incident_retained:
                return False
            return self._pairs.pop(reference, None) is not None


def _bootstrap_identity(attempt: Bitrix24BootstrapAttempt) -> tuple[object, ...]:
    return (
        attempt.workspace_id,
        attempt.environment_id,
        attempt.application_id,
        attempt.portal_member_id,
        attempt.installing_user_id,
    )


def _attempt_matches_connection(
    attempt: Bitrix24BootstrapAttempt, connection: ProviderConnection
) -> bool:
    return (
        attempt.workspace_id,
        attempt.environment_id,
        attempt.approval_id,
        attempt.connection_id,
        attempt.connection_version_id,
        attempt.grant_reference,
        attempt.authorization_generation,
        attempt.configuration_generation,
    ) == (
        connection.workspace_id,
        connection.environment_id,
        connection.approval_id,
        connection.connection_id,
        connection.connection_version_id,
        connection.grant_reference,
        connection.authorization_generation,
        connection.configuration_generation,
    )


def _matches_activation(
    authority: Bitrix24GrantAuthority,
    request: Bitrix24ActivationRequest,
    admission: Bitrix24ActivationAdmission,
) -> bool:
    binding = authority.binding
    connection = request.connection
    return (
        binding.binding_id == admission.binding_id
        and authority.active_pair_reference == admission.pair_candidate_reference
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


__all__ = ["InMemoryBitrix24OAuthRepository", "InMemoryTokenPairStore"]
