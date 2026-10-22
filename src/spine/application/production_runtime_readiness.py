"""Safe profile and readiness facts for the single-deployment R1 runtime."""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, field_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.evals.real_benchmark_qualification import (
    PrerequisiteState,
    RealBenchmarkPreflight,
    RuntimePrerequisite,
    RuntimePrerequisiteCheck,
    TrustedRealBenchmarkPreflight,
)

if TYPE_CHECKING:
    from spine.application.production_qa_composition import ProductionQAACompositionEvidence

_SAFE_EVIDENCE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class ProductionRuntimeProfile(DefinitionModel):
    """Version pins and non-secret identity for one configured deployment."""

    schema_version: Literal["spine-production-runtime-profile:v1"] = (
        "spine-production-runtime-profile:v1"
    )
    postgresql_major: Literal[17]
    migration_revision: VersionIdentifier
    temporal_namespace: VersionIdentifier
    temporal_task_queue: VersionIdentifier
    worker_roles: tuple[VersionIdentifier, ...] = Field(min_length=1)
    authentication_mode: Literal["local", "oidc"]
    storage_profile: VersionIdentifier
    context_projection_provider: VersionIdentifier
    projection_backend_versions: tuple[VersionIdentifier, ...] = Field(min_length=1)
    llm_model_version: VersionIdentifier
    embedding_model_version: VersionIdentifier
    embedding_dimensions: int = Field(gt=0, le=65536)
    web_release_identity: VersionIdentifier
    api_release_identity: VersionIdentifier
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("worker_roles")
    @classmethod
    def validate_worker_roles(cls, roles: tuple[str, ...]) -> tuple[str, ...]:
        if len(roles) != len(set(roles)):
            raise ValueError("runtime worker roles must be unique")
        return roles

    @field_validator("projection_backend_versions")
    @classmethod
    def validate_projection_backend_versions(
        cls, versions: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(versions) != len(set(versions)):
            raise ValueError("projection backend version pins must be unique")
        return versions


class RuntimeReadinessStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    NOT_VERIFIED = "not_verified"


class RuntimeReadinessReport(DefinitionModel):
    """Content-free readiness facts, kept separate by what they establish."""

    schema_version: Literal["spine-production-runtime-readiness:v1"] = (
        "spine-production-runtime-readiness:v1"
    )
    profile: ProductionRuntimeProfile
    configuration: RuntimeReadinessStatus
    reachability: RuntimeReadinessStatus
    authorization: RuntimeReadinessStatus
    bindings: RuntimeReadinessStatus
    qualification: RuntimeReadinessStatus
    reason_codes: tuple[VersionIdentifier, ...] = Field(
        default=(), max_length=len(RuntimePrerequisite) + 2
    )
    evidence_references: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("evidence_references")
    @classmethod
    def validate_evidence_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SAFE_EVIDENCE_REFERENCE.fullmatch(value) is None for value in values):
            raise ValueError("evidence references must be bounded safe identifiers")
        return values


class ProductionRuntimeInspection(DefinitionModel):
    """Live-observed profile and content-free prerequisite results."""

    profile: ProductionRuntimeProfile
    preflight: RealBenchmarkPreflight


class ProductionRuntimeInspector(Protocol):
    """Trusted live probe bound to one exact API/Temporal Q&A composition."""

    @property
    def is_fake(self) -> bool: ...

    @property
    def production_evidence(self) -> ProductionQAACompositionEvidence: ...

    async def inspect(self) -> ProductionRuntimeInspection: ...


