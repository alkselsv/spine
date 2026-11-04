"""Opaque, versioned and generation-bound Gmail synchronization checkpoints."""

from __future__ import annotations

import base64
import json
from typing import Literal

from pydantic import Field

from spine.connectors.gmail_history import GmailHistoryId, gmail_history_scope_digest
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import MailboxReadScope


class _GmailCheckpoint(DefinitionModel):
    version: Literal[1] = 1
    kind: Literal["history", "snapshot_page"]
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: str = Field(min_length=1, max_length=4096)


def encode_gmail_history_checkpoint(
    scope: MailboxReadScope, history_id: GmailHistoryId
) -> str:
    return _encode("history", scope, history_id)


def decode_gmail_history_checkpoint(
    scope: MailboxReadScope, checkpoint: str
) -> GmailHistoryId:
    return _decode("history", scope, checkpoint)


def encode_gmail_snapshot_page_checkpoint(
    scope: MailboxReadScope, page_token: str
) -> str:
    return _encode("snapshot_page", scope, page_token)


def decode_gmail_snapshot_page_checkpoint(
    scope: MailboxReadScope, checkpoint: str
) -> str:
    return _decode("snapshot_page", scope, checkpoint)


def _encode(
    kind: Literal["history", "snapshot_page"],
    scope: MailboxReadScope,
    value: str,
) -> str:
    envelope = _GmailCheckpoint(
        kind=kind,
        scope_digest=gmail_history_scope_digest(scope),
        value=value,
    )
    payload = json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    checkpoint = f"gmail-checkpoint:v1:{encoded}"
    if len(checkpoint) > 4096:
        raise ValueError("Gmail checkpoint exceeds the durable bound")
    return checkpoint


def _decode(
    expected_kind: Literal["history", "snapshot_page"],
    scope: MailboxReadScope,
    checkpoint: str,
) -> str:
    try:
        prefix, version, encoded = checkpoint.split(":", 2)
        if prefix != "gmail-checkpoint" or version != "v1" or not encoded:
            raise ValueError
        payload = base64.b64decode(
            encoded.encode("ascii") + b"=" * ((-len(encoded)) % 4),
            altchars=b"-_",
            validate=True,
        )
        envelope = _GmailCheckpoint.model_validate_json(payload)
    except Exception:
        raise ValueError("Gmail checkpoint is invalid") from None
    if (
        envelope.kind != expected_kind
        or envelope.scope_digest != gmail_history_scope_digest(scope)
    ):
        raise ValueError("Gmail checkpoint is outside the admitted generation")
    return envelope.value


__all__ = [
    "decode_gmail_history_checkpoint",
    "decode_gmail_snapshot_page_checkpoint",
    "encode_gmail_history_checkpoint",
    "encode_gmail_snapshot_page_checkpoint",
]
