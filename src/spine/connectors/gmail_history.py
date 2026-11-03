"""Normalize bounded Gmail history reads into safe lifecycle evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Protocol

from pydantic import Field, field_validator

from spine.domain.common import DefinitionModel


class GmailHistoryState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class GmailHistoryChangeKind(str, Enum):
    MESSAGE_ADDED = "message_added"
    MESSAGE_DELETED = "message_deleted"
    LABEL_ADDED = "label_added"
    LABEL_REMOVED = "label_removed"


class GmailHistoryPageRequest(DefinitionModel):
    """One bounded request for a page in the same history interval."""

    start_history_id: str
    page_token: str | None = None
    max_results: int = Field(gt=0)

    @field_validator("start_history_id")
    @classmethod
    def _validate_history_id(cls, value: str) -> str:
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Gmail history IDs must be decimal strings")
        return value


class GmailHistoryChange(DefinitionModel):
    """One message or label observation tied to its provider history record."""

    history_id: str
    kind: GmailHistoryChangeKind
    message_id: str
    label_id: str | None = None

    @property
    def event_id(self) -> str:
        """Return a deterministic identity for one normalized history effect."""

        return json.dumps(
            [self.history_id, self.kind.value, self.message_id, self.label_id],
            ensure_ascii=True,
            separators=(",", ":"),
        )


class GmailHistoryResult(DefinitionModel):
    """Safe outcome for a bounded history drain, including usable partial data."""

    state: GmailHistoryState
    changes: tuple[GmailHistoryChange, ...] = ()
    terminal_history_id: str | None = None
    pages_read: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    malformed_count: int = Field(ge=0)
    error_code: str | None = None


class GmailHistoryProviderError(Exception):
    """Provider failure carrying only an HTTP status code, never response text."""

    def __init__(self, status_code: int) -> None:
        super().__init__("Gmail history provider request failed.")
        self.status_code = status_code


class GmailHistoryPageSource(Protocol):
    async def list_history(
        self, request: GmailHistoryPageRequest
    ) -> Mapping[str, object]: ...


class GmailHistoryReader:
    """Drain and normalize every page within the caller's explicit page bound."""

    def __init__(self, source: GmailHistoryPageSource, *, max_pages: int) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._source = source
        self._max_pages = max_pages

    async def read(self, *, start_history_id: str, max_results: int) -> GmailHistoryResult:
        request = GmailHistoryPageRequest(
            start_history_id=start_history_id,
            max_results=max_results,
        )
        events: dict[tuple[str, str, str, str | None], GmailHistoryChange] = {}
        duplicate_count = 0
        malformed_count = 0
        page_token: str | None = None
        seen_tokens: set[str] = set()
        pages_read = 0
        terminal_history_id: str | None = None
        state = GmailHistoryState.COMPLETE
        error_code: str | None = None

        while pages_read < self._max_pages:
            request = request.model_copy(update={"page_token": page_token})
            try:
                page = await self._source.list_history(request)
            except GmailHistoryProviderError as error:
                state, error_code = _provider_failure(error.status_code)
                break
            except Exception:
                state, error_code = GmailHistoryState.INCOMPLETE, "provider_error"
                break

            pages_read += 1
            if not isinstance(page, Mapping):
                malformed_count += 1
                state, error_code = GmailHistoryState.INCOMPLETE, "malformed_page"
                break

            raw_history = page.get("history", [])
            if not isinstance(raw_history, list):
                malformed_count += 1
                state, error_code = GmailHistoryState.INCOMPLETE, "malformed_page"
                break
            if len(raw_history) > request.max_results:
                malformed_count += 1
                state, error_code = (
                    GmailHistoryState.INCOMPLETE,
                    "page_item_limit_exceeded",
                )
                break
            for record in raw_history:
                parsed, malformed = _normalize_record(record)
                malformed_count += int(malformed)
                if malformed:
                    state, error_code = (
                        GmailHistoryState.INCOMPLETE,
                        "malformed_history_record",
                    )
                for change in parsed:
                    key = (
                        change.history_id,
                        change.kind.value,
                        change.message_id,
                        change.label_id,
                    )
                    if key in events:
                        duplicate_count += 1
                    else:
                        events[key] = change

            next_page_token = page.get("nextPageToken")
            if next_page_token is not None:
                if not isinstance(next_page_token, str) or not next_page_token:
                    malformed_count += 1
                    state, error_code = GmailHistoryState.INCOMPLETE, "malformed_page"
                    break
                if next_page_token in seen_tokens:
                    state, error_code = GmailHistoryState.INCOMPLETE, "pagination_cycle"
                    break
                seen_tokens.add(next_page_token)
                page_token = next_page_token
                if pages_read == self._max_pages:
                    state, error_code = GmailHistoryState.INCOMPLETE, "page_limit_reached"
                    break
                continue

            raw_terminal_id = page.get("historyId")
            if _valid_history_id(raw_terminal_id):
                terminal_history_id = raw_terminal_id
            else:
                state, error_code = GmailHistoryState.INCOMPLETE, "missing_terminal_history_id"
            break

        normalized = tuple(
            sorted(
                events.values(),
                key=lambda change: (
                    int(change.history_id),
                    change.kind.value,
                    change.message_id,
                    change.label_id or "",
                ),
            )
        )
        return GmailHistoryResult(
            state=state,
            changes=normalized,
            terminal_history_id=terminal_history_id,
            pages_read=pages_read,
            duplicate_count=duplicate_count,
            malformed_count=malformed_count,
            error_code=error_code,
        )


