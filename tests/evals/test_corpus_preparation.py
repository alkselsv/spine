from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from spine.auth import AuthorizedRequestContext, RequestedAuthorizationScope
from spine.application.document_ingestion import DocumentIngestionStatus
from spine.evals.corpus_preparation import (
    ApprovedCorpusArtifact,
    ApprovedCorpusArtifactLocation,
    ApprovedCorpusPreparationService,
    CorpusPreparationStatus,
    FilesystemApprovedCorpusArtifactStore,
    corpus_readiness_request_from_bindings,
)
from spine.evals.real_qa_r1 import (
    REAL_R1_DATASET_DIGEST,
    REAL_R1_DATASET_VERSION,
    RealRuntimeMappingManifest,
    RealRuntimeRevisionBinding,
    RealRuntimeRevisionIntent,
    RealRuntimeSourceIntent,
)

WORKSPACE_ID = UUID(int=101)
ENVIRONMENT_ID = UUID(int=201)
SOURCE_OBJECT_ID = UUID(int=301)
SOURCE_REVISION_ID = UUID(int=401)


def _content(index: int) -> bytes:
    return f"example-{index}".encode()


def _sha(index: int) -> str:
    return hashlib.sha256(_content(index)).hexdigest()


def _manifest() -> RealRuntimeMappingManifest:
    return RealRuntimeMappingManifest(
        dataset_version=REAL_R1_DATASET_VERSION,
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        owner_approval_ref="approval:test-only",
        sources=tuple(
            RealRuntimeSourceIntent(
                dataset_source_id=f"source:approved-{index}",
                upload_identity=f"approved.source.{index}",
                revisions=(
                    RealRuntimeRevisionIntent(
                        dataset_revision_id=f"revision:approved-{index}",
                        expected_original_sha256=_sha(index),
                        ingestion_intent_id=f"intent:approved-{index}",
                    ),
                ),
            )
            for index in range(30)
        ),
    )


def _artifact(index: int) -> ApprovedCorpusArtifact:
    return ApprovedCorpusArtifact(
        dataset_source_id=f"source:approved-{index}",
        dataset_revision_id=f"revision:approved-{index}",
        original_sha256=_sha(index),
        byte_length=len(_content(index)),
        media_type="text/plain",
        filename=f"approved-{index}.txt",
        content=_content(index),
    )


class _Artifacts:
    def __init__(self, artifacts: tuple[ApprovedCorpusArtifact, ...]) -> None:
        self.artifacts = {
            (item.dataset_source_id, item.dataset_revision_id): item
            for item in artifacts
        }

    async def resolve(self, *, dataset_source_id: str, dataset_revision_id: str):
        return self.artifacts.get((dataset_source_id, dataset_revision_id))


class _Ingestion:
    def __init__(
        self,
        status: DocumentIngestionStatus = DocumentIngestionStatus.SUCCEEDED,
        *,
        fail_after_effect_once: bool = False,
    ) -> None:
        self.status = status
        self.fail_after_effect_once = fail_after_effect_once
        self.requests = []
        self.payloads = []
        self.logical_effects: set[str] = set()

    async def ingest(self, *, request, chunks):
        self.requests.append(request)
        self.payloads.append(b"".join([chunk async for chunk in chunks]))
        replay = request.idempotency_key in self.logical_effects
        self.logical_effects.add(request.idempotency_key)
        if self.fail_after_effect_once:
            self.fail_after_effect_once = False
            raise RuntimeError("injected post-effect failure")
        return SimpleNamespace(
            status=self.status,
            source=SimpleNamespace(source_object_id=SOURCE_OBJECT_ID),
            revision=SimpleNamespace(revision_id=SOURCE_REVISION_ID),
            replay=replay,
        )


def _trusted_context() -> AuthorizedRequestContext:
    context = MagicMock(spec=AuthorizedRequestContext)
    context.scope = RequestedAuthorizationScope(
        workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
    )
    context.acting_subject_id = UUID(int=3010)
    context.authorization_generation = 1
    return context


async def _prepare(service: ApprovedCorpusPreparationService):
    return await service.prepare(
        manifest=_manifest(), trusted_context=_trusted_context()
    )


@pytest.mark.asyncio
async def test_prepare_routes_all_approved_artifacts_through_canonical_ingestion() -> None:
    ingestion = _Ingestion()
    service = ApprovedCorpusPreparationService(
        artifacts=_Artifacts(tuple(_artifact(index) for index in range(30))),
        ingestion=ingestion,
    )

    report = await _prepare(service)

    assert report.status is CorpusPreparationStatus.COMPLETED
    assert report.structural_validation_status == "passed"
    assert report.readiness_status == "not_checked"
    assert report.production_observation_status == "not_run"
    assert report.evaluation_status == "not_run"
    assert report.release_qualification_status == "not_run"
    assert len(report.revisions) == 30
    request = ingestion.requests[0]
    assert request.upload_identity == "approved.source.0"
    assert request.expected_sha256 == _sha(0)
    assert request.workspace_id == WORKSPACE_ID
    assert request.environment_id == ENVIRONMENT_ID
    assert ingestion.payloads[0] == _content(0)


