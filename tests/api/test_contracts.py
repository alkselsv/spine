from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.api.schemas import (
    API_SCHEMA_VERSION,
    ApiCollectionEnvelope,
    ApiCommandResult,
    ApiResponseEnvelope,
    CollectionQuery,
    CommandStatus,
    CursorCodec,
    IdempotencyKey,
    InvalidCursor,
    canonical_filter_digest,
    parse_idempotency_key_header,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000001")
COMMAND_ID = UUID("60000000-0000-0000-0000-000000000001")


def test_response_envelopes_are_versioned_scoped_closed_and_immutable() -> None:
    response = ApiResponseEnvelope[str](
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        trace_id=TRACE_ID,
        data="safe result",
    )

    assert response.schema_version == API_SCHEMA_VERSION
    assert response.model_dump() == {
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "trace_id": TRACE_ID,
        "data": "safe result",
    }
    with pytest.raises(ValidationError):
        ApiResponseEnvelope[str](
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            trace_id=TRACE_ID,
            data="safe result",
            unexpected="must be rejected",
        )
    with pytest.raises(ValidationError):
        response.data = "changed"


def test_collection_query_rejects_unbounded_limits_and_unknown_filters() -> None:
    assert CollectionQuery().limit == 50
    assert CollectionQuery(limit=100).limit == 100

    with pytest.raises(ValidationError):
        CollectionQuery(limit=101)
    with pytest.raises(ValidationError):
        CollectionQuery(limit=0)
    with pytest.raises(ValidationError):
        CollectionQuery(unexpected_filter="not accepted")


def test_cursor_is_bound_to_scope_operation_limit_and_filters() -> None:
    codec = CursorCodec(secret=b"contract-test-secret")
    filters = {"status": "stale", "owner": None}
    digest = canonical_filter_digest(filters)
    cursor = codec.encode(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        operation_id="document.list",
        limit=25,
        filters_digest=digest,
        position="document-000025",
    )

    assert (
        codec.decode(
            cursor,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            operation_id="document.list",
            limit=25,
            filters_digest=digest,
        ).position
        == "document-000025"
    )
    assert canonical_filter_digest({"owner": None, "status": "stale"}) == digest

    for kwargs in (
        {"workspace_id": UUID("20000000-0000-0000-0000-000000000002")},
        {"environment_id": UUID("30000000-0000-0000-0000-000000000002")},
        {"operation_id": "document.history"},
        {"limit": 50},
        {"filters_digest": canonical_filter_digest({"status": "current"})},
    ):
        expected = {
            "workspace_id": WORKSPACE_ID,
            "environment_id": ENVIRONMENT_ID,
            "operation_id": "document.list",
            "limit": 25,
            "filters_digest": digest,
        }
        expected.update(kwargs)
        with pytest.raises(InvalidCursor):
            codec.decode(cursor, **expected)


def test_tampered_cursor_fails_without_disclosing_cursor_payload() -> None:
    codec = CursorCodec(secret=b"contract-test-secret")
    cursor = codec.encode(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        operation_id="document.list",
        limit=25,
        filters_digest=canonical_filter_digest({}),
        position="protected-document-id",
    )
    tampered = f"{cursor[:-1]}{'a' if cursor[-1] != 'a' else 'b'}"

    with pytest.raises(InvalidCursor) as failure:
        codec.decode(
            tampered,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            operation_id="document.list",
            limit=25,
            filters_digest=canonical_filter_digest({}),
        )
    assert "protected-document-id" not in str(failure.value)


def test_idempotency_key_is_bounded_and_rejects_ambiguous_values() -> None:
    key = IdempotencyKey(value="upload-001")
    assert key.value == "upload-001"
    with pytest.raises(ValidationError):
        IdempotencyKey(value="")
    with pytest.raises(ValidationError):
        IdempotencyKey(value=" leading-space")
    with pytest.raises(ValidationError):
        IdempotencyKey(value="x\nretry")
    with pytest.raises(ValidationError):
        IdempotencyKey(value="x" * 256)
    assert parse_idempotency_key_header(("upload-001",)).value == "upload-001"
    with pytest.raises(ValueError):
        parse_idempotency_key_header(("first", "second"))


def test_command_result_carries_typed_identity_scope_trace_and_replay() -> None:
    result = ApiCommandResult[dict[str, str]](
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        trace_id=TRACE_ID,
        command_id=COMMAND_ID,
        status=CommandStatus.COMPLETED,
        replay=True,
        result={"run_id": "run-001"},
    )

    assert result.replay is True
    assert result.result == {"run_id": "run-001"}
    assert (
        ApiCollectionEnvelope[str](
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            trace_id=TRACE_ID,
            items=("one",),
            limit=50,
        ).next_cursor
        is None
    )
