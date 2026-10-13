from __future__ import annotations

from spine.memory.contracts import ProjectionChunk, ProjectionOutcome

from .adapter import ContextProjectionFactory
from .test_conformance import (
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    evidence,
    retrieve_command,
)


async def test_denied_scope_has_no_content_or_sensitive_diagnostic(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    secret = "workspace A confidential text"
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(ProjectionChunk(chunk_id="private", text=secret, evidence=(evidence(),)),),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(
            identity="denied-scope",
            digest="d" * 64,
            workspace_id=OTHER_WORKSPACE_ID,
            environment_id=OTHER_ENVIRONMENT_ID,
        )
    )

    assert result.outcome is ProjectionOutcome.MISSING
    assert result.chunks == ()
    assert result.entities == ()
    assert result.relations == ()
    assert result.failure is not None
    assert result.failure.safe_detail.excerpt is None
    assert secret not in result.model_dump_json()
