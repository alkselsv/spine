"""Fail-closed qualification gate for the explicitly selected real R1 run."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.application.production_qa_composition import ProductionQAACompositionEvidence


R1_BENCHMARK_QUALIFICATION_SCHEMA = "r1-benchmark-qualification:v1"
_TRUSTED_PREFLIGHT_ISSUER = object()


class RuntimePrerequisite(str, Enum):
    POSTGRESQL = "postgresql"
    TEMPORAL = "temporal"
    TRUSTED_AUTHENTICATION = "trusted_authentication"
    AUTHORIZATION_SCOPE = "authorization_scope"
    PRODUCTION_COMPOSITION = "production_composition"
    CAPTURE_CHANNEL = "capture_channel"
    CANONICAL_CORPUS_PERMISSION = "canonical_corpus_permission"
    CANONICAL_CORPUS = "canonical_corpus"
    ACTIVE_PROJECTION = "active_projection"
    APPROVED_DATASET = "approved_dataset"
    LOCAL_INPUTS = "local_inputs"


class PrerequisiteState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class RuntimePrerequisiteCheck(DefinitionModel):
    prerequisite: RuntimePrerequisite
    state: PrerequisiteState


class RealBenchmarkPreflight(DefinitionModel):
    """Content-free readiness snapshot produced by the local composition."""

    schema_version: Literal["r1-benchmark-preflight:v1"] = "r1-benchmark-preflight:v1"
    workspace_id: UUID
    environment_id: UUID
    prerequisites: tuple[RuntimePrerequisiteCheck, ...]

    @model_validator(mode="after")
    def validate_unique_prerequisites(self) -> "RealBenchmarkPreflight":
        names = tuple(item.prerequisite for item in self.prerequisites)
        if len(names) != len(set(names)):
            raise ValueError("preflight prerequisite names must be unique")
        return self


class RealBenchmarkQualification(DefinitionModel):
    """Safe qualification decision; it contains no case or source identifiers."""

    schema_version: Literal["r1-benchmark-qualification:v1"] = (
        R1_BENCHMARK_QUALIFICATION_SCHEMA
    )
    workspace_id: UUID
    environment_id: UUID
    outcome: Literal["ready", "qualified", "denied", "unavailable", "rejected"]
    structural_validation_status: Literal["not_run", "passed", "failed"] = "not_run"
    production_observation_status: Literal[
        "not_run", "completed", "partial", "blocked", "unavailable", "denied", "failed"
    ] = "not_run"
    evaluation_status: Literal["not_run", "completed", "partial", "failed"] = "not_run"
    release_qualification_status: Literal["not_run"] = "not_run"
    real_qualification_status: Literal["not_qualified", "qualified"] = "not_qualified"
    reason_codes: tuple[str, ...] = Field(default=(), max_length=len(RuntimePrerequisite) + 2)


class RealBenchmarkPreflightPort(Protocol):
    async def inspect(self) -> RealBenchmarkPreflight: ...


class TrustedRealBenchmarkPreflight:
    """Bind operator preflight checks to one verified production composition."""

    def __init__(
        self,
        *,
        inspector: RealBenchmarkPreflightPort,
        production_evidence: ProductionQAACompositionEvidence,
        _issuer: object = None,
    ) -> None:
        if _issuer is not _TRUSTED_PREFLIGHT_ISSUER:
            raise TypeError("trusted preflight must be issued by the production composition")
        if type(production_evidence) is not ProductionQAACompositionEvidence:
            raise TypeError("production preflight requires verified composition evidence")
        if getattr(inspector, "is_fake", None) is not False:
            raise TypeError(
                "trusted preflight inspectors must explicitly declare is_fake=False"
            )
        self._inspector = inspector
        self.production_evidence = production_evidence

    async def inspect(self) -> RealBenchmarkPreflight:
        return await self._inspector.inspect()


def _issue_trusted_real_benchmark_preflight(
    *,
    inspector: RealBenchmarkPreflightPort,
    production_evidence: ProductionQAACompositionEvidence,
) -> TrustedRealBenchmarkPreflight:
    """Issue the wrapper only from a trusted local production composition factory."""
    return TrustedRealBenchmarkPreflight(
        inspector=inspector,
        production_evidence=production_evidence,
        _issuer=_TRUSTED_PREFLIGHT_ISSUER,
    )


class RealBenchmarkQualificationGate:
    """Authorize observation only after every named real-runtime prerequisite is ready."""

    _REQUIRED = frozenset(RuntimePrerequisite)

    def evaluate(
        self,
        *,
        preflight: RealBenchmarkPreflight,
        dataset_kind: Literal["real", "synthetic"],
        production_composition: bool,
    ) -> RealBenchmarkQualification:
        if type(preflight) is not RealBenchmarkPreflight:
            raise TypeError("qualification requires a validated trusted preflight")
        scope = {
            "workspace_id": preflight.workspace_id,
            "environment_id": preflight.environment_id,
        }
        states = {item.prerequisite: item.state for item in preflight.prerequisites}
        reasons: list[str] = []
        denied = False
        for prerequisite in sorted(self._REQUIRED, key=lambda item: item.value):
            state = states.get(prerequisite)
            if state is None or state is PrerequisiteState.MISSING:
                reasons.append(f"missing:{prerequisite.value}")
            elif state is PrerequisiteState.DENIED:
                reasons.append(f"denied:{prerequisite.value}")
                denied = True
            elif state is PrerequisiteState.UNAVAILABLE:
                reasons.append(f"unavailable:{prerequisite.value}")

        if denied:
            return RealBenchmarkQualification(
                **scope, outcome="denied", reason_codes=tuple(reasons)
            )
        if dataset_kind != "real":
            return RealBenchmarkQualification(
                **scope, outcome="rejected", reason_codes=("real_dataset_not_selected",)
            )
        if reasons:
            return RealBenchmarkQualification(
                **scope, outcome="unavailable", reason_codes=tuple(reasons)
            )
        if not production_composition:
            return RealBenchmarkQualification(
                **scope,
                outcome="rejected",
                reason_codes=("production_composition_required",),
            )
        return RealBenchmarkQualification(**scope, outcome="ready")


class _BenchmarkRunner(Protocol):
    @property
    def supports_real_qualification(self) -> bool: ...

    @property
    def production_qualification_evidence(self) -> ProductionQAACompositionEvidence | None: ...

    async def run(self, *, loaded: object, manifest: object) -> object: ...


class RealBenchmarkQualificationRuntime:
    """Explicit CLI seam that preflights before invoking the integrated runner."""

    def __init__(
        self,
        *,
        runner: _BenchmarkRunner,
        preflight: RealBenchmarkPreflightPort,
    ) -> None:
        from spine.evals.real_benchmark_runner import RealBenchmarkRunner

        if type(runner) is not RealBenchmarkRunner:
            raise TypeError("qualification runtime requires the integrated benchmark runner")
        self._runner = runner
        self._preflight = preflight

    async def run(
        self, *, loaded: object, manifest: object
    ) -> tuple[RealBenchmarkQualification | None, object | None]:
        try:
            preflight = await self._preflight.inspect()
        except Exception:
            return None, None
        if type(preflight) is not RealBenchmarkPreflight:
            return None, None
        decision = RealBenchmarkQualificationGate().evaluate(
            preflight=preflight,
            dataset_kind="real",
            production_composition=self._runner.supports_real_qualification,
        )
        if decision.outcome != "ready":
            return decision, None
        if (
            type(self._preflight) is not TrustedRealBenchmarkPreflight
            or self._runner.production_qualification_evidence is None
            or not self._preflight.production_evidence.matches_evidence(
                self._runner.production_qualification_evidence
            )
        ):
            return (
                decision.model_copy(
                    update={
                        "outcome": "rejected",
                        "reason_codes": ("trusted_preflight_required",),
                    }
                ),
                None,
            )
        try:
            report = await self._runner.run(loaded=loaded, manifest=manifest)
        except Exception:
            return (
                decision.model_copy(
                    update={
                        "outcome": "unavailable",
                        "production_observation_status": "failed",
                        "reason_codes": ("benchmark_execution_failed",),
                    }
                ),
                None,
            )
        observation_status = getattr(report, "production_observation_status", "failed")
        completed = observation_status == "completed"
        return (
            decision.model_copy(
                update={
                    "outcome": "qualified" if completed else "unavailable",
                    "structural_validation_status": "passed",
                    "production_observation_status": observation_status,
                    "evaluation_status": getattr(report, "evaluation_status", "not_run"),
                    "real_qualification_status": "qualified" if completed else "not_qualified",
                    "reason_codes": () if completed else ("observation_incomplete",),
                }
            ),
            report,
        )


__all__ = [
    "PrerequisiteState",
    "R1_BENCHMARK_QUALIFICATION_SCHEMA",
    "RealBenchmarkPreflight",
    "RealBenchmarkPreflightPort",
    "RealBenchmarkQualification",
    "RealBenchmarkQualificationGate",
    "RealBenchmarkQualificationRuntime",
    "TrustedRealBenchmarkPreflight",
    "RuntimePrerequisiteCheck",
    "RuntimePrerequisite",
]
