from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.corpus_readiness import (
    CorpusReadinessReason,
    CorpusReadinessReport,
    CorpusReadinessRequest,
    CorpusRevisionExpectation,
    CorpusRevisionReadiness,
    CorpusRetrievalEligibility,
    CorpusReadinessStatus,
)
from spine.evals import real_benchmark_runner as runner_module
from spine.evals.production_contract import ProductionContractCaseRequest
from spine.evals.real_benchmark_runner import RealBenchmarkRunner, RuntimeVersionPins
from spine.evals.real_evaluation import FinalizedRealObservation
from spine.evals.real_qa_r1 import (
    REAL_R1_DATASET_DIGEST,
    REAL_R1_DATASET_VERSION,
    REAL_R1_CASE_SCHEMA_VERSION,
    LoadedRealDataset,
    RealQACase,
    RealQADataset,
    RealRuntimeCaseResolution,
    RealRuntimeMappingManifest,
    RealRuntimeRevisionBinding,
    RealRuntimeRevisionIntent,
    RealRuntimeSourceIntent,
    RealSourceObject,
    RealSourceRevision,
    RealDatasetValidationReport,
    runtime_mapping_manifest_digest,
    runtime_revision_bindings_digest,
)
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_contract import _safe_observation


WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000201")
PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000301")
SOURCE_OBJECT_ID = UUID("00000000-0000-0000-0000-000000000401")
SOURCE_REVISION_ID = UUID("00000000-0000-0000-0000-000000000501")
ACCEPTANCE_ID = UUID("00000000-0000-0000-0000-000000000601")


def _scenario():
    case = RealQACase(
        case_schema_version=REAL_R1_CASE_SCHEMA_VERSION,
        case_id="case:opaque-3",
        question="Which approved fact is present?",
        language="en",
        expected_outcome="abstained",
        expected_reason="insufficient_evidence",
        source_ids=("source:opaque-7",),
        source_revision_ids=("revision:opaque-11",),
        available_source_revision_ids=("revision:opaque-11",),
        question_type="abstention",
        difficulty="easy",
        synthetic=False,
    )
    dataset = RealQADataset(
        dataset_id="qa-r1-real-draft",
        dataset_kind="real_document_draft",
        schema_version="qa-manifest:v1",
        case_schema_version=REAL_R1_CASE_SCHEMA_VERSION,
        version=REAL_R1_DATASET_VERSION,
        dataset_integrity_digest=REAL_R1_DATASET_DIGEST,
        approval_status="approved-for-test-only",
        authorization_status="approved-for-test-only",
        integrity_digest_algorithm="sha256",
        language="en",
        privacy_review_status="synthetic-test-data",
        status="ready",
        change_log={},
        compatibility={},
        created_by="test-fixture",
        generated_at_utc="2026-10-18T00:00:00Z",
        dataset_status="ready",
        question_approval={},
        authorization_approval={},
        anonymization_status="synthetic-test-data",
        anonymization_confirmation={},
        answer_evidence_approval={},
        total_abstention_candidates=1,
        cases=(case,),
        source_objects=(
            RealSourceObject(
                source_id="source:opaque-7",
                source_kind="document",
                display_title="Synthetic test source",
                format="txt",
            ),
        ),
        source_revisions=(
            RealSourceRevision(
                source_id="source:opaque-7",
                source_revision_id="revision:opaque-11",
                revision_number=1,
                source_sha256="a" * 64,
                revision_digest="b" * 64,
                anonymized_representation_sha256="c" * 64,
                extraction_status="passed",
            ),
        ),
    )
    validation_report = RealDatasetValidationReport(
        validation_status="passed",
        dataset_id="qa-r1-real-draft",
        version=REAL_R1_DATASET_VERSION,
        digest=REAL_R1_DATASET_DIGEST,
        validator_version="test-validator:v1",
        csv_consistency_status="passed",
        csv_mismatch_case_count=0,
        evidence_integrity_status="passed",
        locator_mismatch_count=0,
        case_count=1,
        answered_case_count=0,
        abstained_case_count=1,
        denied_case_count=0,
        source_count=1,
        source_files_verified=1,
        evidence_count=0,
        claim_count=0,
    )
    loaded = LoadedRealDataset(
        dataset=dataset,
        validation_report=validation_report,
    )
    manifest = RealRuntimeMappingManifest(
        dataset_version=REAL_R1_DATASET_VERSION,
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        owner_approval_ref="approval:test-fixture",
        sources=(
            RealRuntimeSourceIntent(
                dataset_source_id="source:opaque-7",
                upload_identity="approved.source.7",
                revisions=(
                    RealRuntimeRevisionIntent(
                        dataset_revision_id="revision:opaque-11",
                        expected_original_sha256="a" * 64,
                        ingestion_intent_id="intent:revision-11",
                    ),
                ),
            ),
        ),
    )
    manifest_digest = runtime_mapping_manifest_digest(manifest)
    binding = RealRuntimeRevisionBinding(
        manifest_sha256=manifest_digest,
        dataset_source_id="source:opaque-7",
        dataset_revision_id="revision:opaque-11",
        upload_identity="approved.source.7",
        ingestion_intent_id="intent:revision-11",
        owner_approval_ref="approval:test-fixture",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        canonical_acceptance_id=ACCEPTANCE_ID,
        original_sha256="a" * 64,
    )
    request = ProductionContractCaseRequest(
        case_id=case.case_id,
        question=case.question,
        language=case.language,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        principal_id=PRINCIPAL_ID,
        available_revision_ids=(SOURCE_REVISION_ID,),
        retrieval_strategy="hybrid",
    )
    resolution = RealRuntimeCaseResolution(
        request=request,
        manifest_sha256=manifest_digest,
        binding_sha256=runtime_revision_bindings_digest((binding,)),
        bindings=(binding,),
        authorization_generation=1,
    )
    return loaded, manifest, resolution