class TrustedProductionRuntimePreflight:
    """Profile-bound view of the trusted checks issued by the API composition."""

    def __init__(
        self,
        *,
        profile: ProductionRuntimeProfile,
        preflight: TrustedRealBenchmarkPreflight,
        inspector: ProductionRuntimeInspector,
    ) -> None:
        if type(preflight) is not TrustedRealBenchmarkPreflight:
            raise TypeError("production readiness requires trusted composition preflight")
        if preflight._inspector is not inspector:
            raise TypeError("runtime readiness inspector does not match trusted preflight")
        self.profile = profile
        self._preflight = preflight
        self.readiness_report: RuntimeReadinessReport | None = None
        self._inspection: ProductionRuntimeInspection | None = None

    @property
    def production_evidence(self) -> ProductionQAACompositionEvidence:
        """Composition proof carried by the trusted preflight issuer."""

        return self._preflight.production_evidence

    async def inspect(self) -> RealBenchmarkPreflight:
        """Return the same content-free preflight accepted by the benchmark gate."""

        inspection = await self._preflight.inspect()
        if type(inspection) is not ProductionRuntimeInspection:
            raise TypeError("trusted runtime inspector must return an observed runtime profile")
        self._inspection = inspection
        profile_matches = inspection.profile == self.profile
        preflight = inspection.preflight
        if not profile_matches:
            checks = {
                check.prerequisite: check
                for check in preflight.prerequisites
            }
            checks[RuntimePrerequisite.PRODUCTION_COMPOSITION] = (
                RuntimePrerequisiteCheck(
                    prerequisite=RuntimePrerequisite.PRODUCTION_COMPOSITION,
                    state=PrerequisiteState.UNAVAILABLE,
                )
            )
            preflight = RealBenchmarkPreflight.model_validate(
                {
                    **preflight.model_dump(),
                    "prerequisites": tuple(checks.values()),
                }
            )
        self.readiness_report = readiness_report(
            profile=self.profile,
            observed_profile=inspection.profile,
            preflight=preflight,
            composition_verified=profile_matches,
        )
        return preflight

    async def inspect_runtime(self) -> ProductionRuntimeInspection:
        """Return an observed profile only after every trusted runtime check is ready."""
        preflight = await self.inspect()
        if self._inspection is None or self._inspection.profile != self.profile:
            raise ValueError("observed runtime profile does not match the trusted composition")
        states = {check.prerequisite: check.state for check in preflight.prerequisites}
        if len(states) != len(RuntimePrerequisite) or any(
            states.get(prerequisite) is not PrerequisiteState.READY
            for prerequisite in RuntimePrerequisite
        ):
            raise ValueError("trusted production runtime is not ready")
        return self._inspection

    async def report(
        self, *, evidence_references: tuple[str, ...] = ()
    ) -> RuntimeReadinessReport:
        await self.inspect()
        if self.readiness_report is None:
            raise RuntimeError("runtime inspection did not produce a readiness report")
        return self.readiness_report.model_copy(
            update={"evidence_references": evidence_references}
        )


def readiness_report(
    *,
    profile: ProductionRuntimeProfile,
    observed_profile: ProductionRuntimeProfile | None,
    preflight: RealBenchmarkPreflight,
    composition_verified: bool,
    evidence_references: tuple[str, ...] = (),
) -> RuntimeReadinessReport:
    """Summarize the trusted 11-check preflight without elevating selectors."""

    states = {check.prerequisite: check.state for check in preflight.prerequisites}

    def status_for(required: tuple[RuntimePrerequisite, ...]) -> RuntimeReadinessStatus:
        selected = tuple(states.get(prerequisite) for prerequisite in required)
        if all(state is PrerequisiteState.READY for state in selected):
            return RuntimeReadinessStatus.READY
        if any(state is PrerequisiteState.DENIED for state in selected):
            return RuntimeReadinessStatus.BLOCKED
        return RuntimeReadinessStatus.NOT_VERIFIED

    reachability = status_for(
        (
            RuntimePrerequisite.POSTGRESQL,
            RuntimePrerequisite.TEMPORAL,
            RuntimePrerequisite.CAPTURE_CHANNEL,
        )
    )
    authorization = status_for(
        (
            RuntimePrerequisite.TRUSTED_AUTHENTICATION,
            RuntimePrerequisite.AUTHORIZATION_SCOPE,
            RuntimePrerequisite.CANONICAL_CORPUS_PERMISSION,
        )
    )
    binding_checks = (
        RuntimePrerequisite.PRODUCTION_COMPOSITION,
        RuntimePrerequisite.CAPTURE_CHANNEL,
    )
    bindings = (
        status_for(binding_checks)
        if composition_verified
        else RuntimeReadinessStatus.BLOCKED
    )
    profile_matches = observed_profile == profile
    all_ready = len(states) == len(RuntimePrerequisite) and all(
        states.get(prerequisite) is PrerequisiteState.READY
        for prerequisite in RuntimePrerequisite
    )
    reason_codes = tuple(
        (
            f"{states[prerequisite].value}:{prerequisite.value}"
            if prerequisite in states
            else f"missing:{prerequisite.value}"
        )
        for prerequisite in sorted(RuntimePrerequisite, key=lambda item: item.value)
        if states.get(prerequisite) is not PrerequisiteState.READY
    )
    if not profile_matches:
        reason_codes += ("mismatched:runtime_profile",)
    if not composition_verified:
        reason_codes += ("mismatched:production_composition",)
    return RuntimeReadinessReport(
        profile=profile,
        configuration=(
            RuntimeReadinessStatus.READY
            if profile_matches
            else RuntimeReadinessStatus.BLOCKED
        ),
        reachability=reachability,
        authorization=authorization,
        bindings=bindings,
        qualification=(
            RuntimeReadinessStatus.READY
            if all_ready and composition_verified
            else RuntimeReadinessStatus.BLOCKED
        ),
        reason_codes=reason_codes,
        evidence_references=evidence_references,
    )


__all__ = [
    "ProductionRuntimeProfile",
    "ProductionRuntimeInspection",
    "ProductionRuntimeInspector",
    "RuntimeReadinessReport",
    "RuntimeReadinessStatus",
    "TrustedProductionRuntimePreflight",
    "readiness_report",
]
