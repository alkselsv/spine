from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from spine.application.connections.email_read import (
    MailboxReadCommand,
    MailboxReadDeniedError,
)
from spine.application.email_sync import (
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.gmail_initial_snapshot import (
    GmailHistoryBaseline,
    GmailHistoryBaselineState,
    GmailInitialSnapshotCommand,
    GmailSnapshotPage,
    gmail_snapshot_effect_key,
)
from spine.application.gmail_message_state import (
    GmailMessageEvidenceBinding,
    InMemoryGmailMessageStateRepository,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.connectors.gmail import (
    GmailEnumerationResult,
    GmailHistoryReadCommand,
    GmailHistoryPageReadRequest,
    GmailMessageIdPage,
    GmailRawMessageResponse,
    GmailRawMessageStatus,
    GmailReadRequest,
    compose_gmail_history_reader,
    compose_gmail_mailbox_reader,
)
from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryResult,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.connectors.gmail_message_state import GmailMessageStateWriter
from spine.connectors.gmail_checkpoints import (
    decode_gmail_history_checkpoint,
    decode_gmail_snapshot_page_checkpoint,
    encode_gmail_history_checkpoint,
)
from spine.connectors.gmail_snapshot import (
    GmailHistoryBaselineReadCommand,
    GmailInitialSnapshotProviderReader,
    GmailInitialHistoryDrainAdapter,
    GmailProfileResponse,
    GmailSnapshotPageReadCommand,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.email_scope import (
    EmailOperation,
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.sources.gmail import GmailMessageKey
from spine.workers.gmail_initial_snapshot import (
    create_gmail_initial_snapshot_operation,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope(**changes: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "connection_id": _id(3),
        "connection_version_id": _id(4),
        "provider_kind": "google",
        "resource_scope_reference": _id(5),
        "mailbox_subject_reference": _id(6),
        "mailbox_binding_id": _id(7),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "scope_generation": 10,
        "allowed_label_references": (_id(11),),
        "allowed_operations": (EmailProviderOperation.READ_MESSAGE_METADATA,),
    }
    values.update(changes)
    return MailboxReadScope(**values)


def _connection(**changes: object) -> ProviderConnection:
    capabilities = tuple(
        ProviderCapability(
            kind=kind,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=OpaqueProviderReference(value=_id(30 + index)),
        )
        for index, kind in enumerate(
            (
                ProviderCapabilityKind.MAILBOX_ENUMERATION,
                ProviderCapabilityKind.MESSAGE_METADATA_READ,
                ProviderCapabilityKind.RAW_MESSAGE_READ,
            )
        )
    )
    values: dict[str, object] = {
        "connection_version_id": _id(4),
        "connection_id": _id(3),
        "approval_id": _id(23),
        "version": 1,
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "service_principal_id": _id(21),
        "provider_kind": ProviderKind.GOOGLE,
        "credential_reference": OpaqueProviderReference(value=_id(24)),
        "grant_reference": OpaqueProviderReference(value=_id(25)),
        "approved_resource_scope_references": (OpaqueProviderReference(value=_id(5)),),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "status": ProviderConnectionStatus.ACTIVE,
        "capabilities": capabilities,
    }
    values.update(changes)
    return ProviderConnection(**values)


def _context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=_id(1), environment_id=_id(2)),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("connector.email.read.v1"),
        operation=PersistenceOperation("provider_connection_read"),
        trace_id=_id(20),
        acting_subject_id=None,
        service_principal_id=_id(21),
        provenance=TrustedContextProvenance(issuer_id=_id(22), signature=b"trusted"),
    )


def _state_writer(
    scope: MailboxReadScope, *, message_id: str | None = None
) -> tuple[GmailMessageStateWriter, InMemoryGmailMessageStateRepository]:
    bindings = ()
    if message_id is not None:
        bindings = (
            GmailMessageEvidenceBinding(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                source_object_id=_id(40),
                source_revision_id=_id(41),
                message_key=GmailMessageKey(
                    connection_id=scope.connection_id,
                    mailbox_binding_id=scope.mailbox_binding_id,
                    message_id=message_id,
                ),
            ),
        )
    repository = InMemoryGmailMessageStateRepository(bindings)
    return (
        GmailMessageStateWriter(
            repository=repository,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            mailbox_binding_id=scope.mailbox_binding_id,
        ),
        repository,
    )


class _Scopes:
    def __init__(self, approved: MailboxReadScope | None) -> None:
        self.approved = approved
        self.calls = 0

    async def resolve_exact(self, scope, context):
        del scope, context
        self.calls += 1
        return self.approved


class _Connections:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.calls = 0

    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        self.calls += 1
        yield self.connection


class _RecordingGmailClient:
    def __init__(self) -> None:
        self.requests: list[GmailReadRequest] = []
        self.failure: Exception | None = None

    async def read(self, request: GmailReadRequest) -> str:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return "synthetic-gmail-result"


class _RawGmailClient(_RecordingGmailClient):
    def __init__(self, response: GmailRawMessageResponse | None = None) -> None:
        super().__init__()
        self.response = response

    async def read(self, request: GmailReadRequest) -> GmailRawMessageResponse:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        assert self.response is not None
        return self.response


class _SnapshotGmailClient(_RecordingGmailClient):
    def __init__(self, responses: list[object]) -> None:
        super().__init__()
        self.responses = responses

    async def read(self, request: GmailReadRequest) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


class _HistoryAdmission:
    def __init__(self, result: GmailHistoryResult) -> None:
        self.result = result

    async def read(self, command, context):
        del command, context
        return self.result


class _StagingHistoryApplier:
    def __init__(self, mailbox_scope: MailboxReadScope) -> None:
        self.scope = mailbox_scope
        self.result: GmailHistoryResult | None = None

    async def stage(self, attempt_id, result, *, start_history_id):
        del attempt_id, start_history_id
        self.result = result
        return "staged"


class _SnapshotAdmission:
    async def check(self, admission):
        del admission
        return EmailSyncAdmissionState.ALLOWED


class _NoEffects:
    async def apply(self, effect):
        del effect
        raise AssertionError("empty Gmail snapshot has no effects")


class _EmptySnapshotReader:
    def __init__(self, mailbox_scope: MailboxReadScope) -> None:
        self.scope = mailbox_scope

    async def capture_history_baseline(self, mailbox_scope):
        assert mailbox_scope == self.scope
        return GmailHistoryBaseline(
            state=GmailHistoryBaselineState.CAPTURED,
            admission_scope_digest=gmail_history_scope_digest(self.scope),
            checkpoint=encode_gmail_history_checkpoint(self.scope, "100"),
        )

    async def enumerate_snapshot_page(self, request):
        assert request.scope == self.scope
        return GmailSnapshotPage(requested_checkpoint=request.checkpoint)


class _NoHistoryEffects:
    async def prepare(self, mailbox_scope, change):
        del mailbox_scope, change
        raise AssertionError("empty Gmail history has no effects")


class _HttpFailure(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("provider payload secret subject@example.test")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_initial_snapshot_provider_encodes_baseline_and_page_cursor() -> None:
    mailbox_scope = _scope(
        allowed_operations=(
            EmailProviderOperation.ENUMERATE_MAILBOX,
            EmailProviderOperation.SYNCHRONIZE,
        )
    )
    client = _SnapshotGmailClient(
        [
            GmailProfileResponse(history_id="100"),
            GmailMessageIdPage(
                message_ids=("message-a",), next_page_token="provider-secret"
            ),
        ]
    )
    provider = GmailInitialSnapshotProviderReader(client)
    connection = _connection()
    connection = connection.model_copy(
        update={
            "capabilities": connection.capabilities
            + (
                ProviderCapability(
                    kind=ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=OpaqueProviderReference(value=_id(40)),
                ),
            )
        }
    )
    baseline = await provider.read(
        connection,
        GmailHistoryBaselineReadCommand(
            scope=mailbox_scope,
            operation=EmailOperation.SYNCHRONIZE,
            mailbox_subject_reference=mailbox_scope.mailbox_subject_reference,
            folder_or_label_reference=_id(11),
        ),
    )
    page = await provider.read(
        connection,
        GmailSnapshotPageReadCommand(
            scope=mailbox_scope,
            operation=EmailOperation.ENUMERATE_MAILBOX,
            mailbox_subject_reference=mailbox_scope.mailbox_subject_reference,
            checkpoint=None,
            provider_page_token=None,
            max_results=10,
        ),
    )

    assert decode_gmail_history_checkpoint(mailbox_scope, baseline.checkpoint) == "100"
    assert page.message_ids == ("message-a",)
    assert (
        decode_gmail_snapshot_page_checkpoint(mailbox_scope, page.next_checkpoint)
        == "provider-secret"
    )
    assert [request.resource for request in client.requests] == ["profile", "messages"]


def test_gmail_checkpoint_rejects_another_generation() -> None:
    mailbox_scope = _scope()
    checkpoint = encode_gmail_history_checkpoint(mailbox_scope, "100")

    with pytest.raises(ValueError, match="outside the admitted generation"):
        decode_gmail_history_checkpoint(
            mailbox_scope.model_copy(update={"scope_generation": 11}), checkpoint
        )


@pytest.mark.asyncio
async def test_initial_history_drain_removes_snapshot_message_overlap() -> None:
    mailbox_scope = _scope(allowed_operations=(EmailProviderOperation.SYNCHRONIZE,))
    result = GmailHistoryResult(
        requested_start_history_id="100",
        admission_scope_digest=gmail_history_scope_digest(mailbox_scope),
        state=GmailHistoryState.COMPLETE,
        terminal_history_id="102",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
        changes=(
            GmailHistoryChange(
                history_id="101",
                kind=GmailHistoryChangeKind.MESSAGE_ADDED,
                message_id="message-a",
            ),
            GmailHistoryChange(
                history_id="102",
                kind=GmailHistoryChangeKind.MESSAGE_DELETED,
                message_id="message-b",
            ),
        ),
    )
    applier = _StagingHistoryApplier(mailbox_scope)
    adapter = GmailInitialHistoryDrainAdapter(
        reader=_HistoryAdmission(result),
        applier=applier,
        context_factory=lambda operation, trace_id: _context(),
    )

    staged = await adapter.stage(
        _id(50),
        mailbox_scope,
        baseline_checkpoint=encode_gmail_history_checkpoint(mailbox_scope, "100"),
        max_results=100,
        max_pages=2,
        covered_snapshot_effect_keys=(
            gmail_snapshot_effect_key(mailbox_scope, "message-a"),
        ),
    )

    assert staged == "staged"
    assert applier.result is not None
    assert [change.kind for change in applier.result.changes] == [
        GmailHistoryChangeKind.MESSAGE_DELETED
    ]


@pytest.mark.asyncio
async def test_worker_composition_executes_bounded_gmail_snapshot() -> None:
    mailbox_scope = _scope(
        allowed_operations=(
            EmailProviderOperation.ENUMERATE_MAILBOX,
            EmailProviderOperation.READ_RAW_MESSAGE,
            EmailProviderOperation.SYNCHRONIZE,
        )
    )
    history_result = GmailHistoryResult(
        requested_start_history_id="100",
        admission_scope_digest=gmail_history_scope_digest(mailbox_scope),
        state=GmailHistoryState.COMPLETE,
        terminal_history_id="101",
        pages_read=1,
        duplicate_count=0,
        malformed_count=0,
    )
    operation = create_gmail_initial_snapshot_operation(
        scope=mailbox_scope,
        sync=EmailSyncCoordinator(
            repository=InMemoryEmailSyncRepository(),
            admission=_SnapshotAdmission(),
            effects=_NoEffects(),
        ),
        snapshot_reader=_EmptySnapshotReader(mailbox_scope),
        history_reader=_HistoryAdmission(history_result),
        history_effects=_NoHistoryEffects(),
        context_factory=lambda operation, trace_id: _context(),
        ingestion=object(),
        raw_messages=object(),
        clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )

    result = await operation.run(
        GmailInitialSnapshotCommand(
            scope=mailbox_scope,
            attempt_key="issue-838-worker-composition",
            snapshot_page_size=10,
            max_snapshot_pages=2,
            history_page_size=10,
            max_history_pages=2,
            max_message_bytes=4096,
            access_policy_version_id=_id(60),
        )
    )

    assert result.attempt.status is EmailSyncStatus.COMPLETE
    assert result.completed_snapshot_pages == 1
    assert result.snapshot_messages == 0


class _RecordingGmailHistoryClient:
    def __init__(self, pages: tuple[dict[str, object], ...]) -> None:
        self.pages = pages
        self.requests: list[GmailHistoryPageReadRequest] = []

    async def read(self, request: GmailReadRequest) -> dict[str, object]:
        self.requests.append(request)
        return self.pages[len(self.requests) - 1]


def _compose(
    *,
    scope: MailboxReadScope | None = None,
    approved: MailboxReadScope | None = None,
    connection: ProviderConnection | None = None,
):
    exact_scope = scope or _scope()
    scopes = _Scopes(approved if approved is not None else exact_scope)
    connections = _Connections(connection or _connection())
    client = _RecordingGmailClient()
    reader = compose_gmail_mailbox_reader(
        scopes=scopes,
        connections=connections,
        client=client,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.READ_MESSAGE_METADATA,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
    )
    return reader, command, scopes, connections, client


def _raw_response(**changes: object) -> GmailRawMessageResponse:
    payload = b"From: synthetic@example.test\r\n\r\nSynthetic body."
    values: dict[str, object] = {
        "http_status": 200,
        "message_id": "gmail-message-123",
        "raw_data": base64.urlsafe_b64encode(payload).decode("ascii").rstrip("="),
        "label_references": (_id(11),),
        "internal_date_ms": 1_800_000_000_000,
        "history_id": "987654321",
        "thread_id": "gmail-thread-456",
    }
    values.update(changes)
    return GmailRawMessageResponse(**values)


def _compose_raw(
    *,
    response: GmailRawMessageResponse | None = None,
    connection: ProviderConnection | None = None,
    max_raw_message_bytes: int | None = 4096,
    **scope_changes: object,
):
    exact_scope = _scope(
        allowed_operations=(EmailProviderOperation.READ_RAW_MESSAGE,), **scope_changes
    )
    scopes = _Scopes(exact_scope)
    connections = _Connections(connection or _connection())
    client = _RawGmailClient(response or _raw_response())
    reader = compose_gmail_mailbox_reader(
        scopes=scopes,
        connections=connections,
        client=client,
        max_raw_message_bytes=max_raw_message_bytes,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.READ_RAW_MESSAGE,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        message_id="gmail-message-123",
    )
    return reader, command, client


@pytest.mark.asyncio
async def test_admitted_google_mailbox_reaches_fake_client_with_opaque_read_request() -> (
    None
):
    reader, command, _, _, client = _compose()

    result = await reader.read(command, _context())

    assert result == "synthetic-gmail-result"
    assert client.requests == [
        GmailReadRequest(
            operation=EmailOperation.READ_MESSAGE_METADATA,
            workspace_id=_id(1),
            environment_id=_id(2),
            connection_id=_id(3),
            connection_version_id=_id(4),
            credential_reference=OpaqueProviderReference(value=_id(24)),
            grant_reference=OpaqueProviderReference(value=_id(25)),
            resource_scope_reference=OpaqueProviderReference(value=_id(5)),
            mailbox_subject_reference=_id(6),
            mailbox_binding_id=_id(7),
            authorization_generation=8,
            configuration_generation=9,
            scope_generation=10,
            label_references=(_id(11),),
        )
    ]


@pytest.mark.asyncio
async def test_gmail_enumeration_consumes_pages_and_deduplicates_ids() -> None:
    exact_scope = _scope(allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,))

    class Pages:
        def __init__(self) -> None:
            self.requests: list[GmailReadRequest] = []

        async def read(self, request: GmailReadRequest) -> GmailMessageIdPage:
            self.requests.append(request)
            if request.page_token is None:
                return GmailMessageIdPage(
                    message_ids=("m2", "m1"), next_page_token="next"
                )
            return GmailMessageIdPage(message_ids=("m1", "m3"))

    client = Pages()
    reader = compose_gmail_mailbox_reader(
        scopes=_Scopes(exact_scope),
        connections=_Connections(_connection()),
        client=client,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.ENUMERATE_MAILBOX,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
    )

    result = await reader.read(command, _context())

    assert result == GmailEnumerationResult(
        message_ids=("m2", "m1", "m3"), complete=True, pages_read=2
    )
    assert [request.page_token for request in client.requests] == [None, "next"]
    assert all(request.label_references == (_id(11),) for request in client.requests)


@pytest.mark.asyncio
async def test_gmail_enumeration_returns_partial_coverage_at_page_limit() -> None:
    exact_scope = _scope(allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,))

    class Pages:
        async def read(self, request: GmailReadRequest) -> GmailMessageIdPage:
            del request
            return GmailMessageIdPage(message_ids=("m1",), next_page_token="next")

    reader = compose_gmail_mailbox_reader(
        scopes=_Scopes(exact_scope),
        connections=_Connections(_connection()),
        client=Pages(),
        max_pages=1,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.ENUMERATE_MAILBOX,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
    )

    result = await reader.read(command, _context())

    assert result == GmailEnumerationResult(
        message_ids=("m1",), complete=False, pages_read=1, continuation_token="next"
    )


@pytest.mark.asyncio
async def test_gmail_enumeration_marks_empty_page_partial() -> None:
    exact_scope = _scope(allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,))

    class Pages:
        async def read(self, request: GmailReadRequest) -> GmailMessageIdPage:
            del request
            return GmailMessageIdPage()

    reader = compose_gmail_mailbox_reader(
        scopes=_Scopes(exact_scope),
        connections=_Connections(_connection()),
        client=Pages(),
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.ENUMERATE_MAILBOX,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
    )

    result = await reader.read(command, _context())

    assert result == GmailEnumerationResult(
        message_ids=(), complete=False, pages_read=1
    )


@pytest.mark.asyncio
async def test_gmail_enumeration_preserves_continuation_at_item_limit() -> None:
    exact_scope = _scope(allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,))

    class Pages:
        async def read(self, request: GmailReadRequest) -> GmailMessageIdPage:
            del request
            return GmailMessageIdPage(message_ids=("m1", "m2"), next_page_token="next")

    reader = compose_gmail_mailbox_reader(
        scopes=_Scopes(exact_scope),
        connections=_Connections(_connection()),
        client=Pages(),
        max_items=1,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.ENUMERATE_MAILBOX,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
    )

    result = await reader.read(command, _context())

    assert result == GmailEnumerationResult(
        message_ids=("m1",), complete=False, pages_read=1, continuation_token="next"
    )


