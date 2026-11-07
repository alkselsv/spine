from __future__ import annotations

import pytest

from spine.evals import answer_quality_benchmark_runner as runner_module
from spine.evals.answer_quality_benchmark_runner import AnswerQualityBenchmarkRunner
from spine.evals.answer_quality_dataset import (
    AnswerQualityCase,
    AnswerQualityDataset,
    AnswerQualityDatasetValidationReport,
    LoadedAnswerQualityDataset,
)
from spine.evaluations.answer_quality import (
    ValidatedAnswerQualityGoldProvider,
)
from spine.evals.real_benchmark_runner import RealObserverResult, RuntimeVersionPins
from spine.evals.real_benchmark_qualification import RealBenchmarkQualificationRuntime
from spine.evals.real_qa_r1 import (
    runtime_mapping_manifest_digest,
    runtime_revision_bindings_digest,
)
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_contract import _safe_observation
from tests.evals.test_real_benchmark_runner import (
    ENVIRONMENT_ID,
    WORKSPACE_ID,
    _readiness_report,
    _readiness_request,
    _scenario,
)
from spine.application.corpus_readiness import (
    CorpusReadinessReason,
    CorpusReadinessStatus,
)


def _answer_quality_inputs():
    _, original_manifest, original_resolution = _scenario()
    version = "answer-quality-test"
    digest = "d" * 64
    manifest = original_manifest.model_copy(
        update={"dataset_version": version, "dataset_sha256": digest}
    )
    manifest_digest = runtime_mapping_manifest_digest(manifest)
    bindings = tuple(
        item.model_copy(update={"manifest_sha256": manifest_digest})
        for item in original_resolution.bindings
    )
    resolution = original_resolution.model_copy(
        update={
            "manifest_sha256": manifest_digest,
            "binding_sha256": runtime_revision_bindings_digest(bindings),
            "bindings": bindings,
        }
    )
    case = AnswerQualityCase(
        case_id=resolution.request.case_id,
        question=resolution.request.question,
        expected_answer="The approved answer.",
        document_refs=("policy.txt",),
        source_ids=(bindings[0].dataset_source_id,),
        source_revision_ids=(bindings[0].dataset_revision_id,),
        retrieval_source_ids=(bindings[0].dataset_source_id,),
        retrieval_source_revision_ids=(bindings[0].dataset_revision_id,),
    )
    dataset = AnswerQualityDataset(version=version, sha256=digest, cases=(case,))
    loaded = LoadedAnswerQualityDataset(
        dataset=dataset,
        validation_report=AnswerQualityDatasetValidationReport(
            dataset_version=version,
            dataset_sha256=digest,
            case_count=1,
            source_bound_case_count=1,
            unscoped_case_count=0,
            unique_document_count=1,
            source_files_verified=1,
        ),
    )
    return loaded, manifest, resolution


