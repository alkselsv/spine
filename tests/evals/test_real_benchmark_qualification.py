from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.production_qa_composition import (
    _issue_production_qa_composition_evidence,
)
from spine.evals.durable_qa_observer import DurableProductionQAAFlow
from spine.evals.real_benchmark_production import RealBenchmarkProductionObserver
from spine.evals.real_benchmark_qualification import (
    PrerequisiteState,
    RealBenchmarkPreflight,
    RealBenchmarkQualificationGate,
    RealBenchmarkQualificationRuntime,
    TrustedRealBenchmarkPreflight,
    RuntimePrerequisiteCheck,
    RuntimePrerequisite,
    _issue_trusted_real_benchmark_preflight,
)
from spine.evals.real_benchmark_runner import RealBenchmarkRunner, RuntimeVersionPins


def _ready_preflight() -> RealBenchmarkPreflight:
    return RealBenchmarkPreflight(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        prerequisites=tuple(
            RuntimePrerequisiteCheck(
                prerequisite=prerequisite,
                state=PrerequisiteState.READY,
            )
            for prerequisite in RuntimePrerequisite
        ),
    )


def _runner() -> RealBenchmarkRunner:
    class Mapping:
        async def resolve(self, **_kwargs):
            raise AssertionError("preflight rejection must stop before mapping")

    class Readiness:
        async def inspect(self, **_kwargs):
            raise AssertionError("preflight rejection must stop before readiness")

    return RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=SimpleNamespace(),
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
        workspace_selector=UUID(int=1),
        environment_selector=UUID(int=2),
    )


@pytest.mark.parametrize("missing", list(RuntimePrerequisite))
def test_missing_runtime_prerequisite_blocks_with_exact_safe_reason(missing) -> None:
    prerequisites = tuple(
        RuntimePrerequisiteCheck(
            prerequisite=item.prerequisite,
            state=(PrerequisiteState.MISSING if item.prerequisite is missing else item.state),
        )
        for item in _ready_preflight().prerequisites
    )

    result = RealBenchmarkQualificationGate().evaluate(
        preflight=RealBenchmarkPreflight(
            workspace_id=UUID(int=1),
            environment_id=UUID(int=2),
            prerequisites=prerequisites,
        ),
        dataset_kind="real",
        production_composition=True,
    )

    assert result.outcome == "unavailable"
    assert result.reason_codes == (f"missing:{missing.value}",)
    assert result.production_observation_status == "not_run"
    assert result.evaluation_status == "not_run"
    assert result.release_qualification_status == "not_run"
    assert result.real_qualification_status == "not_qualified"


def test_unauthorized_scope_is_denied_before_runner_invocation() -> None:
    prerequisites = tuple(
        RuntimePrerequisiteCheck(
            prerequisite=item.prerequisite,
            state=(
                PrerequisiteState.DENIED
                if item.prerequisite is RuntimePrerequisite.AUTHORIZATION_SCOPE
                else item.state
            ),
        )
        for item in _ready_preflight().prerequisites
    )

    class Preflight:
        async def inspect(self):
            return RealBenchmarkPreflight(
                workspace_id=UUID(int=1),
                environment_id=UUID(int=2),
                prerequisites=prerequisites,
            )

    runtime = RealBenchmarkQualificationRuntime(runner=_runner(), preflight=Preflight())
    result, report = asyncio.run(runtime.run(loaded=object(), manifest=object()))

    assert result.outcome == "denied"
    assert result.reason_codes == ("denied:authorization_scope",)
    assert report is None


def test_fake_composition_cannot_enter_real_qualification() -> None:
    class Preflight:
        async def inspect(self):
            return _ready_preflight()

    result, report = asyncio.run(
        RealBenchmarkQualificationRuntime(
            runner=_runner(), preflight=Preflight()
        ).run(loaded=object(), manifest=object())
    )

    assert result.outcome == "rejected"
    assert result.reason_codes == ("production_composition_required",)
    assert report is None


