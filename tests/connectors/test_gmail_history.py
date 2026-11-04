from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.connectors.gmail_history import (
    GmailHistoryChange,
    GmailHistoryChangeKind,
    GmailHistoryPageRequest,
    GmailHistoryProviderError,
    GmailHistoryReader,
    GmailHistoryState,
    gmail_history_scope_digest,
)
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)


def _scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=UUID("10000000-0000-0000-0000-000000000001"),
        environment_id=UUID("20000000-0000-0000-0000-000000000002"),
        connection_id=UUID("30000000-0000-0000-0000-000000000003"),
        connection_version_id=UUID("40000000-0000-0000-0000-000000000004"),
        provider_kind="google",
        resource_scope_reference=UUID("50000000-0000-0000-0000-000000000005"),
        mailbox_subject_reference=UUID("60000000-0000-0000-0000-000000000006"),
        mailbox_binding_id=UUID("70000000-0000-0000-0000-000000000007"),
        authorization_generation=3,
        configuration_generation=4,
        scope_generation=5,
        allowed_operations=(EmailProviderOperation.SYNCHRONIZE,),
    )


class _HistoryPages:
    def __init__(self, pages: tuple[Mapping[str, object], ...]) -> None:
        self.pages = pages
        self.requests: list[GmailHistoryPageRequest] = []

    async def list_history(
        self, request: GmailHistoryPageRequest
    ) -> Mapping[str, object]:
        self.requests.append(request)
        return self.pages[len(self.requests) - 1]


class _FailingHistoryPages:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.requests: list[GmailHistoryPageRequest] = []

    async def list_history(
        self, request: GmailHistoryPageRequest
    ) -> Mapping[str, object]:
        self.requests.append(request)
        raise self.failure


@pytest.mark.asyncio
async def test_history_reader_normalizes_each_change_kind_across_pages() -> None:
    provider = _HistoryPages(
        (
            {
                "history": [
                    {
                        "id": "102",
                        "labelsRemoved": [
                            {"message": {"id": "message-c"}, "labelIds": ["INBOX"]}
                        ],
                    },
                    {
                        "id": "100",
                        "messagesAdded": [{"message": {"id": "message-b"}}],
                    },
                ],
                "nextPageToken": "page-2",
            },
            {
                "history": [
                    {
                        "id": "99",
                        "labelsAdded": [
                            {
                                "message": {"id": "message-a"},
                                "labelIds": ["STARRED", "INBOX"],
                            }
                        ],
                    },
                    {
                        "id": "101",
                        "messagesDeleted": [{"message": {"id": "message-d"}}],
                    },
                    {
                        "id": "99",
                        "labelsAdded": [
                            {
                                "message": {"id": "message-a"},
                                "labelIds": ["INBOX", "STARRED"],
                            }
                        ],
                    },
                ],
                "historyId": "103",
            },
        )
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=2).read(
        start_history_id="98", max_results=25
    )

    assert result.state is GmailHistoryState.COMPLETE
    assert result.schema_version == "gmail-history-result:v1"
    assert result.admission_scope_digest == gmail_history_scope_digest(_scope())
    assert result.terminal_history_id == "103"
    assert result.pages_read == 2
    assert result.duplicate_count == 2
    assert [
        (change.history_id, change.kind.value, change.message_id, change.label_id)
        for change in result.changes
    ] == [
        ("99", "label_added", "message-a", "INBOX"),
        ("99", "label_added", "message-a", "STARRED"),
        ("100", "message_added", "message-b", None),
        ("101", "message_deleted", "message-d", None),
        ("102", "label_removed", "message-c", "INBOX"),
    ]
    assert [request.page_token for request in provider.requests] == [None, "page-2"]
    assert all(request.start_history_id == "98" for request in provider.requests)
    assert all(request.max_results == 25 for request in provider.requests)
    assert all(
        request.schema_version == "gmail-history-page-request:v1"
        for request in provider.requests
    )
    assert result.changes[0].event_id == '["99","label_added","message-a","INBOX"]'


def test_history_reader_rejects_missing_sync_authority_before_provider_call() -> None:
    provider = _HistoryPages(({"history": [], "historyId": "103"},))
    unauthorized_scope = _scope().model_copy(
        update={"allowed_operations": (EmailProviderOperation.READ_MESSAGE_METADATA,)}
    )

    with pytest.raises(ValueError, match="synchronization authority"):
        GmailHistoryReader(provider, scope=unauthorized_scope, max_pages=1)

    assert provider.requests == []