@pytest.mark.asyncio
async def test_gmail_enumeration_rejects_repeated_page_token_without_exposing_it() -> (
    None
):
    exact_scope = _scope(allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,))

    class Pages:
        async def read(self, request: GmailReadRequest) -> GmailMessageIdPage:
            return GmailMessageIdPage(
                message_ids=("m1",), next_page_token="sensitive-repeated-token"
            )

    reader = compose_gmail_mailbox_reader(
        scopes=_Scopes(exact_scope),
        connections=_Connections(_connection()),
        client=Pages(),
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.ENUMERATE_MAILBOX,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
    )

    with pytest.raises(MailboxReadDeniedError) as error:
        await reader.read(command, _context())

    assert str(error.value) == "Mailbox read denied."
    assert "sensitive-repeated-token" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection_changes",
    (
        {"status": ProviderConnectionStatus.REVOKED},
        {"provider_kind": ProviderKind.YANDEX_MAIL},
        {"workspace_id": _id(99)},
        {"environment_id": _id(99)},
        {"connection_version_id": _id(99)},
        {"authorization_generation": 99},
        {"configuration_generation": 99},
        {
            "approved_resource_scope_references": (
                OpaqueProviderReference(value=_id(99)),
            )
        },
        {"capabilities": ()},
    ),
)
async def test_inactive_or_mismatched_connection_never_reaches_gmail(
    connection_changes: dict[str, object],
) -> None:
    reader, command, _, connections, client = _compose(
        connection=_connection(**connection_changes)
    )

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 1
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approved",
    (
        None,
        _scope(mailbox_subject_reference=_id(99)),
        _scope(resource_scope_reference=_id(99)),
        _scope(scope_generation=99),
        _scope(mailbox_binding_id=_id(99)),
    ),
)
async def test_missing_or_wrong_exact_mail_scope_never_reaches_gmail(
    approved: MailboxReadScope | None,
) -> None:
    reader, command, scopes, connections, client = _compose(approved=approved)
    if approved is None:
        scopes.approved = None

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 0
    assert client.requests == []


