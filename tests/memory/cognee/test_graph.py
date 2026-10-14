from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionNamespaceState,
    ProjectionOperation,
)
from spine.memory.cognee.extraction import (
    EntityCandidate,
    ExtractionBatch,
    GraphProjectionRequest,
    RelationCandidate,
    extract_projection,
)
from spine.memory.cognee.graph import CogneeGraphProjectionWriter, GraphProjectionStatus
from spine.memory.cognee.validation import PhysicalArtifact
from spine.memory.cognee.models.baseline import (
    BASELINE_EXTRACTION_CONFIG,
    BASELINE_ONTOLOGY,
    build_baseline_fixtures,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter


RUN_ID = UUID("50000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("60000000-0000-4000-8000-000000000001")
PARTITION_ID = UUID("70000000-0000-4000-8000-000000000001")
CONFIG_ID = UUID("80000000-0000-4000-8000-000000000001")
BOUNDARY_ID = UUID("90000000-0000-4000-8000-000000000001")
DATASET_ID = UUID("a0000000-0000-4000-8000-000000000001")


@dataclass(frozen=True)
class FakeDataItem:
    data: str
    data_id: UUID
    external_metadata: dict[str, object]


class FakeCogneeSdk:
    version = "1.5.4"

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        fail_on_attempt: int | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.fail_on_attempt = fail_on_attempt
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._attempts: dict[str, int] = {}

    def make_data_item(
        self,
        *,
        data: str,
        data_id: UUID,
        external_metadata: dict[str, object],
    ) -> FakeDataItem:
        return FakeDataItem(data, data_id, external_metadata)

    async def add(
        self,
        *,
        data: list[FakeDataItem],
        dataset_name: str,
        dataset_id: UUID | None,
        node_set: list[str],
    ) -> dict[str, object]:
        self._record("add", data=data, dataset_name=dataset_name, dataset_id=dataset_id, node_set=node_set)
        return {"dataset_id": DATASET_ID}

    async def cognify(self, *, datasets: list[str]) -> None:
        self._record("cognify", datasets=datasets)

    async def update(
        self,
        *,
        data_id: UUID,
        data: FakeDataItem,
        dataset_id: UUID,
        node_set: list[str],
        chunk_level_diff: bool,
    ) -> None:
        self._record(
            "update",
            data_id=data_id,
            data=data,
            dataset_id=dataset_id,
            node_set=node_set,
            chunk_level_diff=chunk_level_diff,
        )

    async def delete_data(self, *, dataset_id: UUID, data_id: UUID, mode: str) -> None:
        self._record("delete_data", dataset_id=dataset_id, data_id=data_id, mode=mode)

    async def list_datasets(self) -> list[object]:
        return [SimpleNamespace(id=DATASET_ID, name="unused")]

    def _record(self, name: str, **payload: object) -> None:
        self._attempts[name] = self._attempts.get(name, 0) + 1
        if self.fail_on == name and (
            self.fail_on_attempt is None
            or self._attempts[name] == self.fail_on_attempt
        ):
            raise RuntimeError("synthetic provider failure containing protected graph text")
        self.calls.append((name, payload))


def extraction_result(
    *,
    include_relation: bool = True,
    include_forbidden: bool = False,
    workspace_id: UUID | None = None,
    environment_id: UUID | None = None,
):
    fixture = build_baseline_fixtures()["positive"]
    workspace_id = workspace_id or fixture.source_revisions[0].workspace_id
    environment_id = environment_id or fixture.source_revisions[0].environment_id
    revision_id = fixture.source_revisions[0].source_revision_id
    source_revisions = tuple(
        item.model_copy(update={"workspace_id": workspace_id, "environment_id": environment_id})
        for item in fixture.source_revisions
    )
    entities = tuple(
        EntityCandidate(
            candidate_id=item.entity_id,
            source_label="organization" if item.entity_id == "entity-acme" else "person",
            canonical_name=item.canonical_name,
            source_revision_id=revision_id,
            chunk_ids=item.chunk_ids,
            evidence=(item.evidence[0],),
        )
        for item in fixture.entities
    )
    if include_forbidden:
        entities += (
            EntityCandidate(
                candidate_id="entity-forbidden",
                source_label="secret_project",
                canonical_name="Forbidden graph content",
                source_revision_id=revision_id,
                chunk_ids=("chunk-current",),
                evidence=(fixture.entities[0].evidence[0],),
            ),
        )
    relations = (
        RelationCandidate(
            candidate_id="relation-owner",
            source_label="works_for",
            source_revision_id=revision_id,
            source_entity_candidate_id="entity-owner",
            target_entity_candidate_id="entity-acme",
            evidence=(fixture.relations[0].evidence[0],),
        ),
    ) if include_relation else ()
    return extract_projection(
        ExtractionBatch(
            workspace_id=workspace_id,
            environment_id=environment_id,
            ontology=BASELINE_ONTOLOGY,
            extraction_config=BASELINE_EXTRACTION_CONFIG,
            source_revisions=source_revisions,
            chunks=fixture.chunks,
            entities=entities,
            relations=relations,
        )
    )


def tombstone_result():
    fixture = build_baseline_fixtures()["tombstone_revision"]
    return extract_projection(
        ExtractionBatch(
            workspace_id=fixture.source_revisions[0].workspace_id,
            environment_id=fixture.source_revisions[0].environment_id,
            ontology=BASELINE_ONTOLOGY,
            extraction_config=BASELINE_EXTRACTION_CONFIG,
            source_revisions=fixture.source_revisions,
        )
    )


def request(
    result,
    *,
    operation: ProjectionOperation = ProjectionOperation.PROJECT,
    identity: str = "graph-1",
    digest: str = "a" * 64,
    candidate_id: UUID = CANDIDATE_ID,
) -> GraphProjectionRequest:
    candidate = ProjectionCandidate(
        candidate_id=candidate_id,
        run_id=RUN_ID,
        partition_id=PARTITION_ID,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG_ID,
        version="config-1",
        config_digest="b" * 64,
        ontology_version=result.ontology_version,
        adapter_contract_version="graph-projection:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY_ID,
        boundary_digest="c" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=result.workspace_id,
            environment_id=result.environment_id,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=ProjectionNamespaceState.ACTIVE,
        )
    )
    return GraphProjectionRequest(
        operation=operation,
        workspace_id=result.workspace_id,
        environment_id=result.environment_id,
        projection_run_id=RUN_ID,
        command_identity=identity,
        command_digest=digest,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        result=result,
    )


@pytest.mark.asyncio
async def test_graph_writer_preserves_approved_artifacts_and_evidence_lineage() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    written = await writer.write(request(result))

    assert written.status is GraphProjectionStatus.APPLIED
    add = next(payload for name, payload in sdk.calls if name == "add")
    items = add["data"]
    assert isinstance(items, list)
    assert {item.external_metadata["artifact_id"] for item in items} == {
        "entity-acme",
        "entity-owner",
        "relation-owner",
    }
    entity_metadata = next(
        item.external_metadata for item in items if item.external_metadata["artifact_id"] == "entity-acme"
    )
    assert entity_metadata["source_revision_id"] == str(result.entities[0].source_revision_id)
    assert entity_metadata["ontology_version"] == result.ontology_version
    assert entity_metadata["evidence"][0]["evidence_id"] == "evidence-acme"
    physical = PhysicalArtifact.from_graph_mapping(written.mappings[0])
    assert physical.projection_config_digest == request(result).projection_config.config_digest
    assert physical.locators[0].digest


@pytest.mark.asyncio
async def test_graph_writer_never_writes_rejected_ontology_candidates() -> None:
    result = extraction_result(include_forbidden=True)
    sdk = FakeCogneeSdk()

    response = await CogneeGraphProjectionWriter(sdk=sdk).write(request(result))

    assert response.status is GraphProjectionStatus.APPLIED
    add = next(payload for name, payload in sdk.calls if name == "add")
    items = add["data"]
    assert result.status.value == "degraded"
    assert {item.external_metadata["artifact_id"] for item in items} == {
        "entity-acme",
        "entity-owner",
        "relation-owner",
    }


@pytest.mark.asyncio
async def test_graph_writer_replays_the_same_command_without_a_second_write() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)
    command = request(result)

    first = await writer.write(command)
    replay = await writer.write(command)

    assert first.status is GraphProjectionStatus.APPLIED
    assert replay.status is GraphProjectionStatus.REPLAYED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_graph_writer_rejects_a_changed_command_digest() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    await writer.write(request(result))
    conflict = await writer.write(request(result, identity="graph-1", digest="d" * 64))

    assert conflict.status is GraphProjectionStatus.FAILED
    assert conflict.failure_code == "integrity_conflict"
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_graph_writer_updates_graph_artifacts_without_changing_data_identity() -> None:
    result = extraction_result()
    updated_values = result.model_dump(mode="python")
    updated_values.pop("result_digest")
    updated_values.update({"entities": (result.entities[0],), "relations": ()})
    updated = result.model_validate(updated_values)
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    first = await writer.write(request(result))
    changed = await writer.write(
        request(updated, operation=ProjectionOperation.UPDATE, identity="graph-update", digest="d" * 64)
    )

    assert first.status is GraphProjectionStatus.APPLIED
    assert changed.status is GraphProjectionStatus.APPLIED
    update_calls = [payload for name, payload in sdk.calls if name == "update"]
    assert update_calls[0]["data_id"] == first.mappings[0].data_id