@pytest.mark.asyncio
async def test_malformed_history_record_keeps_safe_valid_events_as_incomplete() -> None:
    provider = _HistoryPages(
        (
            {
                "history": [
                    {
                        "id": "100",
                        "messagesAdded": [{"message": {"id": "message-safe"}}],
                    },
                    {
                        "id": "101",
                        "messagesDeleted": [
                            {
                                "message": {
                                    "id": "",
                                    "subject": "private provider payload",
                                    "from": "sender@example.invalid",
                                }
                            }
                        ],
                    },
                ],
                "historyId": "102",
            },
        )
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "malformed_history_record"
    assert result.malformed_count == 1
    assert result.terminal_history_id == "102"
    assert [(change.kind.value, change.message_id) for change in result.changes] == [
        ("message_added", "message-safe")
    ]
    assert "private provider payload" not in repr(result)
    assert "@example.invalid" not in repr(result)


@pytest.mark.asyncio
async def test_missing_terminal_history_id_returns_incomplete_outcome() -> None:
    provider = _HistoryPages(({"history": []},))

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.terminal_history_id is None
    assert result.error_code == "missing_terminal_history_id"


@pytest.mark.asyncio
async def test_malformed_page_returns_safe_incomplete_outcome() -> None:
    provider = _HistoryPages(
        ({"history": "raw provider token: sensitive-value", "historyId": "100"},)
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "malformed_page"
    assert result.malformed_count == 1
    assert "sensitive-value" not in repr(result)


@pytest.mark.asyncio
async def test_history_reader_stops_at_explicit_page_bound() -> None:
    provider = _HistoryPages(
        (
            {
                "history": [],
                "nextPageToken": "page-2",
            },
        )
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "page_limit_reached"
    assert result.pages_read == 1
    assert result.terminal_history_id is None
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_history_reader_rejects_page_larger_than_requested_bound() -> None:
    provider = _HistoryPages(
        (
            {
                "history": [
                    {"id": "100", "messagesAdded": [{"message": {"id": "one"}}]},
                    {"id": "101", "messagesAdded": [{"message": {"id": "two"}}]},
                ],
                "historyId": "102",
            },
        )
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=1
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "page_item_limit_exceeded"
    assert result.malformed_count == 1
    assert result.changes == ()


@pytest.mark.asyncio
async def test_history_reader_detects_repeated_page_token() -> None:
    provider = _HistoryPages(
        (
            {"history": [], "nextPageToken": "repeat"},
            {"history": [], "nextPageToken": "repeat"},
        )
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=3).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "pagination_cycle"
    assert result.pages_read == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "state", "error_code"),
    (
        (404, GmailHistoryState.RECONCILIATION_REQUIRED, "history_cursor_stale"),
        (429, GmailHistoryState.INCOMPLETE, "provider_rate_limited"),
        (503, GmailHistoryState.INCOMPLETE, "provider_unavailable"),
        (403, GmailHistoryState.INCOMPLETE, "provider_rejected_request"),
    ),
)
async def test_provider_errors_return_typed_safe_history_outcomes(
    status_code: int,
    state: GmailHistoryState,
    error_code: str,
) -> None:
    provider = _FailingHistoryPages(GmailHistoryProviderError(status_code))

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=3).read(
        start_history_id="99", max_results=10
    )

    assert result.state is state
    assert result.error_code == error_code
    assert result.pages_read == 0
    assert repr(status_code) not in repr(result)


@pytest.mark.asyncio
async def test_untyped_provider_error_does_not_disclose_exception_text() -> None:
    provider = _FailingHistoryPages(
        RuntimeError("access_token=secret and raw payload private provider message")
    )

    result = await GmailHistoryReader(provider, scope=_scope(), max_pages=1).read(
        start_history_id="99", max_results=10
    )

    assert result.state is GmailHistoryState.INCOMPLETE
    assert result.error_code == "provider_error"
    assert "secret" not in repr(result)
    assert "private provider message" not in repr(result)


def test_history_contracts_reject_invalid_provider_history_ids() -> None:
    with pytest.raises(ValidationError):
        GmailHistoryChange(
            history_id="not-a-cursor",
            kind=GmailHistoryChangeKind.MESSAGE_DELETED,
            message_id="synthetic-message",
        )
