"""Deterministic release evaluation for lifecycle review and disclosure holds."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from spine.domain.common import DefinitionModel


class DisclosureHoldSafetyViolation(str, Enum):
    REVIEW_REQUIRED_CONTENT_DISCLOSED = "review_required_content_disclosed"
    HELD_CONTENT_DISCLOSED = "held_content_disclosed"


class DisclosureHoldSafetyObservation(DefinitionModel):
    schema_version: Literal["disclosure-hold-safety-observation:v1"] = "disclosure-hold-safety-observation:v1"
    lifecycle_disclosure_eligible: bool
    active_disclosure_hold: bool
    content_disclosed: bool


class DisclosureHoldSafetyResult(DefinitionModel):
    schema_version: Literal["disclosure-hold-safety-result:v1"] = "disclosure-hold-safety-result:v1"
    status: Literal["passed", "failed"]
    violation_codes: tuple[DisclosureHoldSafetyViolation, ...] = ()


class DisclosureHoldSafetyGate:
    """Fail a release when deny-only state and observed disclosure diverge."""

    def evaluate(self, observation: DisclosureHoldSafetyObservation) -> DisclosureHoldSafetyResult:
        violations: list[DisclosureHoldSafetyViolation] = []
        if not observation.lifecycle_disclosure_eligible and observation.content_disclosed:
            violations.append(DisclosureHoldSafetyViolation.REVIEW_REQUIRED_CONTENT_DISCLOSED)
        if observation.active_disclosure_hold and observation.content_disclosed:
            violations.append(DisclosureHoldSafetyViolation.HELD_CONTENT_DISCLOSED)
        return DisclosureHoldSafetyResult(
            status="failed" if violations else "passed",
            violation_codes=tuple(violations),
        )


__all__ = [
    "DisclosureHoldSafetyGate",
    "DisclosureHoldSafetyObservation",
    "DisclosureHoldSafetyResult",
    "DisclosureHoldSafetyViolation",
]