def _runner(*, mapping, readiness, observer, loaded):
    class Judge:
        async def assess(self, **_kwargs):
            raise AssertionError("answer judge must not be called")

    return AnswerQualityBenchmarkRunner(
        mapping=mapping,
        readiness=readiness,
        trusted_persistence_context=object(),
        observer=observer,
        gold=ValidatedAnswerQualityGoldProvider(loaded),
        judge=Judge(),
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


@pytest.mark.asyncio
async def test_answer_quality_runner_observes_without_gold_then_reports_unavailable_evidence_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, original_manifest, original_resolution = _scenario()
    version = "answer-quality-test"
    digest = "d" * 64
    manifest = original_manifest.model_copy(
        update={"dataset_version": version, "dataset_sha256": digest}
    )
    manifest_digest = runtime_mapping_manifest_digest(manifest)
    bindings = tuple(
        item.model_copy(update={"manifest_sha256": manifest_digest})
        for item in original_resolution.bindings
    )
    resolution = original_resolution.model_copy(
        update={
            "manifest_sha256": manifest_digest,
            "binding_sha256": runtime_revision_bindings_digest(bindings),
            "bindings": bindings,
        }
    )
    case = AnswerQualityCase(
        case_id=resolution.request.case_id,
        question=resolution.request.question,
        expected_answer="The approved answer.",
        document_refs=("policy.txt",),
        source_ids=(bindings[0].dataset_source_id,),
        source_revision_ids=(bindings[0].dataset_revision_id,),
        retrieval_source_ids=(bindings[0].dataset_source_id,),
        retrieval_source_revision_ids=(bindings[0].dataset_revision_id,),
    )
    dataset = AnswerQualityDataset(version=version, sha256=digest, cases=(case,))
    loaded = LoadedAnswerQualityDataset(
        dataset=dataset,
        validation_report=AnswerQualityDatasetValidationReport(
            dataset_version=version,
            dataset_sha256=digest,
            case_count=1,
            source_bound_case_count=1,
            unscoped_case_count=0,
            unique_document_count=1,
            source_files_verified=1,
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda values, *, projection_kind: _readiness_request(
            values, projection_kind=projection_kind
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
            assert not hasattr(resolution.request, "expected_answer")
            source_case = load_r1_dataset().cases[1].model_copy(
                update={
                    "case_id": resolution.request.case_id,
                    "question": resolution.request.question,
                    "workspace_id": resolution.request.workspace_id,
                    "environment_id": resolution.request.environment_id,
                    "principal_id": resolution.request.principal_id,
                    "available_revision_ids": resolution.request.available_revision_ids,
                }
            )
            return RealObserverResult(
                status="observed",
                observation=_safe_observation(source_case),
            )

    class Judge:
        async def assess(self, **_kwargs):
            raise AssertionError("abstained observations must not call the answer judge")

    runner = AnswerQualityBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=ValidatedAnswerQualityGoldProvider(loaded),
        judge=Judge(),
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
    qualification_runtime = RealBenchmarkQualificationRuntime(
        runner=runner,
        preflight=object(),  # type: ignore[arg-type]
    )

    report = await runner.run(loaded=loaded, manifest=manifest)

    assert qualification_runtime is not None
    assert report.production_observation_status == "completed", (
        report.case_counts,
        report.failure_reason_counts,
        report.readiness_reason_counts,
    )
    assert report.evaluation_status == "completed"
    assert report.case_counts.observed == 1
    assert report.evaluation.metric("qa.answer_correctness").score == 0.0
    assert report.evaluation.metric("qa.citation_recall").status == "unavailable"


@pytest.mark.asyncio
async def test_answer_quality_runner_rejects_readiness_with_wrong_canonical_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, resolution = _answer_quality_inputs()
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda values, *, projection_kind: _readiness_request(
            values, projection_kind=projection_kind
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
            revisions = tuple(
                item.model_copy(update={"source_revision_id": item.source_object_id})
                for item in report.revisions
            )
            return report.model_copy(update={"revisions": revisions})

    class Observer:
        async def observe(self, **_kwargs):
            raise AssertionError("invalid readiness must block observation")

    report = await _runner(
        mapping=Mapping(),
        readiness=Readiness(),
        observer=Observer(),
        loaded=loaded,
    ).run(loaded=loaded, manifest=manifest)

    assert report.production_observation_status == "blocked"
    assert report.readiness_status == "unavailable"
    assert report.case_counts.readiness_blocked == 1
    assert report.failure_reason_counts == {"readiness_unavailable": 1}


@pytest.mark.asyncio
async def test_answer_quality_runner_rejects_every_case_affected_by_mapping_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, first = _answer_quality_inputs()
    first_binding = first.bindings[0]
    second_binding = first_binding.model_copy(
        update={"source_revision_id": first_binding.source_object_id}
    )
    second_bindings = (second_binding,)
    second_request = first.request.model_copy(
        update={
            "case_id": "second-case",
            "question": "A second question?",
            "available_revision_ids": (second_binding.source_revision_id,),
        }
    )
    second = first.model_copy(
        update={
            "request": second_request,
            "bindings": second_bindings,
            "binding_sha256": runtime_revision_bindings_digest(second_bindings),
        }
    )
    second_case = loaded.dataset.cases[0].model_copy(
        update={"case_id": "second-case", "question": "A second question?"}
    )
    dataset = loaded.dataset.model_copy(
        update={"cases": (loaded.dataset.cases[0], second_case)}
    )
    loaded = LoadedAnswerQualityDataset(
        dataset=dataset,
        validation_report=loaded.validation_report.model_copy(
            update={"case_count": 2, "source_bound_case_count": 2}
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "corpus_readiness_request_from_bindings",
        lambda *_args, **_kwargs: pytest.fail("conflicts must block readiness"),
    )

    class Mapping:
        async def resolve(self, *, case, **_kwargs):
            return first if case.case_id == first.request.case_id else second

    class Readiness:
        async def inspect(self, **_kwargs):
            raise AssertionError("conflicting mappings must block readiness")

    class Observer:
        async def observe(self, **_kwargs):
            raise AssertionError("conflicting mappings must block observation")

    report = await _runner(
        mapping=Mapping(),
        readiness=Readiness(),
        observer=Observer(),
        loaded=loaded,
    ).run(loaded=loaded, manifest=manifest)

    assert report.production_observation_status == "blocked"
    assert report.case_counts.mapping_unavailable == 2
    assert report.failure_reason_counts == {"mapping_conflict": 1}