@pytest.mark.asyncio
async def test_drive_and_sheets_only_capabilities_never_reach_gmail() -> None:
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.BINARY_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(31)),
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.TABULAR_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    reader, command, _, connections, client = _compose(connection=connection)

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 1
    assert client.requests == []


@pytest.mark.asyncio
async def test_google_error_details_are_sanitized_and_tokens_are_not_in_request() -> (
    None
):
    reader, command, _, _, client = _compose()
    client.failure = RuntimeError(
        "refresh-token-value private@example.test customer subject.pdf"
    )

    with pytest.raises(MailboxReadDeniedError) as error:
        await reader.read(command, _context())

    assert str(error.value) == "Mailbox read denied."
    assert "refresh-token-value" not in str(error.value)
    assert "private@example.test" not in str(error.value)
    assert "subject.pdf" not in str(error.value)
    assert client.requests[0].credential_reference == OpaqueProviderReference(
        value=_id(24)
    )
    assert client.requests[0].grant_reference == OpaqueProviderReference(value=_id(25))


@pytest.mark.asyncio
async def test_admitted_gmail_history_pages_reach_client_with_same_exact_authority() -> (
    None
):
    scope = _scope(allowed_operations=(EmailProviderOperation.SYNCHRONIZE,))
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    scopes = _Scopes(scope)
    connections = _Connections(connection)
    message_id = "message-state-840"
    state_writer, state_repository = _state_writer(scope, message_id=message_id)
    client = _RecordingGmailHistoryClient(
        (
            {
                "history": [
                    {
                        "id": "102",
                        "labelsAdded": [
                            {"message": {"id": message_id}, "labelIds": ["INBOX"]}
                        ],
                    }
                ],
                "nextPageToken": "page-2",
            },
            {"history": [], "historyId": "103"},
        )
    )
    reader = compose_gmail_history_reader(
        scopes=scopes,
        connections=connections,
        client=client,
        state_writer=state_writer,
        clock=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )
    command = GmailHistoryReadCommand(
        scope=scope,
        operation=EmailOperation.SYNCHRONIZE,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        start_history_id="98",
        max_results=20,
        max_pages=2,
    )

    result = await reader.read(command, _context())

    assert result.state is GmailHistoryState.COMPLETE
    assert result.terminal_history_id == "103"
    assert result.admission_scope_digest == gmail_history_scope_digest(scope)
    assert connections.calls == 1
    assert len(client.requests) == 2
    assert [request.page_token for request in client.requests] == [None, "page-2"]
    assert all(
        request.operation is EmailOperation.SYNCHRONIZE for request in client.requests
    )
    assert all(request.start_history_id == "98" for request in client.requests)
    assert all(request.max_results == 20 for request in client.requests)
    assert all(request.connection_id == _id(3) for request in client.requests)
    assert all(request.mailbox_binding_id == _id(7) for request in client.requests)
    assert all(request.authorization_generation == 8 for request in client.requests)
    binding = await state_repository.resolve_current_binding(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        message_key=GmailMessageKey(
            connection_id=scope.connection_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            message_id=message_id,
        ),
    )
    assert binding is not None
    assert len(await state_repository.list_label_observations(binding)) == 1


