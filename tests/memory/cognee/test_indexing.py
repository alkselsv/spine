from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.domain.artifacts import ParsedDocument
from spine.domain.sources import RevisionMetadata, SourceRevision, content_revision
from spine.domain.sources.original_reference import ObjectReference
from spine.ingestion.parsers import parse_document
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionEffect,
    ProjectionEffectAction,
    ProjectionEffectStatus,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionOperation,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter

from spine.memory.cognee.indexing import CogneeChunkProjectionStore, CogneeVersionError


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000002")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_OBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("40000000-0000-4000-8000-000000000001")
NEXT_REVISION_ID = UUID("40000000-0000-4000-8000-000000000002")
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

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict[str, object]]] = []

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
        if self.fail_on == name:
            raise RuntimeError("synthetic provider failure containing protected text")
        self.calls.append((name, payload))


def parsed_document(content: bytes, revision_id: UUID) -> tuple[SourceRevision, ParsedDocument]:
    digest = sha256(content).hexdigest()
    source_revision = content_revision(
        revision_id=revision_id,
        source_object_id=SOURCE_OBJECT_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="fixture"),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID("b0000000-0000-4000-8000-000000000001"),
            storage_generation=UUID("c0000000-0000-4000-8000-000000000001"),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(content),
        ),
        original_sha256=digest,
        byte_length=len(content),
        media_type="text/plain",
        observed_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )
    return source_revision, parse_document(source_revision, content)


def effect(
    *,
    source_revision: SourceRevision,
    revision_number: int = 1,
    action: ProjectionEffectAction = ProjectionEffectAction.UPSERT_SOURCE,
    operation: ProjectionOperation | None = None,
    command_digest: str | None = None,
) -> ProjectionEffect:
    candidate = ProjectionCandidate(
        candidate_id=CANDIDATE_ID,
        run_id=RUN_ID,
        partition_id=PARTITION_ID,
    )
    config = ProjectionConfigVersion(
        config_id=CONFIG_ID,
        version="config-1",
        config_digest="d" * 64,
        ontology_version="ontology-1",
        adapter_contract_version="context-projection:v1",
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY_ID,
        boundary_digest="e" * 64,
        accepted_through_generation=1,
    )
    namespace = ProjectionNamespaceRouter().route(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_candidate=candidate,
            projection_config=config,
            canonical_boundary=boundary,
            state=ProjectionNamespaceState.ACTIVE,
        )
    )
    return ProjectionEffect(
        effect_id=f"effect-{revision_number}-{action.value}",
        operation=operation or (
            ProjectionOperation.REMOVE
            if action is ProjectionEffectAction.REMOVE_SOURCE
            else ProjectionOperation.PROJECT
        ),
        action=action,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        command_identity="project-1",
        command_digest=command_digest,
        projection_candidate=candidate,
        projection_config=config,
        canonical_boundary=boundary,
        physical_namespace=namespace,
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=source_revision.revision_id,
        revision_digest=source_revision.revision_digest,
        revision_number=revision_number,
    )


