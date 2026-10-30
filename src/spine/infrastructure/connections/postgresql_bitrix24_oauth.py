"""PostgreSQL mappings and CAS authority for offline Bitrix24 OAuth state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

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
    StaleReadFenced,
    StaleRefreshFenced,
)
from spine.application.diagnostics import (
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
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
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.connections.postgresql_provider_management import (
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.persistence.postgresql_mappings import audit_events

_metadata = MetaData(schema="spine")


bitrix24_grant_bindings = Table(
    "bitrix24_grant_bindings",
    _metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("approval_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("portal_member_id", String(255), nullable=False),
    Column("application_id", String(255), nullable=False),
    Column("acting_user_id", String(255), nullable=False),
    Column("granted_scopes", ARRAY(String(64)), nullable=False),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
    ForeignKeyConstraint(
        [
            "connection_version_id",
            "workspace_id",
            "environment_id",
            "approval_id",
            "connection_id",
            "grant_reference",
            "authorization_generation",
            "configuration_generation",
        ],
        [
            "spine.provider_connection_versions.connection_version_id",
            "spine.provider_connection_versions.workspace_id",
            "spine.provider_connection_versions.environment_id",
            "spine.provider_connection_versions.approval_id",
            "spine.provider_connection_versions.connection_id",
            "spine.provider_connection_versions.grant_reference",
            "spine.provider_connection_versions.authorization_generation",
            "spine.provider_connection_versions.configuration_generation",
        ],
    ),
)


bitrix24_grant_authorities = Table(
    "bitrix24_grant_authorities",
    _metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("active_pair_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("token_epoch", BigInteger, nullable=False),
    Column("security_fence", BigInteger, nullable=False),
    Column("state", String(32), nullable=False),
    Column("refresh_attempt_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("incident_reference", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["binding_id", "workspace_id", "environment_id"],
        [
            "spine.bitrix24_grant_bindings.binding_id",
            "spine.bitrix24_grant_bindings.workspace_id",
            "spine.bitrix24_grant_bindings.environment_id",
        ],
    ),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


bitrix24_oauth_attempts = Table(
    "bitrix24_oauth_attempts",
    _metadata,
    Column("attempt_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("approval_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("connection_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("connection_version_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("grant_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("portal_member_id", String(255), nullable=False),
    Column("application_id", String(255), nullable=False),
    Column("installing_user_id", String(255), nullable=False),
    Column("approved_scopes", ARRAY(String(64)), nullable=False),
    Column("authorization_generation", BigInteger, nullable=False),
    Column("configuration_generation", BigInteger, nullable=False),
    Column("status", String(32), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


bitrix24_oauth_callback_receipts = Table(
    "bitrix24_oauth_callback_receipts",
    _metadata,
    Column("callback_reference", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("attempt_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("payload_fingerprint", String(64), nullable=False),
    Column("receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("binding_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("pair_candidate_reference", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["attempt_id"], ["spine.bitrix24_oauth_attempts.attempt_id"]),
    ForeignKeyConstraint(
        ["workspace_id", "environment_id"],
        ["spine.environments.workspace_id", "spine.environments.id"],
    ),
)


_BIND_SCOPE = text(
    "SELECT set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true)"
)


class PostgreSQLBitrix24OAuthRepository:
    """Scope-bound PostgreSQL pointer/epoch CAS authority.

    The trusted context is reverified before every Unit of Work. Secret values are
    never accepted by this adapter; it persists only immutable references.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
        context: TrustedPersistenceContext,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier
        self._context = context
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def current_authority(
        self, connection: ProviderConnection
    ) -> Bitrix24GrantAuthority | None:
        scope = self._scope(
            connection,
            {
                "bitrix24_oauth_read",
                "bitrix24_oauth_refresh",
                "bitrix24_oauth_contain",
                "bitrix24_oauth_recover",
                "bitrix24_oauth_uninstall",
            },
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                return await _find_authority(session, connection)

    async def create_attempt(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapAttemptCreated | BootstrapRetryBlocked:
        scope = self._attempt_scope(attempt, {"bitrix24_oauth_bootstrap"})
        identity = (
            f"{attempt.workspace_id}:{attempt.environment_id}:"
            f"{attempt.application_id}:{attempt.portal_member_id}:"
            f"{attempt.installing_user_id}"
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"
                    ),
                    {"identity": identity},
                )
                existing = (
                    await session.execute(
                        select(bitrix24_oauth_attempts.c.attempt_id).where(
                            bitrix24_oauth_attempts.c.workspace_id
                            == attempt.workspace_id,
                            bitrix24_oauth_attempts.c.environment_id
                            == attempt.environment_id,
                            bitrix24_oauth_attempts.c.application_id
                            == attempt.application_id,
                            bitrix24_oauth_attempts.c.portal_member_id
                            == attempt.portal_member_id,
                            bitrix24_oauth_attempts.c.installing_user_id
                            == attempt.installing_user_id,
                            bitrix24_oauth_attempts.c.status.in_(
                                (
                                    BootstrapAttemptState.PENDING.value,
                                    BootstrapAttemptState.UNCERTAIN.value,
                                )
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return BootstrapRetryBlocked(existing_attempt_id=existing)
                await session.execute(
                    insert(bitrix24_oauth_attempts).values(
                        attempt_id=attempt.attempt_id,
                        workspace_id=attempt.workspace_id,
                        environment_id=attempt.environment_id,
                        approval_id=attempt.approval_id,
                        connection_id=attempt.connection_id,
                        connection_version_id=attempt.connection_version_id,
                        grant_reference=attempt.grant_reference.value,
                        portal_member_id=attempt.portal_member_id,
                        application_id=attempt.application_id,
                        installing_user_id=attempt.installing_user_id,
                        approved_scopes=list(attempt.approved_scopes),
                        authorization_generation=attempt.authorization_generation,
                        configuration_generation=attempt.configuration_generation,
                        status=attempt.state.value,
                        expires_at=attempt.expires_at,
                        recorded_at=func.now(),
                    )
                )
                return BootstrapAttemptCreated(attempt_id=attempt.attempt_id)

    async def existing_receipt(
        self, command: Bitrix24CallbackCommand
    ) -> CallbackDuplicate | CallbackConflicting | None:
        scope = self._callback_scope(command, {"bitrix24_oauth_callback"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                row = (
                    (
                        await session.execute(
                            select(bitrix24_oauth_callback_receipts).where(
                                (
                                    bitrix24_oauth_callback_receipts.c.callback_reference
                                    == command.callback_reference.value
                                )
                                | (
                                    (
                                        bitrix24_oauth_callback_receipts.c.workspace_id
                                        == command.workspace_id
                                    )
                                    & (
                                        bitrix24_oauth_callback_receipts.c.environment_id
                                        == command.environment_id
                                    )
                                    & (
                                        bitrix24_oauth_callback_receipts.c.payload_fingerprint
                                        == command.payload_fingerprint
                                    )
                                )
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                if row.payload_fingerprint == command.payload_fingerprint:
                    return CallbackDuplicate(
                        receipt_id=row.receipt_id, binding_id=row.binding_id
                    )
                await session.execute(
                    update(bitrix24_oauth_callback_receipts)
                    .where(
                        bitrix24_oauth_callback_receipts.c.callback_reference
                        == command.callback_reference.value
                    )
                    .values(outcome="quarantined")
                )
                await session.execute(
                    update(bitrix24_oauth_attempts)
                    .where(bitrix24_oauth_attempts.c.attempt_id == row.attempt_id)
                    .values(status=BootstrapAttemptState.QUARANTINED.value)
                )
                return CallbackConflicting(
                    callback_reference=command.callback_reference
                )

    async def find_pending(
        self, command: Bitrix24CallbackCommand
    ) -> tuple[Bitrix24BootstrapAttempt, ...]:
        scope = self._callback_scope(command, {"bitrix24_oauth_callback"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                rows = (
                    (
                        await session.execute(
                            select(bitrix24_oauth_attempts).where(
                                bitrix24_oauth_attempts.c.workspace_id
                                == command.workspace_id,
                                bitrix24_oauth_attempts.c.environment_id
                                == command.environment_id,
                                bitrix24_oauth_attempts.c.application_id
                                == command.application_id,
                                bitrix24_oauth_attempts.c.portal_member_id
                                == command.portal_member_id_hint,
                                bitrix24_oauth_attempts.c.status
                                == BootstrapAttemptState.PENDING.value,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                return tuple(_attempt_from_row(row) for row in rows)

    async def mark_attempt_uncertain(
        self, attempt: Bitrix24BootstrapAttempt
    ) -> BootstrapMarkedUncertain:
        scope = self._attempt_scope(attempt, {"bitrix24_oauth_callback"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                await session.execute(
                    update(bitrix24_oauth_attempts)
                    .where(
                        bitrix24_oauth_attempts.c.attempt_id == attempt.attempt_id,
                        bitrix24_oauth_attempts.c.status
                        == BootstrapAttemptState.PENDING.value,
                    )
                    .values(status=BootstrapAttemptState.UNCERTAIN.value)
                )
                return BootstrapMarkedUncertain(attempt_id=attempt.attempt_id)

    async def record_verified(
        self,
        command: Bitrix24CallbackCommand,
        attempt: Bitrix24BootstrapAttempt,
        verification: CallbackVerification,
    ) -> CallbackAccepted | CallbackDuplicate | CallbackConflicting:
        scope = self._callback_scope(command, {"bitrix24_oauth_callback"})
        receipt_id = uuid5(
            NAMESPACE_URL,
            "spine:bitrix24-callback-receipt:v1:"
            f"{attempt.attempt_id}:{command.callback_reference.value}",
        )
        binding_id = uuid5(
            NAMESPACE_URL,
            "spine:bitrix24-grant-binding:v1:"
            f"{attempt.attempt_id}:{command.callback_reference.value}",
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:callback, 0))"
                    ),
                    {"callback": str(command.callback_reference.value)},
                )
                row = (
                    (
                        await session.execute(
                            select(bitrix24_oauth_callback_receipts).where(
                                bitrix24_oauth_callback_receipts.c.callback_reference
                                == command.callback_reference.value
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is not None:
                    if row.payload_fingerprint == command.payload_fingerprint:
                        return CallbackDuplicate(
                            receipt_id=row.receipt_id, binding_id=row.binding_id
                        )
                    await session.execute(
                        update(bitrix24_oauth_callback_receipts)
                        .where(
                            bitrix24_oauth_callback_receipts.c.callback_reference
                            == command.callback_reference.value
                        )
                        .values(outcome="quarantined")
                    )
                    return CallbackConflicting(
                        callback_reference=command.callback_reference
                    )
                replay = (
                    (
                        await session.execute(
                            select(bitrix24_oauth_callback_receipts).where(
                                bitrix24_oauth_callback_receipts.c.workspace_id
                                == command.workspace_id,
                                bitrix24_oauth_callback_receipts.c.environment_id
                                == command.environment_id,
                                bitrix24_oauth_callback_receipts.c.payload_fingerprint
                                == command.payload_fingerprint,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if replay is not None:
                    return CallbackDuplicate(
                        receipt_id=replay.receipt_id,
                        binding_id=replay.binding_id,
                    )
                if verification.pair_candidate_reference is None:
                    raise ValueError("verified callback requires a pair candidate")
                await session.execute(
                    insert(bitrix24_oauth_callback_receipts).values(
                        callback_reference=command.callback_reference.value,
                        workspace_id=attempt.workspace_id,
                        environment_id=attempt.environment_id,
                        attempt_id=attempt.attempt_id,
                        payload_fingerprint=command.payload_fingerprint,
                        receipt_id=receipt_id,
                        binding_id=binding_id,
                        pair_candidate_reference=(
                            verification.pair_candidate_reference.value
                        ),
                        outcome="accepted",
                        recorded_at=command.received_at,
                    )
                )
                await session.execute(
                    update(bitrix24_oauth_attempts)
                    .where(
                        bitrix24_oauth_attempts.c.attempt_id == attempt.attempt_id,
                        bitrix24_oauth_attempts.c.status
                        == BootstrapAttemptState.PENDING.value,
                    )
                    .values(status=BootstrapAttemptState.ACCEPTED.value)
                )
                return CallbackAccepted(receipt_id=receipt_id, binding_id=binding_id)

    async def admit_activation(
        self, request: Bitrix24ActivationRequest
    ) -> Bitrix24ActivationAdmission | ActivationDenied:
        scope = self._scope(request.connection, {"bitrix24_oauth_activate"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                evidence = await _activation_evidence(session, request)
                if evidence is None:
                    return ActivationDenied(receipt_id=request.receipt_id)
                return Bitrix24ActivationAdmission(
                    binding_id=evidence.binding_id,
                    pair_candidate_reference=OpaqueProviderReference(
                        value=evidence.pair_candidate_reference
                    ),
                )

    async def activate(
        self,
        request: Bitrix24ActivationRequest,
        admission: Bitrix24ActivationAdmission,
    ) -> ConnectionActivated | ActivationDenied:
        scope = self._scope(request.connection, {"bitrix24_oauth_activate"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                evidence = await _activation_evidence(session, request)
                if (
                    evidence is None
                    or evidence.binding_id != admission.binding_id
                    or evidence.pair_candidate_reference
                    != admission.pair_candidate_reference.value
                ):
                    return ActivationDenied(receipt_id=request.receipt_id)
                existing = await _find_authority_by_binding(
                    session, admission.binding_id
                )
                if existing is not None:
                    if (
                        existing.active_pair_reference
                        != admission.pair_candidate_reference
                        or not _authority_matches_connection(
                            existing, request.connection
                        )
                    ):
                        return ActivationDenied(receipt_id=request.receipt_id)
                    return ConnectionActivated(
                        connection_version_id=request.connection.connection_version_id,
                        binding_id=existing.binding.binding_id,
                        token_epoch=existing.token_epoch,
                    )
                attempt = await _attempt_by_id(session, evidence.attempt_id)
                if attempt is None or not _attempt_matches_connection(
                    attempt, request.connection
                ):
                    return ActivationDenied(receipt_id=request.receipt_id)
                if not await _insert_connection_version(
                    session,
                    request.connection,
                    command_id=request.command_id,
                    occurred_at=request.occurred_at,
                ):
                    return ActivationDenied(receipt_id=request.receipt_id)
                binding = _binding_from_attempt(attempt, admission.binding_id)
                authority = Bitrix24GrantAuthority(
                    binding=binding,
                    active_pair_reference=admission.pair_candidate_reference,
                    token_epoch=1,
                    security_fence=0,
                    state=Bitrix24GrantState.READY,
                )
                await _insert_authority(
                    session, authority, recorded_at=request.occurred_at
                )
                await self._append_audit(
                    session,
                    binding=binding,
                    command_id=request.command_id,
                    command_type="bitrix24_oauth_activation",
                    reason="connection_activated",
                    occurred_at=request.occurred_at,
                )
                return ConnectionActivated(
                    connection_version_id=request.connection.connection_version_id,
                    binding_id=binding.binding_id,
                    token_epoch=1,
                )

    async def is_current(
        self, connection: ProviderConnection, pinned: Bitrix24PinnedGrant
    ) -> bool:
        authority = await self.current_authority(connection)
        return (
            authority is not None
            and authority.state is Bitrix24GrantState.READY
            and authority.binding.binding_id == pinned.binding_id
            and authority.token_epoch == pinned.token_epoch
            and authority.security_fence == pinned.security_fence == 0
        )

    async def claim_refresh(
        self, command: Bitrix24RefreshCommand
    ) -> RefreshClaim | RefreshBusy | RefreshUncertain | StaleRefreshFenced:
        scope = self._scope(command.connection, {"bitrix24_oauth_refresh"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                authority = await _find_authority(
                    session, command.connection, for_update=True
                )
                if (
                    authority is None
                    or authority.token_epoch != command.expected_token_epoch
                ):
                    return StaleRefreshFenced(
                        binding_id=(authority.binding.binding_id if authority else None)
                    )
                binding_id = authority.binding.binding_id
                row = await _authority_state_row(session, binding_id)
                if authority.state is Bitrix24GrantState.UNCERTAIN:
                    if row.refresh_attempt_id is None:
                        return StaleRefreshFenced(binding_id=binding_id)
                    return RefreshUncertain(
                        binding_id=binding_id,
                        attempt_id=row.refresh_attempt_id,
                    )
                if authority.state is Bitrix24GrantState.REFRESH_CLAIMED:
                    if row.refresh_attempt_id == command.attempt_id:
                        await session.execute(
                            update(bitrix24_grant_authorities)
                            .where(
                                bitrix24_grant_authorities.c.binding_id == binding_id,
                                bitrix24_grant_authorities.c.state
                                == Bitrix24GrantState.REFRESH_CLAIMED.value,
                                bitrix24_grant_authorities.c.refresh_attempt_id
                                == command.attempt_id,
                            )
                            .values(
                                state=Bitrix24GrantState.UNCERTAIN.value,
                                updated_at=func.now(),
                            )
                        )
                        await self._append_audit(
                            session,
                            binding=authority.binding,
                            command_id=command.attempt_id,
                            command_type="bitrix24_oauth_refresh",
                            reason="refresh_uncertain",
                            occurred_at=self._clock(),
                        )
                        return RefreshUncertain(
                            binding_id=binding_id,
                            attempt_id=command.attempt_id,
                        )
                    return RefreshBusy(
                        binding_id=binding_id, token_epoch=authority.token_epoch
                    )
                if (
                    authority.state is not Bitrix24GrantState.READY
                    or authority.security_fence
                ):
                    return StaleRefreshFenced(binding_id=binding_id)
                result = await session.execute(
                    update(bitrix24_grant_authorities)
                    .where(
                        bitrix24_grant_authorities.c.binding_id == binding_id,
                        bitrix24_grant_authorities.c.state
                        == Bitrix24GrantState.READY.value,
                        bitrix24_grant_authorities.c.token_epoch
                        == command.expected_token_epoch,
                        bitrix24_grant_authorities.c.security_fence == 0,
                    )
                    .values(
                        state=Bitrix24GrantState.REFRESH_CLAIMED.value,
                        refresh_attempt_id=command.attempt_id,
                        updated_at=func.now(),
                    )
                    .returning(bitrix24_grant_authorities.c.binding_id)
                )
                if result.scalar_one_or_none() is None:
                    return StaleRefreshFenced(binding_id=binding_id)
                return RefreshClaim(attempt_id=command.attempt_id, authority=authority)

    async def activate_candidate(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | StaleRefreshFenced:
        scope = self._binding_scope(claim.authority.binding, {"bitrix24_oauth_refresh"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                next_epoch = claim.authority.token_epoch + 1
                result = await session.execute(
                    update(bitrix24_grant_authorities)
                    .where(*_claim_predicates(claim))
                    .values(
                        active_pair_reference=candidate.value,
                        token_epoch=next_epoch,
                        state=Bitrix24GrantState.READY.value,
                        refresh_attempt_id=None,
                        updated_at=func.now(),
                    )
                    .returning(bitrix24_grant_authorities.c.binding_id)
                )
                if result.scalar_one_or_none() is None:
                    return StaleRefreshFenced(
                        binding_id=claim.authority.binding.binding_id
                    )
                outcome = PairActivated(
                    binding_id=claim.authority.binding.binding_id,
                    pair_reference=candidate,
                    token_epoch=next_epoch,
                )
                await self._append_audit(
                    session,
                    binding=claim.authority.binding,
                    command_id=claim.attempt_id,
                    command_type="bitrix24_oauth_refresh",
                    reason="pair_activated",
                    occurred_at=self._clock(),
                )
                return outcome

    async def resolve_activation(
        self, claim: RefreshClaim, candidate: OpaqueProviderReference
    ) -> PairActivated | RefreshUncertain | StaleRefreshFenced:
        scope = self._binding_scope(claim.authority.binding, {"bitrix24_oauth_refresh"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                current = await _find_authority_by_binding(
                    session, claim.authority.binding.binding_id
                )
                row = await _authority_state_row(
                    session, claim.authority.binding.binding_id
                )
        if (
            current is not None
            and current.state is Bitrix24GrantState.READY
            and current.active_pair_reference == candidate
            and current.token_epoch == claim.authority.token_epoch + 1
        ):
            return PairActivated(
                binding_id=current.binding.binding_id,
                pair_reference=candidate,
                token_epoch=current.token_epoch,
            )
        if (
            current is not None
            and current.state is Bitrix24GrantState.REFRESH_CLAIMED
            and row.refresh_attempt_id == claim.attempt_id
        ):
            return await self.mark_refresh_uncertain(claim)
        return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)

    async def mark_refresh_uncertain(
        self, claim: RefreshClaim
    ) -> RefreshUncertain | StaleRefreshFenced:
        if not await self._transition_claim(claim, Bitrix24GrantState.UNCERTAIN):
            return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)
        return RefreshUncertain(
            binding_id=claim.authority.binding.binding_id,
            attempt_id=claim.attempt_id,
        )

    async def mark_grant_uncertain(
        self, claim: RefreshClaim
    ) -> GrantUncertain | StaleRefreshFenced:
        if not await self._transition_claim(claim, Bitrix24GrantState.UNCERTAIN):
            return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)
        return GrantUncertain(
            binding_id=claim.authority.binding.binding_id,
            attempt_id=claim.attempt_id,
        )

    async def require_reauthorization(
        self, claim: RefreshClaim
    ) -> ReauthorizationRequired | StaleRefreshFenced:
        if not await self._transition_claim(
            claim, Bitrix24GrantState.REAUTHORIZATION_REQUIRED
        ):
            return StaleRefreshFenced(binding_id=claim.authority.binding.binding_id)
        return ReauthorizationRequired(binding_id=claim.authority.binding.binding_id)

    async def contain(
        self, command: Bitrix24ContainmentCommand
    ) -> GrantContained | StaleReadFenced:
        scope = self._scope(command.connection, {"bitrix24_oauth_contain"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                authority = await _find_authority(
                    session, command.connection, for_update=True
                )
                if (
                    authority is None
                    or authority.binding.binding_id != command.binding_id
                    or authority.security_fence != command.expected_security_fence
                ):
                    return StaleReadFenced()
                next_fence = authority.security_fence + 1
                await session.execute(
                    update(bitrix24_grant_authorities)
                    .where(
                        bitrix24_grant_authorities.c.binding_id == command.binding_id,
                        bitrix24_grant_authorities.c.security_fence
                        == command.expected_security_fence,
                    )
                    .values(
                        security_fence=next_fence,
                        state=Bitrix24GrantState.CONTAINED.value,
                        refresh_attempt_id=None,
                        incident_reference=command.incident_reference.value,
                        updated_at=func.now(),
                    )
                )
                outcome = GrantContained(
                    binding_id=command.binding_id,
                    security_fence=next_fence,
                    incident_reference=command.incident_reference,
                )
                await self._append_audit(
                    session,
                    binding=authority.binding,
                    command_id=command.command_id,
                    command_type="bitrix24_oauth_containment",
                    reason="grant_contained",
                    occurred_at=self._clock(),
                )
                return outcome

    async def confirm_uninstall(
        self, evidence: Bitrix24UninstallEvidence
    ) -> ReauthorizationRequired | Bitrix24ConfirmedUninstall:
        scope = self._scope(evidence.connection, {"bitrix24_oauth_uninstall"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                authority = await _find_authority(
                    session, evidence.connection, for_update=True
                )
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
                await session.execute(
                    update(bitrix24_grant_authorities)
                    .where(
                        bitrix24_grant_authorities.c.binding_id
                        == evidence.binding_id
                    )
                    .values(
                        state=Bitrix24GrantState.REAUTHORIZATION_REQUIRED.value,
                        refresh_attempt_id=None,
                        updated_at=func.now(),
                    )
                )
                await self._append_audit(
                    session,
                    binding=authority.binding,
                    command_id=evidence.qualification_reference.value,
                    command_type="bitrix24_oauth_uninstall",
                    reason="confirmed_uninstall",
                    occurred_at=evidence.observed_at,
                )
                return ReauthorizationRequired(binding_id=evidence.binding_id)

    async def recover(
        self, command: Bitrix24RecoveryCommand
    ) -> GrantRecovered | RecoveryDenied:
        scope = self._scope(command.contained_connection, {"bitrix24_oauth_recover"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                current = await _find_authority(
                    session, command.contained_connection, for_update=True
                )
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
                if not await _insert_connection_version(
                    session,
                    command.replacement_connection,
                    command_id=command.command_id,
                    occurred_at=self._clock(),
                ):
                    return RecoveryDenied(binding_id=current.binding.binding_id)
                await _insert_authority(
                    session,
                    command.replacement_authority,
                    recorded_at=func.now(),
                )
                outcome = GrantRecovered(
                    previous_binding_id=current.binding.binding_id,
                    binding_id=command.replacement_authority.binding.binding_id,
                    token_epoch=command.replacement_authority.token_epoch,
                )
                await self._append_audit(
                    session,
                    binding=command.replacement_authority.binding,
                    command_id=command.command_id,
                    command_type="bitrix24_oauth_recovery",
                    reason="grant_recovered",
                    occurred_at=self._clock(),
                )
                return outcome

    async def _transition_claim(
        self, claim: RefreshClaim, state: Bitrix24GrantState
    ) -> bool:
        scope = self._binding_scope(claim.authority.binding, {"bitrix24_oauth_refresh"})
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(_BIND_SCOPE, _scope_parameters(scope))
                result = await session.execute(
                    update(bitrix24_grant_authorities)
                    .where(*_claim_predicates(claim))
                    .values(state=state.value, updated_at=func.now())
                    .returning(bitrix24_grant_authorities.c.binding_id)
                )
                transitioned = result.scalar_one_or_none() is not None
                if transitioned:
                    await self._append_audit(
                        session,
                        binding=claim.authority.binding,
                        command_id=claim.attempt_id,
                        command_type="bitrix24_oauth_refresh",
                        reason=(
                            "reauthorization_required"
                            if state is Bitrix24GrantState.REAUTHORIZATION_REQUIRED
                            else "grant_uncertain"
                        ),
                        occurred_at=self._clock(),
                    )
                return transitioned

    async def _append_audit(
        self,
        session,
        *,
        binding: Bitrix24GrantBinding,
        command_id: UUID,
        command_type: str,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        trusted = self._context_verifier.verify(self._context)
        event = AuditEventRegistry.with_default_families().build_event(
            audit_event_id=uuid5(
                NAMESPACE_URL,
                f"spine:bitrix24-oauth-audit:v1:{command_type}:{command_id}:{reason}",
            ),
            workspace_id=binding.workspace_id,
            environment_id=binding.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(command_type=command_type),
            origin=trusted.origin,
            acting_subject_id=trusted.acting_subject_id,
            service_principal_id=trusted.service_principal_id,
            trace_id=trusted.trace_id,
            occurred_at=occurred_at,
            target=AuditObjectReference(
                object_type="bitrix24_grant_binding",
                object_id=binding.binding_id,
                schema_version=1,
            ),
            outcome=AuditOutcome.ACCEPTED,
            reason=reason,
            producer_deduplication_id=f"bitrix24:{command_id}:{reason}",
        )
        target = event.target
        await session.execute(
            insert(audit_events).values(
                audit_event_id=event.audit_event_id,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                origin=event.origin.value,
                acting_subject_id=event.acting_subject_id,
                service_principal_id=event.service_principal_id,
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                occurred_at=event.occurred_at,
                appended_at=event.occurred_at,
                target_type=target.object_type if target else None,
                target_id=target.object_id if target else None,
                target_schema_version=target.schema_version if target else None,
                outcome=event.outcome.value,
                reason=event.reason,
                producer_deduplication_id=event.producer_deduplication_id,
                payload=event.payload_json(),
            )
        )

    def _scope(
        self, connection: ProviderConnection, operations: set[str]
    ) -> EnvironmentScope:
        trusted = self._context_verifier.verify(self._context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.operation.value not in operations
            or (
                trusted.scope.workspace_id,
                trusted.scope.environment_id,
            )
            != (connection.workspace_id, connection.environment_id)
            or (
                trusted.origin is ContextOrigin.WORKER
                and trusted.service_principal_id != connection.service_principal_id
            )
        ):
            raise PermissionError("Bitrix24 OAuth authority is unavailable.")
        return trusted.scope

    def _binding_scope(
        self, binding: Bitrix24GrantBinding, operations: set[str]
    ) -> EnvironmentScope:
        trusted = self._context_verifier.verify(self._context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.operation.value not in operations
            or (
                trusted.scope.workspace_id,
                trusted.scope.environment_id,
            )
            != (binding.workspace_id, binding.environment_id)
        ):
            raise PermissionError("Bitrix24 OAuth authority is unavailable.")
        return trusted.scope

    def _attempt_scope(
        self, attempt: Bitrix24BootstrapAttempt, operations: set[str]
    ) -> EnvironmentScope:
        trusted = self._context_verifier.verify(self._context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.operation.value not in operations
            or (
                trusted.scope.workspace_id,
                trusted.scope.environment_id,
            )
            != (attempt.workspace_id, attempt.environment_id)
        ):
            raise PermissionError("Bitrix24 OAuth authority is unavailable.")
        return trusted.scope

    def _callback_scope(
        self, command: Bitrix24CallbackCommand, operations: set[str]
    ) -> EnvironmentScope:
        trusted = self._context_verifier.verify(self._context)
        if (
            not isinstance(trusted.scope, EnvironmentScope)
            or trusted.operation.value not in operations
            or (
                trusted.scope.workspace_id,
                trusted.scope.environment_id,
            )
            != (command.workspace_id, command.environment_id)
        ):
            raise PermissionError("Bitrix24 OAuth authority is unavailable.")
        return trusted.scope


def _scope_parameters(scope: EnvironmentScope) -> dict[str, str]:
    return {
        "workspace_id": str(scope.workspace_id),
        "environment_id": str(scope.environment_id),
    }


def _authority_select():
    binding = bitrix24_grant_bindings
    authority = bitrix24_grant_authorities
    return select(
        *binding.c,
        authority.c.active_pair_reference,
        authority.c.token_epoch,
        authority.c.security_fence,
        authority.c.state,
        authority.c.refresh_attempt_id,
        authority.c.incident_reference,
        authority.c.updated_at,
    ).join(authority, authority.c.binding_id == binding.c.binding_id)


async def _find_authority(
    session, connection: ProviderConnection, *, for_update: bool = False
) -> Bitrix24GrantAuthority | None:
    binding = bitrix24_grant_bindings
    statement = _authority_select().where(
        binding.c.workspace_id == connection.workspace_id,
        binding.c.environment_id == connection.environment_id,
        binding.c.approval_id == connection.approval_id,
        binding.c.connection_id == connection.connection_id,
        binding.c.connection_version_id == connection.connection_version_id,
        binding.c.grant_reference == connection.grant_reference.value,
        binding.c.authorization_generation == connection.authorization_generation,
        binding.c.configuration_generation == connection.configuration_generation,
    )
    if for_update:
        statement = statement.with_for_update(of=bitrix24_grant_authorities)
    row = (await session.execute(statement)).mappings().one_or_none()
    return _authority_from_row(row) if row is not None else None


async def _activation_evidence(session, request: Bitrix24ActivationRequest):
    receipt = (
        (
            await session.execute(
                select(bitrix24_oauth_callback_receipts).where(
                    bitrix24_oauth_callback_receipts.c.receipt_id == request.receipt_id,
                    bitrix24_oauth_callback_receipts.c.workspace_id
                    == request.connection.workspace_id,
                    bitrix24_oauth_callback_receipts.c.environment_id
                    == request.connection.environment_id,
                    bitrix24_oauth_callback_receipts.c.outcome == "accepted",
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if receipt is None:
        return None
    attempt = await _attempt_by_id(session, receipt.attempt_id)
    if attempt is None or not _attempt_matches_connection(attempt, request.connection):
        return None
    return receipt


async def _attempt_by_id(session, attempt_id: UUID) -> Bitrix24BootstrapAttempt | None:
    row = (
        (
            await session.execute(
                select(bitrix24_oauth_attempts).where(
                    bitrix24_oauth_attempts.c.attempt_id == attempt_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return _attempt_from_row(row) if row is not None else None


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


def _binding_from_attempt(
    attempt: Bitrix24BootstrapAttempt, binding_id: UUID
) -> Bitrix24GrantBinding:
    return Bitrix24GrantBinding(
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
        granted_scopes=tuple(Bitrix24Scope(scope) for scope in attempt.approved_scopes),
        authorization_generation=attempt.authorization_generation,
        configuration_generation=attempt.configuration_generation,
    )


def _authority_matches_connection(
    authority: Bitrix24GrantAuthority, connection: ProviderConnection
) -> bool:
    binding = authority.binding
    return (
        binding.workspace_id,
        binding.environment_id,
        binding.approval_id,
        binding.connection_id,
        binding.connection_version_id,
        binding.grant_reference,
        binding.authorization_generation,
        binding.configuration_generation,
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


async def _current_provider_connection_row(
    session,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    connection_id: UUID,
):
    row = (
        (
            await session.execute(
                select(provider_connection_versions)
                .where(
                    provider_connection_versions.c.workspace_id == workspace_id,
                    provider_connection_versions.c.environment_id == environment_id,
                    provider_connection_versions.c.connection_id == connection_id,
                )
                .order_by(provider_connection_versions.c.version.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    return row


async def _insert_connection_version(
    session,
    connection: ProviderConnection,
    *,
    command_id: UUID,
    occurred_at: datetime,
) -> bool:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:connection_id, 0))"),
        {"connection_id": str(connection.connection_id)},
    )
    approval = (
        (
            await session.execute(
                select(provider_connection_approvals).where(
                    provider_connection_approvals.c.approval_id
                    == connection.approval_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if approval is None or not _approval_matches_connection(approval, connection):
        return False
    current = await _current_provider_connection_row(
        session,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        connection_id=connection.connection_id,
    )
    if current is None:
        if connection.version != 1 or connection.predecessor_version_id is not None:
            return False
    elif (
        connection.version != current.version + 1
        or connection.predecessor_version_id != current.connection_version_id
    ):
        return False
    digest_payload = {
        "approval_id": str(connection.approval_id),
        "connection_id": str(connection.connection_id),
        "connection_version_id": str(connection.connection_version_id),
        "grant_reference": str(connection.grant_reference.value),
        "authorization_generation": connection.authorization_generation,
        "configuration_generation": connection.configuration_generation,
    }
    command_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await session.execute(
        insert(provider_connection_versions).values(
            connection_version_id=connection.connection_version_id,
            connection_id=connection.connection_id,
            approval_id=connection.approval_id,
            version=connection.version,
            predecessor_version_id=connection.predecessor_version_id,
            workspace_id=connection.workspace_id,
            environment_id=connection.environment_id,
            service_principal_id=connection.service_principal_id,
            provider_kind=connection.provider_kind.value,
            credential_reference=connection.credential_reference.value,
            grant_reference=connection.grant_reference.value,
            approved_resource_scope_references=[
                item.value for item in connection.approved_resource_scope_references
            ],
            authorization_generation=connection.authorization_generation,
            configuration_generation=connection.configuration_generation,
            status=connection.status.value,
            capabilities=[
                item.model_dump(mode="json") for item in connection.capabilities
            ],
            health="healthy",
            diagnostic_codes=[],
            observed_at=occurred_at,
            cursor_lag_seconds=None,
            last_successful_reconciliation_at=None,
            receipt_id=command_id,
            idempotency_key=f"bitrix24-oauth:{command_id}",
            command_digest=command_digest,
            recorded_at=occurred_at,
        )
    )
    return True


def _approval_matches_connection(approval, connection: ProviderConnection) -> bool:
    return (
        approval.workspace_id == connection.workspace_id
        and approval.environment_id == connection.environment_id
        and approval.service_principal_id == connection.service_principal_id
        and approval.provider_kind == connection.provider_kind.value
        and approval.credential_reference == connection.credential_reference.value
        and approval.grant_reference == connection.grant_reference.value
        and frozenset(approval.approved_resource_scope_references)
        == frozenset(
            item.value for item in connection.approved_resource_scope_references
        )
        and approval.authorization_generation == connection.authorization_generation
        and approval.configuration_generation == connection.configuration_generation
        and approval.status == connection.status.value
    )


async def _find_authority_by_binding(
    session, binding_id: UUID
) -> Bitrix24GrantAuthority | None:
    row = (
        (
            await session.execute(
                _authority_select().where(
                    bitrix24_grant_bindings.c.binding_id == binding_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return _authority_from_row(row) if row is not None else None


async def _authority_state_row(session, binding_id: UUID):
    return (
        (
            await session.execute(
                select(bitrix24_grant_authorities).where(
                    bitrix24_grant_authorities.c.binding_id == binding_id
                )
            )
        )
        .mappings()
        .one()
    )


def _authority_from_row(row) -> Bitrix24GrantAuthority:
    binding = Bitrix24GrantBinding(
        binding_id=row.binding_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        approval_id=row.approval_id,
        connection_id=row.connection_id,
        connection_version_id=row.connection_version_id,
        grant_reference=OpaqueProviderReference(value=row.grant_reference),
        portal_member_id=row.portal_member_id,
        application_id=row.application_id,
        acting_user_id=row.acting_user_id,
        granted_scopes=tuple(Bitrix24Scope(scope) for scope in row.granted_scopes),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
    )
    return Bitrix24GrantAuthority(
        binding=binding,
        active_pair_reference=OpaqueProviderReference(value=row.active_pair_reference),
        token_epoch=row.token_epoch,
        security_fence=row.security_fence,
        state=Bitrix24GrantState(row.state),
    )


def _attempt_from_row(row) -> Bitrix24BootstrapAttempt:
    return Bitrix24BootstrapAttempt(
        attempt_id=row.attempt_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        approval_id=row.approval_id,
        connection_id=row.connection_id,
        connection_version_id=row.connection_version_id,
        grant_reference=OpaqueProviderReference(value=row.grant_reference),
        portal_member_id=row.portal_member_id,
        application_id=row.application_id,
        installing_user_id=row.installing_user_id,
        approved_scopes=tuple(row.approved_scopes),
        authorization_generation=row.authorization_generation,
        configuration_generation=row.configuration_generation,
        expires_at=row.expires_at,
        state=BootstrapAttemptState(row.status),
    )


def _claim_predicates(claim: RefreshClaim) -> tuple[object, ...]:
    authority = bitrix24_grant_authorities
    return (
        authority.c.binding_id == claim.authority.binding.binding_id,
        authority.c.state == Bitrix24GrantState.REFRESH_CLAIMED.value,
        authority.c.refresh_attempt_id == claim.attempt_id,
        authority.c.token_epoch == claim.authority.token_epoch,
        authority.c.security_fence == claim.authority.security_fence,
    )


async def _insert_authority(
    session, authority: Bitrix24GrantAuthority, *, recorded_at
) -> None:
    binding = authority.binding
    await session.execute(
        insert(bitrix24_grant_bindings).values(
            binding_id=binding.binding_id,
            workspace_id=binding.workspace_id,
            environment_id=binding.environment_id,
            approval_id=binding.approval_id,
            connection_id=binding.connection_id,
            connection_version_id=binding.connection_version_id,
            grant_reference=binding.grant_reference.value,
            portal_member_id=binding.portal_member_id,
            application_id=binding.application_id,
            acting_user_id=binding.acting_user_id,
            granted_scopes=tuple(scope.value for scope in binding.granted_scopes),
            authorization_generation=binding.authorization_generation,
            configuration_generation=binding.configuration_generation,
            recorded_at=recorded_at,
        )
    )
    await session.execute(
        insert(bitrix24_grant_authorities).values(
            binding_id=binding.binding_id,
            workspace_id=binding.workspace_id,
            environment_id=binding.environment_id,
            active_pair_reference=authority.active_pair_reference.value,
            token_epoch=authority.token_epoch,
            security_fence=authority.security_fence,
            state=authority.state.value,
            refresh_attempt_id=None,
            incident_reference=None,
            updated_at=recorded_at,
        )
    )


__all__ = [
    "PostgreSQLBitrix24OAuthRepository",
    "bitrix24_grant_authorities",
    "bitrix24_grant_bindings",
    "bitrix24_oauth_attempts",
    "bitrix24_oauth_callback_receipts",
]
