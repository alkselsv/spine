from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest

from spine.application.persistence.command_digest import (
    COMMAND_DIGEST_ALGORITHM_VERSION,
    CommandDigest,
    UnsupportedCommandValueError,
    canonical_command_bytes,
    digest_command,
)
from spine.application.persistence.context import PersistenceOperation


class ExampleStatus(str, Enum):
    READY = "ready"
    PAUSED = "paused"


class RenamedExampleStatus(str, Enum):
    READY = "ready"


OPERATION = PersistenceOperation("proposal.generate")
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


def test_equivalent_command_payloads_produce_same_digest() -> None:
    first = {
        "workspace_id": WORKSPACE_ID,
        "amount": Decimal("12.3400"),
        "tags": ["alpha", "beta"],
        "requested_at": datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc),
    }
    second = {
        "requested_at": datetime(
            2026,
            10,
            9,
            15,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        ),
        "tags": ["alpha", "beta"],
        "amount": Decimal("12.34"),
        "workspace_id": UUID("10000000-0000-0000-0000-000000000001"),
    }

    assert (
        digest_command(
            operation=OPERATION,
            operation_schema_version=1,
            payload=first,
        )
        == digest_command(
            operation=OPERATION,
            operation_schema_version=1,
            payload=second,
        )
    )


def test_meaningful_command_content_changes_digest() -> None:
    first = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"items": ["a", "b"]},
    )
    second = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"items": ["b", "a"]},
    )

    assert first != second


def test_canonical_command_bytes_are_versioned_and_deterministic() -> None:
    canonical = canonical_command_bytes(
        operation=OPERATION,
        operation_schema_version=1,
        payload={
            "status": ExampleStatus.READY,
            "workspace_id": WORKSPACE_ID,
            "amount": Decimal("1.2300"),
            "enabled": True,
            "count": 3,
            "missing": None,
            "requested_at": datetime(2026, 10, 9, 12, 30, 45, 123456, timezone.utc),
        },
    )

    assert canonical.decode("utf-8") == (
        '{"canonical_version":"spine.canonical-command.v1",'
        '"operation":"proposal.generate","operation_schema_version":1,'
        '"payload":{"type":"object","value":{'
        '"amount":{"type":"decimal","value":"1.23"},'
        '"count":{"type":"integer","value":"3"},'
        '"enabled":{"type":"bool","value":true},'
        '"missing":{"type":"null","value":null},'
        '"requested_at":{"precision":"microseconds","type":"datetime",'
        '"value":"2026-10-09T12:30:45.123456Z"},'
        '"status":{"type":"enum","value":"ready"},'
        '"workspace_id":{"type":"uuid","value":"10000000-0000-0000-0000-000000000001"}'
        "}}}"
    )


@pytest.mark.parametrize(
    ("payload", "expected_digest"),
    [
        (
            {
                "workspace_id": WORKSPACE_ID,
                "status": ExampleStatus.READY,
                "requested_at": datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc),
                "amount": Decimal("12.3400"),
                "lines": [
                    {"sku": "A-1", "quantity": 2},
                    {"sku": "B-2", "quantity": 1},
                ],
            },
            "b2bb4c9eb3cad3cf0048d916fe645107ca5c01edbb98537fb25d6cdf424e1c0e",
        ),
        (
            {
                "unicode": "Cafe\u0301 / \u041f\u0440\u0438\u0432\u0435\u0442",
                "zero": Decimal("-0.000"),
                "flag": False,
                "nested": {"b": 2, "a": 1},
            },
            "1c812fe4b9ecaa1dad183683c3a2838c19cefccb32a102d5fa7d6affef0632bf",
        ),
    ],
)
def test_golden_digest_vectors_are_stable(payload: dict[str, object], expected_digest: str) -> None:
    digest = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload=payload,
    )

    assert digest.algorithm_version == COMMAND_DIGEST_ALGORITHM_VERSION
    assert digest.value == expected_digest


def test_enum_class_name_does_not_affect_digest() -> None:
    first = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"status": ExampleStatus.READY},
    )
    second = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"status": RenamedExampleStatus.READY},
    )

    assert first == second


def test_unicode_strings_are_normalized_to_nfc() -> None:
    composed = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"unicode": "Caf\u00e9"},
    )
    decomposed = digest_command(
        operation=OPERATION,
        operation_schema_version=1,
        payload={"unicode": "Cafe\u0301"},
    )

    assert composed == decomposed


def test_command_digest_accepts_current_version_and_rejects_future_or_malformed_versions() -> None:
    digest = "0" * 64

    assert CommandDigest("spine.command-digest.v1", digest).value == digest

    with pytest.raises(UnsupportedCommandValueError):
        CommandDigest("spine.command-digest.v2", digest)
    with pytest.raises(UnsupportedCommandValueError):
        CommandDigest("spine.command-digest.latest", digest)


@pytest.mark.parametrize(
    "payload",
    [
        {"float": 1.0},
        {"nan": Decimal("NaN")},
        {"infinity": Decimal("Infinity")},
        {"set": {"a", "b"}},
        {1: "not a string key"},
        {"bytes": b"raw"},
        {"timestamp": datetime(2026, 10, 9, 12, 30)},
    ],
)
def test_unsupported_command_values_are_rejected(payload: dict[object, object]) -> None:
    with pytest.raises(UnsupportedCommandValueError):
        digest_command(
            operation=OPERATION,
            operation_schema_version=1,
            payload=payload,
        )