@pytest.mark.asyncio
async def test_graph_writer_resumes_after_a_later_update_without_repeating_completed_updates() -> None:
    result = extraction_result()
    updated_values = result.model_dump(mode="python")
    updated_values.pop("result_digest")
    updated_values["entities"] = (
        result.entities[0].model_copy(update={"canonical_name": "Acme Updated"}),
        result.entities[1],
    )
    updated = result.model_validate(updated_values)
    sdk = FakeCogneeSdk(fail_on="update", fail_on_attempt=2)
    writer = CogneeGraphProjectionWriter(sdk=sdk)
    command = request(updated, operation=ProjectionOperation.UPDATE, identity="graph-update", digest="d" * 64)

    await writer.write(request(result))
    failed = await writer.write(command)
    sdk.fail_on = None
    recovered = await writer.write(command)

    assert failed.status is GraphProjectionStatus.RECOVERABLE
    assert recovered.status is GraphProjectionStatus.APPLIED
    update_calls = [payload for name, payload in sdk.calls if name == "update"]
    assert len(update_calls) == 3
    assert update_calls[0]["data_id"] not in {
        update_calls[1]["data_id"],
        update_calls[2]["data_id"],
    }


@pytest.mark.asyncio
async def test_graph_writer_removes_artifacts_for_a_newer_tombstone() -> None:
    result = extraction_result()
    tombstone = tombstone_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    await writer.write(request(result))
    removed = await writer.write(
        request(tombstone, operation=ProjectionOperation.REMOVE, identity="graph-remove", digest="e" * 64)
    )

    assert removed.status is GraphProjectionStatus.APPLIED
    assert len([name for name, _ in sdk.calls if name == "delete_data"]) == 3