@pytest.mark.asyncio
async def test_gmail_history_without_sync_capability_never_reaches_client() -> None:
    scope = _scope(allowed_operations=(EmailProviderOperation.SYNCHRONIZE,))
    scopes = _Scopes(scope)
    connections = _Connections(_connection())
    client = _RecordingGmailHistoryClient(({"history": [], "historyId": "103"},))
    state_writer, _ = _state_writer(scope)
    reader = compose_gmail_history_reader(
        scopes=scopes,
        connections=connections,
        client=client,
        state_writer=state_writer,
    )
    command = GmailHistoryReadCommand(
        scope=scope,
        operation=EmailOperation.SYNCHRONIZE,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        start_history_id="98",
        max_results=20,
        max_pages=2,
    )

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 1
    assert client.requests == []


@pytest.mark.asyncio
async def test_gmail_history_rejects_state_writer_for_another_mailbox_scope() -> None:
    scope = _scope(allowed_operations=(EmailProviderOperation.SYNCHRONIZE,))
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    foreign_scope = scope.model_copy(update={"mailbox_binding_id": _id(700)})
    state_writer, _ = _state_writer(foreign_scope)
    client = _RecordingGmailHistoryClient(({"history": [], "historyId": "103"},))
    reader = compose_gmail_history_reader(
        scopes=_Scopes(scope),
        connections=_Connections(connection),
        client=client,
        state_writer=state_writer,
    )
    command = GmailHistoryReadCommand(
        scope=scope,
        operation=EmailOperation.SYNCHRONIZE,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        start_history_id="98",
        max_results=20,
        max_pages=2,
    )

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert client.requests == []


