"""Versioned synthetic cases for the evidence-lineage safety gate."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from spine.domain.common import DefinitionModel, VersionIdentifier


class EvidenceLineageEvaluationCase(DefinitionModel):
    """Expected verifier decision for one synthetic lineage scenario."""

    case_id: VersionIdentifier
    expected_status: Literal["verified", "rejected"]
    expected_reason_code: VersionIdentifier
    protected_markers: tuple[str, ...] = ()


class EvidenceLineageDataset(DefinitionModel):
    """All version pins needed to reproduce the deterministic evaluation."""

    dataset_id: Literal["evidence-lineage-synthetic"]
    version: Literal["1.0.0"]
    schema_version: Literal["evidence-lineage-eval:v1"]
    retrieval_contract_version: VersionIdentifier
    parser_version: VersionIdentifier
    validation_policy_version: VersionIdentifier
    locator_contract_version: VersionIdentifier
    context_profile_version: VersionIdentifier
    projection_config_version: VersionIdentifier
    model_version: Literal["not-applicable"]
    prompt_version: Literal["not-applicable"]
    assumptions: tuple[str, ...] = Field(min_length=1)
    cases: tuple[EvidenceLineageEvaluationCase, ...] = Field(min_length=1)


@lru_cache(maxsize=1)
def load_evidence_lineage_v1_dataset() -> EvidenceLineageDataset:
    """Load and validate the checked-in deterministic lineage dataset."""

    path = Path(__file__).with_name("data") / "evidence_lineage_v1.json"
    return EvidenceLineageDataset.model_validate_json(path.read_text(encoding="utf-8"))


def canonical_evidence_lineage_dataset_json() -> str:
    """Return stable JSON for audit and digest tooling."""

    dataset = load_evidence_lineage_v1_dataset()
    return json.dumps(dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