@pytest.mark.asyncio
async def test_graph_writer_resumes_after_a_later_delete_without_repeating_completed_deletes() -> None:
    result = extraction_result()
    tombstone = tombstone_result()
    sdk = FakeCogneeSdk(fail_on="delete_data", fail_on_attempt=2)
    writer = CogneeGraphProjectionWriter(sdk=sdk)
    command = request(
        tombstone,
        operation=ProjectionOperation.REMOVE,
        identity="graph-remove",
        digest="e" * 64,
    )

    await writer.write(request(result))
    failed = await writer.write(command)
    sdk.fail_on = None
    removed = await writer.write(command)

    assert failed.status is GraphProjectionStatus.RECOVERABLE
    assert removed.status is GraphProjectionStatus.APPLIED
    delete_calls = [payload for name, payload in sdk.calls if name == "delete_data"]
    assert len(delete_calls) == 3
    assert delete_calls[0]["data_id"] not in {
        delete_calls[1]["data_id"],
        delete_calls[2]["data_id"],
    }


@pytest.mark.asyncio
async def test_graph_writer_recovers_after_cognify_failure_without_readding_artifacts() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk(fail_on="cognify")
    writer = CogneeGraphProjectionWriter(sdk=sdk)
    command = request(result)

    failed = await writer.write(command)
    sdk.fail_on = None
    recovered = await writer.write(command)

    assert failed.status is GraphProjectionStatus.RECOVERABLE
    assert recovered.status is GraphProjectionStatus.APPLIED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_graph_writer_redacts_provider_failure_content_from_result() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk(fail_on="cognify")

    failed = await CogneeGraphProjectionWriter(sdk=sdk).write(request(result))

    assert failed.status is GraphProjectionStatus.RECOVERABLE
    assert "Forbidden graph content" not in failed.model_dump_json()
    assert "protected graph text" not in failed.model_dump_json()


@pytest.mark.asyncio
async def test_graph_writer_fences_an_older_revision_after_tombstone() -> None:
    result = extraction_result()
    tombstone = tombstone_result()
    writer = CogneeGraphProjectionWriter(sdk=FakeCogneeSdk())

    await writer.write(request(result))
    await writer.write(
        request(tombstone, operation=ProjectionOperation.REMOVE, identity="graph-remove", digest="e" * 64)
    )
    stale = await writer.write(
        request(result, identity="graph-stale", digest="f" * 64)
    )

    assert stale.status is GraphProjectionStatus.FENCED


