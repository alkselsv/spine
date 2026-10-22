from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.production_runtime_readiness import (
    ProductionRuntimeProfile,
    ProductionRuntimeInspection,
    RuntimeReadinessReport,
    RuntimeReadinessStatus,
    TrustedProductionRuntimePreflight,
    readiness_report,
)
from spine.application.production_qa_composition import (
    _issue_production_qa_composition_evidence,
)
from spine.evals.real_benchmark_qualification import (
    PrerequisiteState,
    RealBenchmarkPreflight,
    RuntimePrerequisite,
    RuntimePrerequisiteCheck,
    TrustedRealBenchmarkPreflight,
    _issue_trusted_real_benchmark_preflight,
)


def test_runtime_profile_requires_postgresql_17() -> None:
    with pytest.raises(ValidationError):
        _profile(postgresql_major=16)


def test_runtime_profile_requires_positive_embedding_dimensions() -> None:
    with pytest.raises(ValidationError):
        _profile(embedding_dimensions=0)


def test_runtime_profile_accepts_pinned_production_versions() -> None:

    profile = _profile()

    assert profile.postgresql_major == 17
    assert profile.temporal_namespace == "spine-r1"
    assert "secret" not in profile.model_dump_json().lower()


def test_readiness_report_separates_unverified_prerequisites() -> None:
    preflight = RealBenchmarkPreflight(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        prerequisites=tuple(
            RuntimePrerequisiteCheck(
                prerequisite=prerequisite,
                state=(
                    PrerequisiteState.READY
                    if prerequisite is RuntimePrerequisite.POSTGRESQL
                    else PrerequisiteState.UNAVAILABLE
                ),
            )
            for prerequisite in RuntimePrerequisite
        ),
    )

    report = readiness_report(
        profile=_profile(),
        observed_profile=_profile(),
        preflight=preflight,
        composition_verified=True,
        evidence_references=("qualification:issue-506",),
    )

    assert report.configuration is RuntimeReadinessStatus.READY
    assert report.reachability is RuntimeReadinessStatus.NOT_VERIFIED
    assert report.authorization is RuntimeReadinessStatus.NOT_VERIFIED
    assert report.bindings is RuntimeReadinessStatus.NOT_VERIFIED
    assert report.qualification is RuntimeReadinessStatus.BLOCKED
    assert report.evidence_references == ("qualification:issue-506",)
    serialized = report.model_dump_json()
    assert "endpoint" not in serialized
    assert "credential" not in serialized


def test_readiness_report_blocks_profile_pin_mismatch() -> None:
    profile = _profile()
    observed_profile = profile.model_copy(
        update={"api_release_identity": "api:other-release"}
    )
    preflight = RealBenchmarkPreflight(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        prerequisites=tuple(
            RuntimePrerequisiteCheck(
                prerequisite=item,
                state=PrerequisiteState.READY,
            )
            for item in RuntimePrerequisite
        ),
    )

    report = readiness_report(
        profile=profile,
        observed_profile=observed_profile,
        preflight=preflight,
        composition_verified=False,
    )

    assert report.configuration is RuntimeReadinessStatus.BLOCKED
    assert report.bindings is RuntimeReadinessStatus.BLOCKED
    assert report.qualification is RuntimeReadinessStatus.BLOCKED
    assert "mismatched:runtime_profile" in report.reason_codes


def test_trusted_runtime_inspection_returns_profile_only_when_all_checks_are_ready() -> None:
    profile = _profile()
    inspection = ProductionRuntimeInspection(
        profile=profile,
        preflight=_preflight(),
    )
    runtime = _trusted_runtime(profile=profile, inspection=inspection)

    observed = asyncio.run(runtime.inspect_runtime())

    assert observed == inspection
    assert runtime.readiness_report.qualification is RuntimeReadinessStatus.READY


@pytest.mark.parametrize("mismatch", ("profile", "readiness"))
def test_trusted_runtime_inspection_rejects_mismatch_or_incomplete_readiness(
    mismatch: str,
) -> None:
    profile = _profile()
    observed_profile = (
        profile.model_copy(update={"commit_sha": "c" * 40})
        if mismatch == "profile"
        else profile
    )
    inspection = ProductionRuntimeInspection(
        profile=observed_profile,
        preflight=_preflight(ready=mismatch != "readiness"),
    )
    runtime = _trusted_runtime(profile=profile, inspection=inspection)

    with pytest.raises(ValueError):
        asyncio.run(runtime.inspect_runtime())


def _profile(
    *, postgresql_major: int = 17, embedding_dimensions: int = 1536
) -> ProductionRuntimeProfile:
    return ProductionRuntimeProfile(
        postgresql_major=postgresql_major,
        migration_revision="20261020_issue506",
        temporal_namespace="spine-r1",
        temporal_task_queue="spine-r1-qa",
        worker_roles=("outbox", "qa", "observer"),
        authentication_mode="oidc",
        storage_profile="filesystem-v1",
        context_projection_provider="cognee:1.5.4",
        projection_backend_versions=("neo4j:5.26.32", "pgvector:0.8.7"),
        llm_model_version="provider:model-version",
        embedding_model_version="provider:embedding-version",
        embedding_dimensions=embedding_dimensions,
        web_release_identity="web:2026.10.20",
        api_release_identity="api:2026.10.20",
        commit_sha="b" * 40,
        deployment_fingerprint="a" * 64,
    )


def _preflight(*, ready: bool = True) -> RealBenchmarkPreflight:
    return RealBenchmarkPreflight(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        prerequisites=tuple(
            RuntimePrerequisiteCheck(
                prerequisite=prerequisite,
                state=(PrerequisiteState.READY if ready else PrerequisiteState.UNAVAILABLE),
            )
            for prerequisite in RuntimePrerequisite
        ),
    )


def _trusted_runtime(
    *, profile: ProductionRuntimeProfile, inspection: ProductionRuntimeInspection
) -> TrustedProductionRuntimePreflight:
    evidence = _issue_production_qa_composition_evidence(object(), object())  # type: ignore[arg-type]

    class Inspector:
        is_fake = False

        async def inspect(self) -> ProductionRuntimeInspection:
            return inspection

    inspector = Inspector()
    preflight = _issue_trusted_real_benchmark_preflight(
        inspector=inspector,
        production_evidence=evidence,
    )
    return TrustedProductionRuntimePreflight(
        profile=profile,
        preflight=preflight,
        inspector=inspector,
    )