@pytest.mark.asyncio
async def test_raw_message_returns_bytes_digest_and_provider_revision_evidence() -> (
    None
):
    reader, command, client = _compose_raw()
    payload = base64.urlsafe_b64decode(_raw_response().raw_data + "==")

    result = await reader.read(command, _context())

    assert result.status is GmailRawMessageStatus.ACCEPTED
    assert result.raw_mime == payload
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.byte_length == len(payload)
    assert result.history_id == "987654321"
    assert result.internal_date_ms == 1_800_000_000_000
    assert client.requests[0].message_id == command.message_id
    assert client.requests[0].format == "RAW"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"http_status": 404}, GmailRawMessageStatus.NOT_FOUND),
        ({"http_status": 401}, GmailRawMessageStatus.UNAUTHORIZED),
        ({"http_status": 403}, GmailRawMessageStatus.FORBIDDEN),
        ({"http_status": 429}, GmailRawMessageStatus.THROTTLED),
        ({"http_status": 503}, GmailRawMessageStatus.PROVIDER_UNAVAILABLE),
        ({"raw_data": "%%%"}, GmailRawMessageStatus.MALFORMED_ENCODING),
        ({"raw_data": "Y"}, GmailRawMessageStatus.TRUNCATED_DATA),
        ({"message_id": "other"}, GmailRawMessageStatus.MESSAGE_ID_MISMATCH),
        ({"label_references": (_id(99),)}, GmailRawMessageStatus.OUT_OF_SCOPE),
    ),
)
async def test_raw_message_maps_invalid_provider_results_to_safe_outcomes(
    changes: dict[str, object], expected: GmailRawMessageStatus
) -> None:
    reader, command, _ = _compose_raw(response=_raw_response(**changes))

    result = await reader.read(command, _context())

    assert result.status is expected
    assert result.raw_mime is None
    assert result.sha256 is None
    assert result.byte_length is None
    assert "Synthetic body" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    (
        (401, GmailRawMessageStatus.UNAUTHORIZED),
        (403, GmailRawMessageStatus.FORBIDDEN),
        (429, GmailRawMessageStatus.THROTTLED),
        (503, GmailRawMessageStatus.PROVIDER_UNAVAILABLE),
    ),
)
async def test_raw_message_sanitizes_provider_exceptions(
    status_code: int, expected: GmailRawMessageStatus
) -> None:
    reader, command, client = _compose_raw()
    client.failure = _HttpFailure(status_code)

    result = await reader.read(command, _context())

    assert result.status is expected
    assert "provider payload" not in str(result)
    assert "subject@example.test" not in str(result)


@pytest.mark.asyncio
async def test_raw_message_enforces_byte_bound_and_rejects_revoked_connection() -> None:
    reader, command, _ = _compose_raw(max_raw_message_bytes=2)

    result = await reader.read(command, _context())

    assert result.status is GmailRawMessageStatus.TOO_LARGE
    assert result.raw_mime is None

    reader, command, client = _compose_raw(
        connection=_connection(status=ProviderConnectionStatus.REVOKED)
    )
    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())
    assert client.requests == []


@pytest.mark.asyncio
async def test_raw_message_does_not_fetch_without_a_size_bound_or_exact_id() -> None:
    reader, command, client = _compose_raw(max_raw_message_bytes=None)

    result = await reader.read(command, _context())

    assert result.status is GmailRawMessageStatus.LIMIT_NOT_CONFIGURED
    assert client.requests == []

    reader, command, client = _compose_raw()
    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command.model_copy(update={"message_id": None}), _context())
    assert client.requests == []