@pytest.mark.asyncio
async def test_retry_after_partial_failure_reuses_one_logical_effect() -> None:
    ingestion = _Ingestion(fail_after_effect_once=True)
    service = ApprovedCorpusPreparationService(
        artifacts=_Artifacts(tuple(_artifact(index) for index in range(30))),
        ingestion=ingestion,
    )

    first = await _prepare(service)
    second = await _prepare(service)

    assert first.status is CorpusPreparationStatus.UNAVAILABLE
    assert second.status is CorpusPreparationStatus.COMPLETED
    assert second.revisions[0].replay is True
    assert ingestion.requests[0] == ingestion.requests[30]
    assert len(ingestion.logical_effects) == 30


@pytest.mark.asyncio
async def test_missing_rejected_or_mismatched_artifact_is_not_ready() -> None:
    missing = ApprovedCorpusPreparationService(
        artifacts=_Artifacts(()), ingestion=_Ingestion()
    )
    report = await _prepare(missing)
    assert report.status is CorpusPreparationStatus.INCOMPLETE
    assert {item.status for item in report.revisions} == {"missing"}
    assert report.readiness_status == "not_checked"

    rejected = ApprovedCorpusPreparationService(
        artifacts=_Artifacts(tuple(_artifact(index) for index in range(30))),
        ingestion=_Ingestion(DocumentIngestionStatus.REJECTED),
    )
    report = await _prepare(rejected)
    assert report.status is CorpusPreparationStatus.INCOMPLETE
    assert {item.status for item in report.revisions} == {"rejected"}

    ingestion = _Ingestion()
    mismatched = tuple(
        _artifact(index).model_copy(update={"original_sha256": "c" * 64})
        for index in range(30)
    )
    report = await _prepare(
        ApprovedCorpusPreparationService(
            artifacts=_Artifacts(mismatched), ingestion=ingestion
        )
    )
    assert {item.status for item in report.revisions} == {"digest_mismatch"}
    assert ingestion.requests == []


@pytest.mark.asyncio
async def test_filesystem_artifact_store_confines_and_reads_registered_file(tmp_path) -> None:
    source = tmp_path / "approved-0.txt"
    source.write_bytes(_content(0))
    store = FilesystemApprovedCorpusArtifactStore(
        root=tmp_path,
        artifacts=(
            ApprovedCorpusArtifactLocation(
                dataset_source_id="source:approved-0",
                dataset_revision_id="revision:approved-0",
                relative_path=source.name,
                expected_sha256=_sha(0),
                media_type="text/plain",
            ),
        ),
    )

    artifact = await store.resolve(
        dataset_source_id="source:approved-0",
        dataset_revision_id="revision:approved-0",
    )

    assert artifact is not None
    assert artifact.content == _content(0)
    assert artifact.original_sha256 == _sha(0)


@pytest.mark.asyncio
async def test_filesystem_artifact_store_rejects_registered_digest_mismatch(
    tmp_path,
) -> None:
    source = tmp_path / "approved-0.txt"
    source.write_bytes(_content(0))
    store = FilesystemApprovedCorpusArtifactStore(
        root=tmp_path,
        artifacts=(
            ApprovedCorpusArtifactLocation(
                dataset_source_id="source:approved-0",
                dataset_revision_id="revision:approved-0",
                relative_path=source.name,
                expected_sha256="f" * 64,
                media_type="text/plain",
            ),
        ),
    )

    with pytest.raises(ValueError, match="digest does not match registration"):
        await store.resolve(
            dataset_source_id="source:approved-0",
            dataset_revision_id="revision:approved-0",
        )


def _binding(index: int, **updates: object) -> RealRuntimeRevisionBinding:
    values: dict[str, object] = {
        "manifest_sha256": "d" * 64,
        "dataset_source_id": f"source:approved-{index}",
        "dataset_revision_id": f"revision:approved-{index}",
        "upload_identity": f"approved.source.{index}",
        "ingestion_intent_id": f"intent:approved-{index}",
        "owner_approval_ref": "approval:test-only",
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "source_object_id": UUID(int=10_000 + index),
        "source_revision_id": UUID(int=20_000 + index),
        "canonical_acceptance_id": UUID(int=30_000 + index),
        "original_sha256": _sha(index),
    }
    values.update(updates)
    return RealRuntimeRevisionBinding.model_validate(values)


def test_verified_mapping_bindings_adapt_to_exact_readiness_request() -> None:
    bindings = tuple(_binding(index) for index in range(30))
    request = corpus_readiness_request_from_bindings(
        bindings, projection_kind="context_graph"
    )
    assert request.workspace_id == WORKSPACE_ID
    assert len(request.revisions) == 30
    assert request.revisions[0].source_revision_id == UUID(int=20_000)


def test_readiness_rejects_mixed_or_partial_mapping_bindings() -> None:
    bindings = tuple(_binding(index) for index in range(30))
    with pytest.raises(ValueError):
        corpus_readiness_request_from_bindings(
            bindings[:-1], projection_kind="context_graph"
        )
    with pytest.raises(ValueError):
        corpus_readiness_request_from_bindings(
            (*bindings[:-1], _binding(29, manifest_sha256="e" * 64)),
            projection_kind="context_graph",
        )
