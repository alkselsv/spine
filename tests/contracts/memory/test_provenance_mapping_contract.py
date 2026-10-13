from __future__ import annotations

from spine.memory.contracts import ProjectionChunk, ProjectionEntity, ProjectionRelation

from .adapter import ContextProjectionFactory
from .test_conformance import ENVIRONMENT_ID, WORKSPACE_ID, evidence, retrieve_command


async def test_provenance_mapping_preserves_source_revision_and_locator(
    context_projection_factory: ContextProjectionFactory,
) -> None:
    adapter = context_projection_factory()
    reference = evidence()
    adapter.seed_retrieval(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        chunks=(ProjectionChunk(chunk_id="chunk", text="grounded", evidence=(reference,)),),
        entities=(ProjectionEntity(entity_id="entity", kind="client", name="Acme", evidence=(reference,)),),
        relations=(
            ProjectionRelation(
                relation_id="relation",
                kind="owns",
                subject_entity_id="entity",
                object_entity_id="other",
                evidence=(reference,),
            ),
        ),
    )

    result = await adapter.projection.retrieve(
        retrieve_command(identity="provenance", digest="8" * 64)
    )

    assert result.evidence
    assert all(item == reference for item in result.evidence)
    assert all(item.workspace_id == WORKSPACE_ID for item in result.evidence)
    assert all(item.environment_id == ENVIRONMENT_ID for item in result.evidence)
    assert all(item.source_revision_id == reference.source_revision_id for item in result.evidence)
    assert all(item.locator == reference.locator for item in result.evidence)
