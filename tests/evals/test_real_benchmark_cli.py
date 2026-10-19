from __future__ import annotations

import json
from uuid import UUID

import pytest

from spine.evals import real_benchmark_cli
from spine.evals.real_benchmark_runner import (
    REAL_BENCHMARK_REPORT_SCHEMA,
    RealBenchmarkCaseCounts,
    RealBenchmarkReport,
    RealBenchmarkRun,
    RuntimeVersionPins,
)
from spine.evals.real_qa_r1 import RealDatasetValidationError


def test_real_benchmark_cli_requires_explicit_real_inputs() -> None:
    with pytest.raises(SystemExit):
        real_benchmark_cli.main(["--dataset-kind", "real"])


def test_real_benchmark_cli_integrity_failure_never_loads_synthetic(
    tmp_path, monkeypatch, capsys
) -> None:
    dataset_dir = tmp_path / "dataset"
    source_root = tmp_path / "sources"
    dataset_dir.mkdir()
    source_root.mkdir()
    manifest = tmp_path / "mapping.json"
    manifest.write_text("{}", encoding="utf-8")
    called: list[str] = []

    def reject_real(*_args, **_kwargs):
        raise RealDatasetValidationError("real input integrity failure")

    monkeypatch.setattr(real_benchmark_cli, "load_real_r1_dataset", reject_real)
    monkeypatch.setattr(
        "spine.evals.qa_r1.load_r1_dataset",
        lambda: called.append("synthetic"),
    )
    result = real_benchmark_cli.main(
        [
            "--dataset-kind", "real",
            "--dataset-dir", str(dataset_dir),
            "--source-root", str(source_root),
            "--mapping-manifest", str(manifest),
            "--workspace-id", str(UUID(int=1)),
            "--environment-id", str(UUID(int=2)),
            "--composition", "unused:factory",
        ]
    )

    assert result == 2
    assert called == []
    assert "real benchmark could not run" in capsys.readouterr().err


def test_real_benchmark_report_serialization_has_only_metadata() -> None:
    run = RealBenchmarkRun(
        run_id="a" * 64,
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        dataset_version="r1-real-v1",
        dataset_sha256="b" * 64,
        mapping_sha256="c" * 64,
        runtime_pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="application:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        evaluator_version="v1",
    )
    report = RealBenchmarkReport(
        run=run,
        structural_validation_status="passed",
        production_observation_status="blocked",
        evaluation_status="not_run",
        readiness_status="not_ready",
        readiness_reason_counts={"projection_pending": 3},
        case_counts=RealBenchmarkCaseCounts(
            total=3,
            observed=0,
            partial=0,
            unavailable=0,
            denied=0,
            failed=0,
            readiness_blocked=3,
            mapping_unavailable=0,
        ),
    )

    serialized = json.dumps(report.model_dump(mode="json"))

    assert report.schema_version == REAL_BENCHMARK_REPORT_SCHEMA
    assert '"structural_validation_status": "passed"' in serialized
    assert '"production_observation_status": "blocked"' in serialized
    assert '"evaluation_status": "not_run"' in serialized
    assert '"release_qualification_status": "not_run"' in serialized
    for forbidden in ("case_id", "source_id", "filename", "question", "answer", "evidence_text"):
        assert forbidden not in serialized


def test_real_benchmark_cli_reports_exact_missing_preflight_prerequisite(
    tmp_path, monkeypatch, capsys
) -> None:
    from tests.evals.test_real_benchmark_runner import _scenario
    from spine.evals.real_benchmark_qualification import (
        PrerequisiteState,
        RealBenchmarkPreflight,
        RealBenchmarkQualificationRuntime,
        RuntimePrerequisiteCheck,
        RuntimePrerequisite,
    )
    from spine.evals.real_benchmark_runner import RealBenchmarkRunner

    loaded, manifest, _resolution = _scenario()
    dataset_dir = tmp_path / "dataset"
    source_root = tmp_path / "sources"
    dataset_dir.mkdir()
    source_root.mkdir()
    manifest_path = tmp_path / "mapping.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    output = tmp_path / "report.json"

    class Mapping:
        async def resolve(self, **_kwargs):
            raise AssertionError("preflight must run before mapping")

    class Readiness:
        async def inspect(self, **_kwargs):
            raise AssertionError("preflight must run before readiness")

    class Preflight:
        async def inspect(self):
            return RealBenchmarkPreflight(
                workspace_id=UUID(int=1),
                environment_id=UUID(int=2),
                prerequisites=tuple(
                    RuntimePrerequisiteCheck(
                        prerequisite=prerequisite,
                        state=PrerequisiteState.READY,
                    )
                    for prerequisite in RuntimePrerequisite
                    if prerequisite is not RuntimePrerequisite.POSTGRESQL
                ),
            )

    def runner_factory(dataset, workspace_id, environment_id):
        assert dataset is loaded
        runner = RealBenchmarkRunner(
            mapping=Mapping(),
            readiness=Readiness(),
            trusted_persistence_context=object(),
            observer=object(),
            gold=type("Gold", (), {"population": None})(),
            pins=RuntimeVersionPins(
                observer_seam_version="observer:v1",
                application_version="app:v1",
                projection_version="projection:v1",
                model_version="model:v1",
                prompt_version="prompt:v1",
                validation_policy_version="policy:v1",
                projection_kind="qa",
            ),
            workspace_selector=workspace_id,
            environment_selector=environment_id,
        )
        return RealBenchmarkQualificationRuntime(runner=runner, preflight=Preflight())

    monkeypatch.setattr(real_benchmark_cli, "load_real_r1_dataset", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(real_benchmark_cli, "_load_factory", lambda _spec: runner_factory)
    result = real_benchmark_cli.main(
        [
            "--dataset-kind", "real",
            "--dataset-dir", str(dataset_dir),
            "--source-root", str(source_root),
            "--mapping-manifest", str(manifest_path),
            "--workspace-id", str(UUID(int=1)),
            "--environment-id", str(UUID(int=2)),
            "--composition", "fixture:factory",
            "--output", str(output),
        ]
    )

    assert result == 2
    assert not output.exists()
    failure = json.loads(capsys.readouterr().err)
    assert failure["schema_version"] == "r1-benchmark-qualification:v1"
    assert failure["reason_codes"] == ["missing:postgresql"]
    assert failure["production_observation_status"] == "not_run"
