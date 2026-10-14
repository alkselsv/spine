from __future__ import annotations

import pytest
from pydantic import ValidationError

from spine.memory.cognee.production_qualification import (
    GateObservation,
    GateStatus,
    ProductionProfile,
    ProductionQualificationResult,
    QualificationDecision,
    QualificationMode,
    evaluate_production_profile,
    required_gate_ids,
    run_qualification_gates,
)


def _passing_observations() -> tuple[GateObservation, ...]:
    return tuple(
        GateObservation(gate_id=gate_id, status=GateStatus.PASSED)
        for gate_id in required_gate_ids()
    )


def test_live_candidate_requires_every_gate_before_production_approval() -> None:
    result = evaluate_production_profile(
        profile=ProductionProfile.approved_candidate(),
        mode=QualificationMode.LIVE_REAL_BACKENDS,
        observations=_passing_observations(),
    )

    assert result.decision is QualificationDecision.GO
    assert result.production_approval is True
    assert result.failed_gate_ids == ()
    assert result.not_run_gate_ids == ()


def test_missing_live_evidence_is_an_explicit_no_go_with_owned_fallback() -> None:
    observations = list(_passing_observations())
    observations[-1] = GateObservation(
        gate_id=observations[-1].gate_id,
        status=GateStatus.NOT_RUN,
        safe_code="live_prerequisite_unavailable",
    )

    result = evaluate_production_profile(
        profile=ProductionProfile.approved_candidate(),
        mode=QualificationMode.LIVE_REAL_BACKENDS,
        observations=tuple(observations),
    )

    assert result.decision is QualificationDecision.NO_GO
    assert result.production_approval is False
    assert result.not_run_gate_ids == (observations[-1].gate_id,)
    assert result.fallback == "owned_postgresql_projection_mapping"


def test_fake_backed_success_cannot_be_promoted_to_production_approval() -> None:
    result = evaluate_production_profile(
        profile=ProductionProfile.approved_candidate(),
        mode=QualificationMode.OFFLINE_FAKE,
        observations=_passing_observations(),
    )

    assert result.decision is QualificationDecision.NO_GO
    assert result.production_approval is False
    assert result.safe_reason_code == "live_real_backend_evidence_required"


def test_postgres_demo_is_rejected_even_when_observations_pass() -> None:
    result = evaluate_production_profile(
        profile=ProductionProfile(
            relational="postgresql",
            graph="postgres_demo",
            vector="pgvector",
        ),
        mode=QualificationMode.LIVE_REAL_BACKENDS,
        observations=_passing_observations(),
    )

    assert result.decision is QualificationDecision.NO_GO
    assert result.production_approval is False
    assert result.safe_reason_code == "production_profile_rejected"


def test_duplicate_missing_and_unknown_gate_observations_fail_closed() -> None:
    duplicate = _passing_observations() + (_passing_observations()[0],)
    missing = _passing_observations()[:-1]
    unknown = _passing_observations() + (
        GateObservation(gate_id="unapproved_gate", status=GateStatus.PASSED),
    )

    for observations in (duplicate, missing, unknown):
        result = evaluate_production_profile(
            profile=ProductionProfile.approved_candidate(),
            mode=QualificationMode.LIVE_REAL_BACKENDS,
            observations=observations,
        )

        assert result.decision is QualificationDecision.NO_GO
        assert result.production_approval is False
        assert result.safe_reason_code == "invalid_gate_evidence"


def test_report_shape_contains_only_bounded_safe_codes() -> None:
    result = evaluate_production_profile(
        profile=ProductionProfile.approved_candidate(),
        mode=QualificationMode.LIVE_REAL_BACKENDS,
        observations=tuple(
            GateObservation(
                gate_id=gate_id,
                status=GateStatus.FAILED if index == 0 else GateStatus.PASSED,
                safe_code="physical_failure" if index == 0 else None,
            )
            for index, gate_id in enumerate(required_gate_ids())
        ),
    )

    serialized = result.model_dump_json()
    assert result.decision is QualificationDecision.NO_GO
    assert "physical_failure" in serialized
    assert "provider_error" not in serialized

    with pytest.raises(ValidationError):
        GateObservation(
            gate_id="public_sdk",
            status=GateStatus.FAILED,
            safe_code="customer-secret-content",
        )

    with pytest.raises(ValidationError):
        ProductionQualificationResult(
            profile=ProductionProfile.approved_candidate(),
            mode=QualificationMode.LIVE_REAL_BACKENDS,
            decision=QualificationDecision.GO,
            production_approval=True,
            safe_reason_code="all_required_gates_passed",
            observations=(),
            fallback="owned_postgresql_projection_mapping",
        )

    with pytest.raises(ValidationError):
        ProductionQualificationResult(
            profile=ProductionProfile(
                relational="postgresql",
                graph="postgres_demo",
                vector="pgvector",
            ),
            mode=QualificationMode.LIVE_REAL_BACKENDS,
            decision=QualificationDecision.NO_GO,
            production_approval=False,
            safe_reason_code="live_real_backend_evidence_required",
            observations=_passing_observations(),
            fallback="owned_postgresql_projection_mapping",
        )


async def test_gate_runner_executes_complete_inventory_after_a_safe_failure() -> None:
    executed: list[str] = []

    async def passing() -> None:
        executed.append("passed")

    async def failing() -> None:
        executed.append("failed")
        raise RuntimeError("provider failure containing protected customer text")

    gates = {
        gate_id: (failing if gate_id == "unsafe_evidence" else passing)
        for gate_id in required_gate_ids()
    }

    observations = await run_qualification_gates(gates)

    assert len(executed) == len(required_gate_ids())
    assert [item.gate_id for item in observations] == list(required_gate_ids())
    failed = next(item for item in observations if item.gate_id == "unsafe_evidence")
    assert failed.status is GateStatus.FAILED
    assert failed.safe_code == "incomplete_evidence"
    assert "protected customer text" not in repr(observations)
