from __future__ import annotations

from spine.memory.contracts import (
    ProjectionEffect,
    ProjectionEffectStatus,
    ProjectionOutcome,
    ProjectionRetrievalResult,
)
from spine.memory.cognee.context_projection import (
    CogneeContextProjection,
    CogneeGraphEffectStore,
    create_cognee_context_projection,
)
from spine.memory.cognee.graph import GraphProjectionResult, GraphProjectionStatus
from spine.memory.fake import DeterministicPhysicalStore

from tests.contracts.memory.test_conformance import (
    ENVIRONMENT_ID,
    WORKSPACE_ID,
    project_command,
    retrieve_command,
)


class _Retrieval:
    async def retrieve(self, command):
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.MISSING,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
        )


async def test_context_projection_composes_physical_write_and_structured_read() -> None:
    store = DeterministicPhysicalStore()
    projection = CogneeContextProjection(effect_store=store, retrieval=_Retrieval())

    written = await projection.project(project_command())
    retrieved = await projection.retrieve(retrieve_command())

    assert written.outcome is ProjectionOutcome.SUCCEEDED
    assert written.effects[0].status is ProjectionEffectStatus.APPLIED
    assert (
        written.effects[0].processing_receipt_id
        == project_command().source_revisions[0].processing_receipt_id
    )
    assert written.effects[0].locator_contract_version == "locator:v1"
    assert retrieved.outcome is ProjectionOutcome.MISSING


class _GraphLane:
    def __init__(self) -> None:
        self.effects: list[ProjectionEffect] = []

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect:
        self.effects.append(effect)
        return effect.model_copy(update={"status": ProjectionEffectStatus.APPLIED})


async def test_context_projection_applies_every_configured_physical_lane() -> None:
    chunks = _GraphLane()
    graph = _GraphLane()
    projection = CogneeContextProjection(
        effect_store=chunks,
        graph_effect_store=graph,
        retrieval=_Retrieval(),
    )

    result = await projection.project(project_command())

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert len(chunks.effects) == 1
    assert len(graph.effects) == 1


async def test_context_projection_retries_only_failed_lane() -> None:
    chunks = _GraphLane()
    graph = DeterministicPhysicalStore(fail_once_for={"recoverable:1"})
    projection = CogneeContextProjection(
        effect_store=chunks,
        graph_effect_store=graph,
        retrieval=_Retrieval(),
    )
    command = project_command(identity="recoverable")

    first = await projection.project(command)
    recovered = await projection.project(command)

    assert first.outcome is ProjectionOutcome.FAILED
    assert recovered.outcome is ProjectionOutcome.SUCCEEDED
    assert len(chunks.effects) == 1


class _GraphWriter:
    def __init__(self, result) -> None:
        self.result = result
        self.requests = []

    async def write(self, request):
        self.requests.append(request)
        return GraphProjectionResult(
            operation=request.operation,
            status=self.result,
            command_identity=request.command_identity,
            command_digest=request.command_digest,
            failure_code=(
                "physical_failure"
                if self.result is GraphProjectionStatus.RECOVERABLE
                else None
            ),
            pending_artifact_ids=(
                ("graph-command",)
                if self.result is GraphProjectionStatus.RECOVERABLE
                else ()
            ),
        )


def _extraction_for_effect(effect: ProjectionEffect):
    from tests.memory.cognee.test_graph import extraction_result

    extraction = extraction_result(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )
    revision_update = {
        "source_object_id": effect.source_object_id,
        "source_revision_id": effect.source_revision_id,
        "revision_digest": effect.revision_digest,
        "revision_number": effect.revision_number,
        "status": "current",
    }
    fact_update = {"source_revision_id": effect.source_revision_id}
    return extraction.model_copy(
        update={
            "source_revisions": (
                extraction.source_revisions[0].model_copy(update=revision_update),
            ),
            "entities": tuple(
                entity.model_copy(
                    update={
                        **fact_update,
                        "evidence": tuple(
                            reference.model_copy(update=fact_update)
                            for reference in entity.evidence
                        ),
                    }
                )
                for entity in extraction.entities
            ),
            "relations": tuple(
                relation.model_copy(
                    update={
                        **fact_update,
                        "evidence": tuple(
                            reference.model_copy(update=fact_update)
                            for reference in relation.evidence
                        ),
                    }
                )
                for relation in extraction.relations
            ),
            "result_digest": "",
        }
    )


async def test_graph_effect_store_builds_request_from_the_command_envelope() -> None:
    writer = _GraphWriter(GraphProjectionStatus.APPLIED)
    effect = (await CogneeContextProjection(
        effect_store=DeterministicPhysicalStore(),
        retrieval=_Retrieval(),
    ).project(project_command())).effects[0]
    extraction = _extraction_for_effect(effect)
    store = CogneeGraphEffectStore(
        writer=writer,
        extraction_results={effect.source_revision_id: extraction},
    )

    result = await store.apply(effect.model_copy(update={"status": "planned"}))

    assert result.status is ProjectionEffectStatus.APPLIED
    assert writer.requests[0].projection_run_id == effect.projection_candidate.run_id
    assert writer.requests[0].source_revision.source_revision_id == effect.source_revision_id
    assert writer.requests[0].source_revision.processing_receipt_id == effect.processing_receipt_id
    assert (
        writer.requests[0].source_revision.locator_contract_version
        == effect.locator_contract_version
    )
    assert writer.requests[0].result == extraction


async def test_graph_effect_store_maps_recoverable_provider_failure_safely() -> None:
    effect = (await CogneeContextProjection(
        effect_store=DeterministicPhysicalStore(),
        retrieval=_Retrieval(),
    ).project(project_command())).effects[0]
    extraction = _extraction_for_effect(effect)
    store = CogneeGraphEffectStore(
        writer=_GraphWriter(GraphProjectionStatus.RECOVERABLE),
        extraction_results={effect.source_revision_id: extraction},
    )

    result = await store.apply(effect.model_copy(update={"status": "planned"}))

    assert result.status is ProjectionEffectStatus.FAILED
    assert result.failure_code.value == "physical_failure"


def test_factory_composes_public_sdk_adapters_without_importing_cognee() -> None:
    from tests.memory.cognee.test_graph import FakeCogneeSdk
    from tests.memory.cognee.test_retrieval import FakeSearchSdk

    projection = create_cognee_context_projection(
        artifacts={},
        extraction_results={},
        sdk=FakeCogneeSdk(),
        retrieval_sdk=FakeSearchSdk(),
    )

    assert isinstance(projection, CogneeContextProjection)