def test_ready_fake_preflight_cannot_enable_real_qualification(monkeypatch) -> None:
    evidence = _issue_production_qa_composition_evidence(object(), object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        RealBenchmarkRunner,
        "supports_real_qualification",
        property(lambda _runner: True),
    )
    monkeypatch.setattr(
        RealBenchmarkRunner,
        "production_qualification_evidence",
        property(lambda _runner: evidence),
    )

    class Preflight:
        async def inspect(self):
            return _ready_preflight()

    result, report = asyncio.run(
        RealBenchmarkQualificationRuntime(
            runner=_runner(), preflight=Preflight()
        ).run(loaded=object(), manifest=object())
    )

    assert result.outcome == "rejected"
    assert result.reason_codes == ("trusted_preflight_required",)
    assert report is None


def test_trusted_preflight_issuer_requires_composition_evidence() -> None:
    class Preflight:
        async def inspect(self):
            return _ready_preflight()

    with pytest.raises(TypeError, match="verified composition evidence"):
        _issue_trusted_real_benchmark_preflight(
            inspector=Preflight(),
            production_evidence=object(),  # type: ignore[arg-type]
        )


def test_trusted_preflight_rejects_explicit_fake_inspector() -> None:
    class FakePreflight:
        is_fake = True

        async def inspect(self):
            return _ready_preflight()

    evidence = _issue_production_qa_composition_evidence(object(), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is_fake=False"):
        _issue_trusted_real_benchmark_preflight(
            inspector=FakePreflight(),
            production_evidence=evidence,
        )


def test_trusted_preflight_rejects_unattested_inspector() -> None:
    class UnattestedPreflight:
        async def inspect(self):
            return _ready_preflight()

    evidence = _issue_production_qa_composition_evidence(object(), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is_fake=False"):
        _issue_trusted_real_benchmark_preflight(
            inspector=UnattestedPreflight(),
            production_evidence=evidence,
        )


def test_trusted_preflight_cannot_be_constructed_by_a_caller() -> None:
    class ProductionPreflight:
        is_fake = False

        async def inspect(self):
            return _ready_preflight()

    evidence = _issue_production_qa_composition_evidence(object(), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="issued by the production composition"):
        TrustedRealBenchmarkPreflight(
            inspector=ProductionPreflight(),
            production_evidence=evidence,
        )


def test_durable_flow_with_fake_ports_cannot_qualify() -> None:
    flow = DurableProductionQAAFlow(
        composition=SimpleNamespace(),
        authorization=object(),
        waiter=object(),
    )

    observer = RealBenchmarkProductionObserver(observer=flow)

    assert observer.uses_durable_production_flow is False
    assert flow.has_production_composition is False


def test_complete_production_preflight_is_ready_but_not_release_qualified() -> None:
    preflight = _ready_preflight()
    result = RealBenchmarkQualificationGate().evaluate(
        preflight=preflight,
        dataset_kind="real",
        production_composition=True,
    )

    assert result.outcome == "ready"
    assert result.real_qualification_status == "not_qualified"
    assert result.production_observation_status == "not_run"
    assert result.release_qualification_status == "not_run"
    assert result.workspace_id == UUID(int=1)
    assert result.environment_id == UUID(int=2)
    assert isinstance(preflight.prerequisites, tuple)
    with pytest.raises(ValueError):
        preflight.prerequisites[0].state = PrerequisiteState.MISSING


def test_qualification_record_contains_only_safe_codes_and_separate_statuses() -> None:
    result = RealBenchmarkQualificationGate().evaluate(
        preflight=_ready_preflight(), dataset_kind="synthetic", production_composition=False
    )
    serialized = result.model_dump_json()

    assert result.schema_version == "r1-benchmark-qualification:v1"
    assert result.outcome == "rejected"
    assert result.reason_codes == ("real_dataset_not_selected",)
    assert result.production_observation_status == "not_run"
    assert result.evaluation_status == "not_run"
    assert result.release_qualification_status == "not_run"
    for forbidden in ("question", "answer", "evidence", "filename", "case_id", "source_id"):
        assert forbidden not in serialized