@pytest.mark.asyncio
async def test_indexing_preserves_revision_locator_and_namespace_provenance() -> None:
    source_revision, document = parsed_document(b"first line\nsecond line\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})
    command_effect = effect(source_revision=source_revision)

    result = await store.apply(command_effect)

    assert result.status is ProjectionEffectStatus.APPLIED
    add = next(payload for name, payload in sdk.calls if name == "add")
    items = add["data"]
    assert isinstance(items, list)
    assert len(items) == len(document.segments)
    metadata = items[0].external_metadata
    assert metadata["workspace_id"] == str(WORKSPACE_ID)
    assert metadata["environment_id"] == str(ENVIRONMENT_ID)
    assert metadata["source_revision_id"] == str(REVISION_ID)
    assert metadata["revision_digest"] == source_revision.revision_digest
    assert metadata["namespace_digest"] == command_effect.physical_namespace.namespace_digest
    assert metadata["locator"] == document.segments[0].locator.model_dump(mode="json")


@pytest.mark.asyncio
async def test_same_effect_replays_without_a_second_physical_write() -> None:
    source_revision, document = parsed_document(b"first line\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})
    command_effect = effect(source_revision=source_revision)

    first = await store.apply(command_effect)
    replay = await store.apply(command_effect)

    assert first.status is ProjectionEffectStatus.APPLIED
    assert replay.status is ProjectionEffectStatus.REPLAYED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_older_revision_is_fenced_without_overwriting_physical_chunks() -> None:
    current_revision, current_document = parsed_document(b"current\n", REVISION_ID)
    older_revision, older_document = parsed_document(b"older\n", NEXT_REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(
        sdk=sdk,
        artifacts={REVISION_ID: current_document, NEXT_REVISION_ID: older_document},
    )

    applied = await store.apply(effect(source_revision=current_revision, revision_number=2))
    fenced = await store.apply(effect(source_revision=older_revision, revision_number=1))

    assert applied.status is ProjectionEffectStatus.APPLIED
    assert fenced.status is ProjectionEffectStatus.FAILED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_update_uses_cognee_update_and_keeps_data_identity_for_shared_chunks() -> None:
    current_revision, current_document = parsed_document(b"first\nsecond\n", REVISION_ID)
    next_revision, next_document = parsed_document(b"firsx\nsecond\n", NEXT_REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(
        sdk=sdk,
        artifacts={REVISION_ID: current_document, NEXT_REVISION_ID: next_document},
    )

    await store.apply(effect(source_revision=current_revision, revision_number=1))
    result = await store.apply(
        effect(
            source_revision=next_revision,
            revision_number=2,
            operation=ProjectionOperation.UPDATE,
        )
    )

    assert result.status is ProjectionEffectStatus.APPLIED
    update_calls = [payload for name, payload in sdk.calls if name == "update"]
    assert len(update_calls) == 2
    assert update_calls[0]["data_id"] == current_document.segments[0].segment_id
    assert update_calls[0]["data"].data == "firsx"


@pytest.mark.asyncio
async def test_remove_tombstones_source_and_repeated_remove_does_not_readd_content() -> None:
    source_revision, document = parsed_document(b"protected content\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})

    await store.apply(effect(source_revision=source_revision))
    removed = await store.apply(
        effect(source_revision=source_revision, action=ProjectionEffectAction.REMOVE_SOURCE)
    )
    replay = await store.apply(
        effect(source_revision=source_revision, action=ProjectionEffectAction.REMOVE_SOURCE)
    )

    assert removed.status is ProjectionEffectStatus.APPLIED
    assert replay.status is ProjectionEffectStatus.REPLAYED
    assert [name for name, _ in sdk.calls] == ["add", "cognify", "delete_data"]
    assert await store.mappings_for(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_OBJECT_ID,
    ) == ()


@pytest.mark.asyncio
async def test_physical_failure_returns_failed_effect_without_provider_content() -> None:
    source_revision, document = parsed_document(b"secret physical text\n", REVISION_ID)
    store = CogneeChunkProjectionStore(
        sdk=FakeCogneeSdk(fail_on="add"),
        artifacts={REVISION_ID: document},
    )

    result = await store.apply(effect(source_revision=source_revision))

    assert result.status is ProjectionEffectStatus.FAILED
    assert "secret physical text" not in result.model_dump_json()


def test_adapter_rejects_an_unqualified_cognee_version() -> None:
    sdk = FakeCogneeSdk()
    sdk.version = "1.5.3"

    with pytest.raises(CogneeVersionError):
        CogneeChunkProjectionStore(sdk=sdk, artifacts={})


@pytest.mark.asyncio
async def test_mismatched_parsed_artifact_lineage_is_not_written() -> None:
    source_revision, document = parsed_document(b"lineage\n", REVISION_ID)
    wrong_document = document.model_copy(
        update={"source_revision_digest": "f" * 64}
    )
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: wrong_document})

    result = await store.apply(effect(source_revision=source_revision))

    assert result.status is ProjectionEffectStatus.FAILED
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_same_command_identity_with_changed_digest_is_an_integrity_conflict() -> None:
    source_revision, document = parsed_document(b"same command\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})

    first = await store.apply(
        effect(source_revision=source_revision, command_digest="1" * 64)
    )
    conflict = await store.apply(
        effect(source_revision=source_revision, command_digest="2" * 64).model_copy(
            update={"effect_id": "effect-conflict"}
        )
    )

    assert first.status is ProjectionEffectStatus.APPLIED
    assert conflict.status is ProjectionEffectStatus.FAILED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_cognify_retry_resumes_pending_write_without_readding_chunks() -> None:
    source_revision, document = parsed_document(b"retry me\n", REVISION_ID)
    sdk = FakeCogneeSdk(fail_on="cognify")
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})
    command_effect = effect(source_revision=source_revision)

    first = await store.apply(command_effect)
    sdk.fail_on = None
    recovered = await store.apply(command_effect)

    assert first.status is ProjectionEffectStatus.FAILED
    assert recovered.status is ProjectionEffectStatus.APPLIED
    assert [name for name, _ in sdk.calls] == ["add", "cognify"]


@pytest.mark.asyncio
async def test_foreign_workspace_effect_is_denied_before_physical_write() -> None:
    source_revision, document = parsed_document(b"workspace scoped\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})
    foreign_effect = effect(source_revision=source_revision).model_copy(
        update={"effect_id": "foreign-effect", "workspace_id": OTHER_WORKSPACE_ID}
    )

    result = await store.apply(foreign_effect)

    assert result.status is ProjectionEffectStatus.FAILED
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_rebuild_replaces_same_revision_physical_chunks_without_duplication() -> None:
    source_revision, document = parsed_document(b"rebuild me\n", REVISION_ID)
    sdk = FakeCogneeSdk()
    store = CogneeChunkProjectionStore(sdk=sdk, artifacts={REVISION_ID: document})

    await store.apply(effect(source_revision=source_revision))
    rebuilt = await store.apply(
        effect(source_revision=source_revision, operation=ProjectionOperation.REBUILD).model_copy(
            update={"effect_id": "effect-rebuild"}
        )
    )

    assert rebuilt.status is ProjectionEffectStatus.APPLIED
    assert [name for name, _ in sdk.calls] == [
        "add",
        "cognify",
        "delete_data",
        "add",
        "cognify",
    ]