def _readiness_request(bindings, *, projection_kind: str) -> CorpusReadinessRequest:
    return CorpusReadinessRequest(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind=projection_kind,
        revisions=tuple(
            CorpusRevisionExpectation(
                dataset_source_id=item.dataset_source_id,
                dataset_revision_id=item.dataset_revision_id,
                workspace_id=item.workspace_id,
                environment_id=item.environment_id,
                source_object_id=item.source_object_id,
                source_revision_id=item.source_revision_id,
                canonical_acceptance_id=item.canonical_acceptance_id,
            )
            for item in bindings
        ),
    )


def _readiness_report(
    status: CorpusReadinessStatus,
    reason: CorpusReadinessReason,
) -> CorpusReadinessReport:
    ready = status is CorpusReadinessStatus.READY
    return CorpusReadinessReport(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="qa",
        status=status,
        preparation_status="completed",
        revisions=(
            CorpusRevisionReadiness(
                dataset_source_id="source:opaque-7",
                dataset_revision_id="revision:opaque-11",
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                source_object_id=SOURCE_OBJECT_ID,
                source_revision_id=SOURCE_REVISION_ID,
                canonical_acceptance_id=ACCEPTANCE_ID,
                status=status,
                reason=reason,
                ingestion_status="succeeded",
                acceptance_status="accepted",
                currentness_status="current",
                projection_processing_status="processed" if ready else "pending",
                activation_status="active" if ready else "unpublished",
                membership_status="included" if ready else "missing",
                publication_health="healthy" if ready else "degraded",
                publication_freshness="current" if ready else "stale",
                retrieval_eligibility=(
                    CorpusRetrievalEligibility.ELIGIBLE
                    if ready
                    else CorpusRetrievalEligibility.UNAVAILABLE
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_real_benchmark_readiness_blocks_observer_and_gold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(
                CorpusReadinessStatus.NOT_READY,
                CorpusReadinessReason.PROJECTION_PENDING,
            )

    class Observer:
        calls = 0

        async def observe(self, **_kwargs):
            self.calls += 1
            raise AssertionError("not-ready cases must not reach observer")

    class Gold:
        population = None
        calls = 0

        def load_case(self, finalized: FinalizedRealObservation):
            self.calls += 1
            raise AssertionError("gold must stay unavailable before observation")

    observer = Observer()
    gold = Gold()
    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=observer,
        gold=gold,
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert report.readiness_status == "not_ready"
    assert report.production_observation_status == "blocked"
    assert report.case_counts.readiness_blocked == 1
    assert observer.calls == 0
    assert gold.calls == 0
    replay = await runner.run(loaded=loaded, manifest=manifest)
    assert replay.run.run_id == report.run.run_id
    assert replay.model_dump(mode="json") == report.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_status", ("observed", "partial"))
async def test_real_benchmark_finalizes_observation_before_joining_gold(
    monkeypatch: pytest.MonkeyPatch,
    observer_status: str,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )
    events: list[str] = []

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(
                CorpusReadinessStatus.READY,
                CorpusReadinessReason.READY,
            )

    class Observer:
        async def observe(self, *, resolution):
            events.append("observer")
            assert not hasattr(resolution.request, "expected_answer")
            synthetic_case = load_r1_dataset().cases[1].model_copy(
                update={
                    "case_id": resolution.request.case_id,
                    "question": resolution.request.question,
                    "workspace_id": resolution.request.workspace_id,
                    "environment_id": resolution.request.environment_id,
                    "principal_id": resolution.request.principal_id,
                    "available_revision_ids": resolution.request.available_revision_ids,
                }
            )
            return runner_module.RealObserverResult(
                status=observer_status,
                observation=_safe_observation(synthetic_case),
            )

    class Gold:
        dataset_version = REAL_R1_DATASET_VERSION
        dataset_sha256 = REAL_R1_DATASET_DIGEST
        population = None

        def load_case(self, finalized):
            events.append("gold")
            assert finalized.is_finalized
            return loaded.dataset.cases[0]

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=Gold(),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert events == ["observer", "gold"]
    assert report.production_observation_status == (
        "completed" if observer_status == "observed" else "partial"
    )
    assert getattr(report.case_counts, observer_status) == 1
    assert report.evaluation is not None
    assert report.run.workspace_id == WORKSPACE_ID
    assert report.run.environment_id == ENVIRONMENT_ID
    assert report.run.runtime_pins.model_version == "model:v1"


@pytest.mark.asyncio
async def test_real_benchmark_readiness_failure_prevents_observer_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            raise RuntimeError("private adapter failure")

    class Observer:
        async def observe(self, **_kwargs):
            raise AssertionError("unavailable readiness must block observation")

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=SimpleNamespace(population=None),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert report.readiness_status == "unavailable"
    assert report.production_observation_status == "blocked"
    assert report.case_counts.readiness_blocked == 1


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("workspace_id", UUID("00000000-0000-0000-0000-000000000999")),
        ("environment_id", UUID("00000000-0000-0000-0000-000000000998")),
        ("projection_kind", "other-projection"),
    ),
)
@pytest.mark.asyncio
async def test_readiness_report_scope_mismatch_blocks_observation(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    wrong_value: object,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            report = _readiness_report(
                CorpusReadinessStatus.READY,
                CorpusReadinessReason.READY,
            )
            return report.model_copy(update={field: wrong_value})

    class Observer:
        async def observe(self, **_kwargs):
            raise AssertionError("mismatched readiness report must not release an observation")

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=SimpleNamespace(population=None),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert report.readiness_status == "unavailable"
    assert report.readiness_reason_counts == {"readiness_unavailable": 1}
    assert report.case_counts.readiness_blocked == 1


@pytest.mark.asyncio
async def test_invalid_finalized_observation_never_releases_gold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(
                CorpusReadinessStatus.READY,
                CorpusReadinessReason.READY,
            )

    class Observer:
        async def observe(self, *, resolution):
            synthetic_case = load_r1_dataset().cases[1].model_copy(
                update={
                    "case_id": resolution.request.case_id,
                    "question": resolution.request.question,
                    "workspace_id": resolution.request.workspace_id,
                    "environment_id": resolution.request.environment_id,
                    "principal_id": resolution.request.principal_id,
                    "available_revision_ids": resolution.request.available_revision_ids,
                }
            )
            observation = _safe_observation(synthetic_case).model_copy(
                update={"case_id": "case:other"}
            )
            return runner_module.RealObserverResult(
                status="observed",
                observation=observation,
            )

    class Gold:
        population = None
        calls = 0

        def load_case(self, finalized):
            self.calls += 1
            raise AssertionError("invalid observation must not release gold")

    gold = Gold()
    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=gold,
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert report.case_counts.observed == 1
    assert report.evaluation_status == "failed"
    assert report.evaluation is not None
    assert report.evaluation.evaluation_status == "failed"
    assert gold.calls == 0


@pytest.mark.parametrize("status", ("unavailable", "denied", "failed"))
@pytest.mark.asyncio
async def test_real_benchmark_preserves_safe_unavailable_dispositions_without_scoring(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(
                CorpusReadinessStatus.READY,
                CorpusReadinessReason.READY,
            )

    class Observer:
        async def observe(self, **_kwargs):
            return runner_module.RealObserverResult(
                status=status,
                failure_category="safe_fixture_reason",
            )

    class Gold:
        population = None

        def load_case(self, _finalized):
            raise AssertionError("non-observations must never be scored against gold")

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=Gold(),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert getattr(report.case_counts, status) == 1
    assert report.evaluation_status == "partial"
    assert report.evaluation is None


@pytest.mark.asyncio
async def test_real_benchmark_continues_after_one_observer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, first_resolution = _scenario()
    first_case = loaded.dataset.cases[0]
    second_case = RealQACase.model_validate(
        {
            **first_case.model_dump(),
            "case_id": "case:opaque-4",
            "question": "Another approved question?",
        }
    )
    dataset = RealQADataset.model_validate(
        {**loaded.dataset.model_dump(), "cases": (first_case, second_case)}
    )
    validation_report = loaded.validation_report.model_copy(
        update={"case_count": 2, "abstained_case_count": 2}
    )
    loaded = LoadedRealDataset.model_validate(
        {"dataset": dataset, "validation_report": validation_report}
    )
    second_request = first_resolution.request.model_copy(
        update={"case_id": second_case.case_id, "question": second_case.question}
    )
    second_resolution = first_resolution.model_copy(update={"request": second_request})
    resolutions = {
        first_case.case_id: first_resolution,
        second_case.case_id: second_resolution,
    }
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, *, case, **_kwargs):
            return resolutions[case.case_id]

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(
                CorpusReadinessStatus.READY,
                CorpusReadinessReason.READY,
            )

    class Observer:
        def __init__(self):
            self.calls: list[str] = []

        async def observe(self, *, resolution):
            self.calls.append(resolution.request.case_id)
            if len(self.calls) == 1:
                raise RuntimeError("private observer exception")
            return runner_module.RealObserverResult(
                status="unavailable",
                failure_category="safe_fixture_reason",
            )

    observer = Observer()
    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=observer,
        gold=SimpleNamespace(population=None),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert observer.calls == [first_case.case_id, second_case.case_id]
    assert report.case_counts.total == 2
    assert report.case_counts.failed == 1
    assert report.case_counts.unavailable == 1
    assert report.production_observation_status == "partial"
    assert report.evaluation_status == "failed"


@pytest.mark.asyncio
async def test_real_benchmark_conflicting_source_mapping_blocks_all_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, first_resolution = _scenario()
    first_case = loaded.dataset.cases[0]
    second_case = RealQACase.model_validate(
        {
            **first_case.model_dump(),
            "case_id": "case:opaque-4",
            "question": "Another approved question?",
        }
    )
    dataset = RealQADataset.model_validate(
        {**loaded.dataset.model_dump(), "cases": (first_case, second_case)}
    )
    validation_report = loaded.validation_report.model_copy(
        update={"case_count": 2, "abstained_case_count": 2}
    )
    loaded = LoadedRealDataset.model_validate(
        {"dataset": dataset, "validation_report": validation_report}
    )
    original_binding = first_resolution.bindings[0]
    conflicting_binding = RealRuntimeRevisionBinding.model_validate(
        {
            **original_binding.model_dump(),
            "source_object_id": UUID("00000000-0000-0000-0000-000000000402"),
            "source_revision_id": UUID("00000000-0000-0000-0000-000000000502"),
        }
    )
    second_request = first_resolution.request.model_copy(
        update={
            "case_id": second_case.case_id,
            "question": second_case.question,
            "available_revision_ids": (conflicting_binding.source_revision_id,),
        }
    )
    second_resolution = RealRuntimeCaseResolution.model_validate(
        {
            **first_resolution.model_dump(),
            "request": second_request,
            "bindings": (conflicting_binding,),
            "binding_sha256": runtime_revision_bindings_digest(
                (conflicting_binding,)
            ),
        }
    )
    resolutions = {
        first_case.case_id: first_resolution,
        second_case.case_id: second_resolution,
    }
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, *, case, **_kwargs):
            return resolutions[case.case_id]

    class Readiness:
        async def inspect(self, **_kwargs):
            raise AssertionError("conflicting mappings must not reach readiness")

    class Observer:
        async def observe(self, **_kwargs):
            raise AssertionError("conflicting mappings must not reach observer")

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=SimpleNamespace(population=None),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=WORKSPACE_ID,
        environment_selector=ENVIRONMENT_ID,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert report.case_counts.total == 2
    assert report.case_counts.mapping_unavailable == 2
    assert report.failure_reason_counts == {"mapping_conflict": 1}
    assert report.readiness_status == "not_checked"