@pytest.mark.asyncio
async def test_graph_writer_rebuilds_a_namespace_without_duplicate_artifacts() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    await writer.write(request(result))
    rebuilt = await writer.write(
        request(result, operation=ProjectionOperation.REBUILD, identity="graph-rebuild", digest="b" * 64)
    )

    assert rebuilt.status is GraphProjectionStatus.APPLIED
    assert len([name for name, _ in sdk.calls if name == "add"]) == 1
    assert len([name for name, _ in sdk.calls if name == "update"]) == 3


@pytest.mark.asyncio
async def test_graph_writer_failed_shadow_rebuild_does_not_change_active_namespace() -> None:
    result = extraction_result()
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)
    active = await writer.write(request(result))
    sdk.fail_on = "cognify"

    failed = await writer.write(
        request(
            result,
            operation=ProjectionOperation.REBUILD,
            identity="graph-failed-rebuild",
            digest="f" * 64,
            candidate_id=UUID("60000000-0000-4000-8000-000000000002"),
        )
    )

    assert failed.status is GraphProjectionStatus.RECOVERABLE
    assert await writer.mappings_for(
        workspace_id=result.workspace_id,
        environment_id=result.environment_id,
        namespace_digest=active.mappings[0].namespace_digest,
    ) == active.mappings


@pytest.mark.asyncio
async def test_graph_writer_rejects_a_namespace_from_another_workspace_before_write() -> None:
    result = extraction_result()
    command = request(result)
    foreign_candidate = command.projection_candidate
    foreign_context = ProjectionNamespaceContext(
        workspace_id=UUID("99999999-9999-4999-8999-999999999999"),
        environment_id=command.environment_id,
        projection_candidate=foreign_candidate,
        projection_config=command.projection_config,
        canonical_boundary=command.canonical_boundary,
        state=ProjectionNamespaceState.ACTIVE,
    )
    forged = command.model_copy(
        update={"physical_namespace": ProjectionNamespaceRouter().route(foreign_context)}
    )
    sdk = FakeCogneeSdk()

    response = await CogneeGraphProjectionWriter(sdk=sdk).write(forged)

    assert response.status is GraphProjectionStatus.FAILED
    assert response.failure_code == "namespace_unavailable"
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_graph_writer_keeps_valid_workspaces_in_separate_namespaces() -> None:
    workspace_a = UUID("33333333-3333-4333-8333-333333333331")
    workspace_b = UUID("33333333-3333-4333-8333-333333333332")
    result_a = extraction_result(workspace_id=workspace_a)
    result_b = extraction_result(workspace_id=workspace_b)
    sdk = FakeCogneeSdk()
    writer = CogneeGraphProjectionWriter(sdk=sdk)

    first = await writer.write(request(result_a, candidate_id=CANDIDATE_ID))
    second = await writer.write(
        request(
            result_b,
            identity="graph-workspace-b",
            digest="c" * 64,
            candidate_id=CANDIDATE_ID,
        )
    )
    assert first.status is GraphProjectionStatus.APPLIED
    assert second.status is GraphProjectionStatus.APPLIED
    assert first.mappings[0].namespace_digest != second.mappings[0].namespace_digest
    assert first.mappings[0].data_id != second.mappings[0].data_id


@pytest.mark.asyncio
async def test_graph_writer_keeps_environments_in_separate_namespaces() -> None:
    result_a = extraction_result()
    result_b = extraction_result(
        environment_id=UUID("22222222-2222-4222-8222-222222222223"),
    )
    writer = CogneeGraphProjectionWriter(sdk=FakeCogneeSdk())

    first = await writer.write(request(result_a, identity="graph-environment-a"))
    second = await writer.write(
        request(result_b, identity="graph-environment-b", digest="e" * 64)
    )

    assert first.status is GraphProjectionStatus.APPLIED
    assert second.status is GraphProjectionStatus.APPLIED
    assert first.mappings[0].namespace_digest != second.mappings[0].namespace_digest


@pytest.mark.asyncio
async def test_graph_writer_keeps_candidates_in_separate_namespaces() -> None:
    result = extraction_result()
    writer = CogneeGraphProjectionWriter(sdk=FakeCogneeSdk())

    first = await writer.write(request(result, identity="graph-candidate-a"))
    second = await writer.write(
        request(
            result,
            identity="graph-candidate-b",
            digest="e" * 64,
            candidate_id=UUID("60000000-0000-4000-8000-000000000003"),
        )
    )

    assert first.status is GraphProjectionStatus.APPLIED
    assert second.status is GraphProjectionStatus.APPLIED
    assert first.mappings[0].namespace_digest != second.mappings[0].namespace_digest