def _provider_failure(status_code: int) -> tuple[GmailHistoryState, str]:
    if status_code == 404:
        return GmailHistoryState.RECONCILIATION_REQUIRED, "history_cursor_stale"
    if status_code == 429:
        return GmailHistoryState.INCOMPLETE, "provider_rate_limited"
    if status_code >= 500:
        return GmailHistoryState.INCOMPLETE, "provider_unavailable"
    return GmailHistoryState.INCOMPLETE, "provider_rejected_request"


def _normalize_record(
    record: object,
) -> tuple[tuple[GmailHistoryChange, ...], bool]:
    if not isinstance(record, Mapping) or not _valid_history_id(record.get("id")):
        return (), True
    history_id = record["id"]
    changes: list[GmailHistoryChange] = []
    malformed = False
    shapes = (
        ("messagesAdded", GmailHistoryChangeKind.MESSAGE_ADDED, False),
        ("messagesDeleted", GmailHistoryChangeKind.MESSAGE_DELETED, False),
        ("labelsAdded", GmailHistoryChangeKind.LABEL_ADDED, True),
        ("labelsRemoved", GmailHistoryChangeKind.LABEL_REMOVED, True),
    )
    for field_name, kind, has_labels in shapes:
        raw_changes = record.get(field_name, [])
        if not isinstance(raw_changes, list):
            malformed = True
            continue
        for raw_change in raw_changes:
            if not isinstance(raw_change, Mapping):
                malformed = True
                continue
            message = raw_change.get("message")
            if not isinstance(message, Mapping) or not _valid_identifier(
                message.get("id")
            ):
                malformed = True
                continue
            message_id = message["id"]
            if not has_labels:
                changes.append(
                    GmailHistoryChange(
                        history_id=history_id,
                        kind=kind,
                        message_id=message_id,
                    )
                )
                continue
            label_ids = raw_change.get("labelIds")
            if not isinstance(label_ids, list) or any(
                not _valid_identifier(label_id) for label_id in label_ids
            ):
                malformed = True
                continue
            for label_id in label_ids:
                changes.append(
                    GmailHistoryChange(
                        history_id=history_id,
                        kind=kind,
                        message_id=message_id,
                        label_id=label_id,
                    )
                )
    return tuple(changes), malformed


def _valid_history_id(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value.isascii()


__all__ = [
    "GmailHistoryChange",
    "GmailHistoryChangeKind",
    "GmailHistoryPageRequest",
    "GmailHistoryProviderError",
    "GmailHistoryReader",
    "GmailHistoryResult",
    "GmailHistoryState",
]
