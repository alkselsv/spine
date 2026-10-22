from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.production_qa_composition import _issue_production_qa_composition_evidence
from spine.application.production_runtime_readiness import (
    ProductionRuntimeInspection,
    ProductionRuntimeProfile,
    TrustedProductionRuntimePreflight,
)
from spine.evals.real_benchmark_qualification import (
    PrerequisiteState,
    RealBenchmarkPreflight,
    RuntimePrerequisite,
    RuntimePrerequisiteCheck,
    _issue_trusted_real_benchmark_preflight,
)
from spine.evals.real_baseline import BaselineCaseInput, BaselineMetricInput, build_r1_baseline_report
from spine.evals.real_benchmark_runner import RealBenchmarkRunner, RuntimeVersionPins
from spine.evals.threshold_gate_composition import build_threshold_evidence
from spine.evaluations.release_gates.thresholds import (
    MandatorySafetyResult,
    MetricThreshold,
    PolicyApproval,
    RuntimeIdentityBinding,
    ThresholdEvidence,
    ThresholdEvidenceV2,
    ThresholdPolicy,
    ThresholdPolicyV2,
    VerifiedThresholdApproval,
    evaluate_threshold_policy as _evaluate_threshold_policy,
    _issue_verified_threshold_approval,
    policy_digest,
)
from spine.evaluations.release_gates.threshold_approval_verifier import (
    GitHubDecisionComment,
    GitHubThresholdApprovalVerifier,
    GitHubTimelineEvent,
    evaluate_threshold_policy_with_github_approval,
)


def _runtime_binding() -> RuntimeIdentityBinding:
    return RuntimeIdentityBinding(
        profile_identity="d" * 64,
        profile_version="spine-production-runtime-profile:v1",
        deployment_fingerprint="a" * 64,
        commit_sha="b" * 40,
        runtime_pins=RuntimeVersionPins(
            observer_seam_version="observer:v1",
            application_version="api:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="validation:v1",
            projection_kind="qa",
        ),
    )


def _evidence(
    *,
    value: float = 0.9,
    coverage: str = "complete",
    metric: str = "qa.answer_outcome",
    failure_counts: dict[str, int] | None = None,
) -> ThresholdEvidenceV2:
    measured = 149 if coverage == "complete" else 148
    baseline = build_r1_baseline_report(
        cases=tuple(
            BaselineCaseInput(
                case_key=f"private-case-{index}",
                source_formats=("pdf",),
                expected_outcome="answered",
                observation_status="measured",
                metric_eligibility=(
                    {"qa.answer_outcome": 149, "qa.denial_quality": 0}
                    if index == 0
                    else {}
                ),
                metrics=(
                    BaselineMetricInput(
                        metric=metric,
                        version="v1",
                        value=value,
                        eligible_count=measured,
                        measured_denominator=1,
                        matched_count=1,
                    ),
                ) if metric == "qa.answer_outcome" and index == 0 else (),
            )
            for index in range(149)
        ),
        observer_latencies_ms=(),
        worker_latencies_ns=(),
        observer_attempt_count=0,
        failure_counts=failure_counts,
    )
    binding = _runtime_binding()
    return ThresholdEvidenceV2(
        evidence_version="baseline-run-7",
        run_id="c" * 64,
        dataset_id="qa-r1-real-approved",
        dataset_version=baseline.dataset_version,
        dataset_digest=baseline.dataset_sha256,
        runtime_identity=binding.profile_identity,
        runtime_scope="r1-production-profile",
        runtime_binding=binding,
        baseline=baseline,
    )


def _policy(
    *, threshold: float = 0.8, approved: bool = True, metric: str = "qa.answer_outcome",
    direction: str = "minimum", comparison: str = "gte", minimum_coverage: float = 0.5,
    optional_denial_metric: bool = False,
) -> ThresholdPolicyV2:
    metrics = [
        MetricThreshold(
            metric=metric,
            evidence_kind="metric",
            direction=direction,
            comparison=comparison,
            statistic="weighted_mean",
            denominator="measured_denominator",
            expected_eligible_count=149 if metric == "qa.answer_outcome" else 0,
            applicability="required",
            minimum_coverage=minimum_coverage,
            threshold=threshold,
        )
    ]
    evaluator_versions = [(metric, "v1")]
    if optional_denial_metric:
        metrics.append(
            MetricThreshold(
                metric="qa.denial_quality",
                evidence_kind="metric",
                direction="minimum",
                comparison="gte",
                statistic="weighted_mean",
                denominator="measured_denominator",
                expected_eligible_count=0,
                applicability="optional",
                minimum_coverage=0,
                threshold=0.5,
            )
        )
        evaluator_versions.append(("qa.denial_quality", "v1"))
    binding = _runtime_binding()
    policy = ThresholdPolicyV2(
        policy_id="r1-quality-policy",
        version="v1",
        dataset_id="qa-r1-real-approved",
        dataset_version="0.3.9-user-approved",
        dataset_digest="7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62",
        metric_evaluator_versions=tuple(evaluator_versions),
        runtime_identity=binding.profile_identity,
        runtime_scope="r1-production-profile",
        runtime_binding=binding,
        metrics=tuple(metrics),
        state="draft",
    )
    if not approved:
        return policy
    return policy.model_copy(
        update={
            "state": "approved",
            "approval": PolicyApproval(
                approval_id="approval-42",
                approver_id="product-owner-1",
                authority="product_owner",
                approval_version="v1",
                approved_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
                policy_digest=policy_digest(policy),
            ),
        }
    )


def _verified_approval(policy: ThresholdPolicyV2) -> VerifiedThresholdApproval:
    return _issue_verified_threshold_approval(
        decision_id="decision-42",
        record_reference="https://github.com/alkselsv/spine/issues/557#issuecomment-42",
        actor_id=167317615,
        authority_reference="https://github.com/alkselsv/spine/issues/17",
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_digest=policy_digest(policy),
        dataset_id=policy.dataset_id,
        dataset_version=policy.dataset_version,
        dataset_digest=policy.dataset_digest,
        runtime_binding=policy.runtime_binding,
        release_scope=policy.runtime_scope,
        decision_status="approved",
        decided_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        source_snapshot_digest="e" * 64,
    )


def evaluate_threshold_policy(**kwargs):
    policy = kwargs["policy"]
    if (
        type(policy) is ThresholdPolicyV2
        and policy.state == "approved"
        and "verified_approval" not in kwargs
    ):
        kwargs["verified_approval"] = _verified_approval(policy)
    return _evaluate_threshold_policy(**kwargs)


def _safety(status: str = "passed") -> tuple[MandatorySafetyResult, ...]:
    return (
        MandatorySafetyResult(gate_id="hard-safety", status=status),  # type: ignore[arg-type]
    )


def test_approved_compatible_policy_passes_at_exact_boundary_deterministically() -> None:
    policy = _policy(threshold=0.9)
    evidence = _evidence(value=0.9)

    first = evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    second = evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert first == second
    assert first.status == "passed"
    assert first.metrics[0].status == "passed"
    assert first.metrics[0].applied_threshold == 0.9
    assert first.metrics[0].denominator == 1
    assert first.metrics[0].eligible_count == 149


def test_draft_policy_is_not_approved_and_cannot_pass() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(approved=False),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "not_approved"
    assert {metric.status for metric in result.metrics} == {"not_approved"}


def test_legacy_policy_approval_cannot_qualify_without_trusted_verifier_evidence() -> None:
    v2_policy = _policy()
    policy = ThresholdPolicy.model_validate(
        v2_policy.model_dump(exclude={"schema_version", "runtime_binding"})
    )
    v2_evidence = _evidence()
    evidence = ThresholdEvidence(
        evidence_version=v2_evidence.evidence_version,
        dataset_id=v2_evidence.dataset_id,
        dataset_version=v2_evidence.dataset_version,
        dataset_digest=v2_evidence.dataset_digest,
        runtime_identity=v2_evidence.runtime_identity,
        runtime_scope=v2_evidence.runtime_scope,
        baseline=v2_evidence.baseline,
    )
    result = _evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "not_approved"


def test_missing_safety_result_blocks_quality_evaluation() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "unavailable"
    assert result.metrics[0].status == "unavailable"
    assert result.reason_codes == ("mandatory_safety_missing",)


def test_failed_safety_result_blocks_quality_evaluation() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=_safety("failed"),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "failed"
    assert result.metrics[0].status == "unavailable"
    assert result.reason_codes == ("mandatory_safety_failed",)


def test_empty_or_conflicting_mandatory_safety_set_cannot_be_omitted() -> None:
    empty = evaluate_threshold_policy(
        policy=_policy(), evidence=_evidence(), safety_results=(), required_safety_gates=()
    )
    conflicting = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=(
            MandatorySafetyResult(gate_id="hard-safety", status="passed"),
            MandatorySafetyResult(gate_id="hard-safety", status="failed"),
        ),
        required_safety_gates=("hard-safety",),
    )
    assert empty.status == "unavailable"
    assert empty.reason_codes == ("mandatory_safety_missing",)
    assert conflicting.status == "failed"


def test_zero_eligible_denied_metric_is_not_applicable() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(metric="qa.denial_quality", minimum_coverage=0.0),
        evidence=_evidence(metric="qa.denial_quality"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert result.status == "unavailable"
    assert result.metrics[0].status == "not_applicable"


def test_optional_not_applicable_metric_does_not_replace_required_decision() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(optional_denial_metric=True),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "passed"
    assert result.metrics[0].status == "passed"
    assert result.metrics[1].status == "not_applicable"


def test_policy_identity_digest_excludes_approval_and_is_stable() -> None:
    policy = _policy(approved=False)
    assert policy_digest(policy) == policy_digest(policy)


@pytest.mark.asyncio
async def test_threshold_evidence_rejects_report_not_emitted_by_production_runner() -> None:
    profile = ProductionRuntimeProfile(
        postgresql_major=17,
        migration_revision="20261020_issue506",
        temporal_namespace="spine-r1",
        temporal_task_queue="spine-r1-qa",
        worker_roles=("observer",),
        authentication_mode="oidc",
        storage_profile="filesystem-v1",
        context_projection_provider="cognee:v1",
        projection_backend_versions=("pgvector:v1",),
        llm_model_version="model:v1",
        embedding_model_version="embed:v1",
        embedding_dimensions=1536,
        web_release_identity="web:v1",
        api_release_identity="api:v1",
        commit_sha="b" * 40,
        deployment_fingerprint="a" * 64,
    )
    composition_evidence = _issue_production_qa_composition_evidence(object(), object())

    class Inspector:
        is_fake = False

        async def inspect(self):
            return ProductionRuntimeInspection(
                profile=profile,
                preflight=RealBenchmarkPreflight(
                    workspace_id=UUID(int=1),
                    environment_id=UUID(int=2),
                    prerequisites=tuple(
                        RuntimePrerequisiteCheck(
                            prerequisite=prerequisite,
                            state=PrerequisiteState.READY,
                        )
                        for prerequisite in RuntimePrerequisite
                    ),
                ),
            )

    inspector = Inspector()
    trusted_runtime = TrustedProductionRuntimePreflight(
        profile=profile,
        preflight=_issue_trusted_real_benchmark_preflight(
            inspector=inspector,
            production_evidence=composition_evidence,
        ),
        inspector=inspector,
    )
    runner = RealBenchmarkRunner(
        mapping=object(),
        readiness=object(),
        trusted_persistence_context=object(),
        observer=object(),
        gold=object(),
        pins=_runtime_binding().runtime_pins,
        workspace_selector=UUID(int=1),
        environment_selector=UUID(int=2),
    )

    with pytest.raises(ValueError, match="runner's accepted production result"):
        await build_threshold_evidence(
            trusted_runtime=trusted_runtime,
            runner=runner,
            report=object(),
            dataset_id="dataset:v1",
            runtime_scope="production",
        )


def test_wrong_verified_actor_is_not_approved() -> None:
    policy = _policy()
    invalid_approval = _verified_approval(policy).model_copy(update={"actor_id": 21})
    result = evaluate_threshold_policy(
        policy=policy,
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
        verified_approval=invalid_approval,
    )
    assert result.status == "not_approved"


def test_below_minimum_threshold_fails() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.91),
        evidence=_evidence(value=0.9),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "failed"
    assert result.metrics[0].status == "failed"


def test_metadata_result_does_not_include_private_case_keys() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    serialized = result.model_dump_json()
    assert "private-case-0" not in serialized


def test_maximum_threshold_passes_on_exact_boundary() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.9, direction="maximum", comparison="lte"),
        evidence=_evidence(value=0.9),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "passed"


def test_partial_metric_population_below_required_coverage_is_insufficient() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(minimum_coverage=1.0),
        evidence=_evidence(coverage="partial"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "insufficient_evidence"
    assert result.metrics[0].reason_code == "coverage_below_minimum"


def test_partial_metric_observations_cannot_pass_even_with_low_minimum_coverage() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(minimum_coverage=0.0),
        evidence=_evidence(coverage="partial"),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "insufficient_evidence"
    assert result.metrics[0].reason_code == "partial_observation"


def test_failed_observation_cluster_blocks_positive_quality_decision() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(failure_counts={"evaluator_failure": 1}),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].status == "unavailable"
    assert "failed_observation" in result.reason_codes


def test_incompatible_dataset_or_runtime_pins_cannot_pass() -> None:
    evidence = _evidence().model_copy(update={"runtime_scope": "other-scope"})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert "runtime_pin_mismatch" in result.reason_codes

    changed_dataset = _evidence().model_copy(update={"dataset_id": "other-dataset"})
    dataset_result = evaluate_threshold_policy(
        policy=_policy(), evidence=changed_dataset, safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert dataset_result.status == "not_approved"


@pytest.mark.parametrize(
    "pin_name",
    (
        "observer_seam_version",
        "application_version",
        "projection_version",
        "model_version",
        "prompt_version",
        "validation_policy_version",
        "projection_kind",
    ),
)
def test_each_runtime_version_pin_is_required(pin_name: str) -> None:
    values = _runtime_binding().runtime_pins.model_dump()
    values.pop(pin_name)
    with pytest.raises(ValidationError):
        RuntimeVersionPins.model_validate(values)


@pytest.mark.parametrize(
    ("binding_field", "replacement"),
    (
        ("commit_sha", "f" * 40),
        ("deployment_fingerprint", "f" * 64),
        ("profile_identity", "f" * 64),
    ),
)
def test_wrong_trusted_profile_binding_cannot_pass(
    binding_field: str, replacement: str
) -> None:
    policy = _policy()
    wrong_binding = policy.runtime_binding.model_copy(
        update={binding_field: replacement}
    )
    wrong_evidence = _evidence().model_copy(
        update={"runtime_binding": wrong_binding, "runtime_identity": wrong_binding.profile_identity}
    )
    result = _evaluate_threshold_policy(
        policy=policy,
        evidence=wrong_evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
        verified_approval=_verified_approval(policy),
    )
    assert result.status == "not_approved"


def test_wrong_model_or_projection_pin_cannot_pass() -> None:
    policy = _policy()
    for pin_name in ("model_version", "projection_version"):
        pins = policy.runtime_binding.runtime_pins.model_copy(
            update={pin_name: "different-version"}
        )
        wrong_binding = policy.runtime_binding.model_copy(update={"runtime_pins": pins})
        evidence = _evidence().model_copy(update={"runtime_binding": wrong_binding})
        result = _evaluate_threshold_policy(
            policy=policy,
            evidence=evidence,
            safety_results=_safety(),
            required_safety_gates=("hard-safety",),
            verified_approval=_verified_approval(policy),
        )
        assert result.status == "not_approved"


class _DecisionSource:
    def __init__(self, comments, timeline=(), actor_id=167317615) -> None:
        self._comments = tuple(comments)
        self._timeline = tuple(timeline)
        self._actor_id = actor_id

    async def issue_author_id(self, issue_number: int) -> int:
        assert issue_number == 17
        return self._actor_id

    async def comments(self, issue_number: int):
        assert issue_number == 557
        return self._comments

    async def timeline(self, issue_number: int):
        assert issue_number == 557
        return self._timeline


def _decision_comment(
    policy: ThresholdPolicyV2,
    *,
    status: str = "approved",
    actor_id: int = 167317615,
    comment_id: int = 42,
    decision_id: str = "decision-42",
    updated_at: datetime | None = None,
    dataset_digest: str | None = None,
    policy_digest_value: str | None = None,
    policy_version: str | None = None,
    html_url: str | None = None,
    supersedes: tuple[str, ...] = (),
    revokes: tuple[str, ...] = (),
) -> GitHubDecisionComment:
    decided_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    decision = {
        "schema_version": "spine-r1-threshold-decision:v1",
        "decision_id": decision_id,
        "status": status,
        "policy_id": policy.policy_id,
        "policy_version": policy_version or policy.version,
        "policy_digest": policy_digest_value or policy_digest(policy),
        "dataset_id": policy.dataset_id,
        "dataset_version": policy.dataset_version,
        "dataset_digest": dataset_digest or policy.dataset_digest,
        "runtime_binding": policy.runtime_binding.model_dump(mode="json"),
        "release_scope": policy.runtime_scope,
        "decided_at": decided_at.isoformat(),
        "supersedes_decision_ids": supersedes,
        "revokes_decision_ids": revokes,
    }
    return GitHubDecisionComment(
        comment_id=comment_id,
        actor_id=actor_id,
        body="```spine-r1-threshold-decision:v1\n"
        + json.dumps(decision, sort_keys=True)
        + "\n```",
        created_at=decided_at,
        updated_at=decided_at if updated_at is None else updated_at,
        html_url=html_url
        or f"https://github.com/alkselsv/spine/issues/557#issuecomment-{comment_id}",
    )


@pytest.mark.asyncio
async def test_github_verifier_issues_exact_current_product_owner_approval() -> None:
    policy = _policy()
    evidence = _evidence()
    verifier = GitHubThresholdApprovalVerifier(
        _DecisionSource((_decision_comment(policy),))
    )

    approval = await verifier.verify(policy=policy, evidence=evidence)
    result = await evaluate_threshold_policy_with_github_approval(
        verifier=verifier,
        policy=policy,
        evidence=evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )

    assert approval is not None
    assert approval.actor_id == 167317615
    assert approval.policy_digest == policy_digest(policy)
    assert approval.runtime_binding == policy.runtime_binding
    assert result.status == "passed"
    assert result.runtime_binding == evidence.runtime_binding
    assert result.approval_reference == approval.record_reference
    assert result.approval_source_snapshot_digest == approval.source_snapshot_digest


@pytest.mark.parametrize(
    "source",
    (
        _DecisionSource((_decision_comment(_policy(), actor_id=9),)),
        _DecisionSource((_decision_comment(_policy(), updated_at=datetime(2026, 10, 20, tzinfo=timezone.utc)),)),
        _DecisionSource((_decision_comment(_policy(), status="revoked"),)),
        _DecisionSource((_decision_comment(_policy(), status="rejected"),)),
        _DecisionSource((_decision_comment(_policy(), dataset_digest="f" * 64),)),
        _DecisionSource((_decision_comment(_policy(), policy_digest_value="f" * 64),)),
        _DecisionSource((_decision_comment(_policy(), policy_version="v2"),)),
        _DecisionSource((_decision_comment(_policy(), html_url="https://attacker.invalid/fake"),)),
        _DecisionSource(
            (_decision_comment(_policy()),),
            (GitHubTimelineEvent(event="comment_deleted", comment_id=42),),
        ),
        _DecisionSource((_decision_comment(_policy()),), actor_id=10),
    ),
)
@pytest.mark.asyncio
async def test_github_verifier_rejects_untrusted_or_stale_decision(source) -> None:
    policy = _policy()
    approval = await GitHubThresholdApprovalVerifier(source).verify(
        policy=policy,
        evidence=_evidence(),
    )

    assert approval is None


@pytest.mark.asyncio
async def test_github_verifier_rejects_superseded_decision() -> None:
    policy = _policy()
    original = _decision_comment(policy)
    successor = _decision_comment(
        policy,
        status="approved",
        comment_id=43,
        supersedes=("decision-42",),
    )
    approval = await GitHubThresholdApprovalVerifier(
        _DecisionSource((original, successor))
    ).verify(policy=policy, evidence=_evidence())

    assert approval is None


@pytest.mark.asyncio
async def test_github_verifier_checks_supersession_across_policy_versions() -> None:
    policy = _policy()
    original = _decision_comment(policy)
    successor = _decision_comment(
        policy,
        comment_id=43,
        decision_id="decision-43",
        policy_version="threshold-policy-v2",
        supersedes=("decision-42",),
    )
    approval = await GitHubThresholdApprovalVerifier(
        _DecisionSource((original, successor))
    ).verify(policy=policy, evidence=_evidence())

    assert approval is None


@pytest.mark.asyncio
async def test_github_verifier_rejects_conflicting_current_decisions() -> None:
    policy = _policy()
    comments = (
        _decision_comment(policy),
        _decision_comment(policy, comment_id=43),
    )
    approval = await GitHubThresholdApprovalVerifier(
        _DecisionSource(comments)
    ).verify(policy=policy, evidence=_evidence())

    assert approval is None


@pytest.mark.asyncio
async def test_missing_or_unavailable_github_source_fails_closed() -> None:
    class UnavailableSource(_DecisionSource):
        async def comments(self, issue_number: int):
            raise OSError("network unavailable")

    policy = _policy()
    approval = await GitHubThresholdApprovalVerifier(
        UnavailableSource(())
    ).verify(policy=policy, evidence=_evidence())

    assert approval is None


def test_v2_policy_without_verifier_evidence_cannot_pass() -> None:
    result = _evaluate_threshold_policy(
        policy=_policy(),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
        verified_approval=None,
    )

    assert result.status == "not_approved"


def test_serialized_approval_record_cannot_recreate_verifier_issued_evidence() -> None:
    policy = _policy()
    caller_constructed = VerifiedThresholdApproval.model_validate(
        _verified_approval(policy).model_dump()
    )
    result = _evaluate_threshold_policy(
        policy=policy,
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
        verified_approval=caller_constructed,
    )

    assert result.status == "not_approved"


def test_incompatible_evaluator_pin_cannot_pass() -> None:
    evidence = _evidence()
    changed_metric = evidence.baseline.metrics[0].model_copy(update={"version": "v2"})
    changed_baseline = evidence.baseline.model_copy(update={"metrics": (changed_metric,)})
    changed_evidence = evidence.model_copy(update={"baseline": changed_baseline})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=changed_evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert result.metrics[0].reason_code == "evaluator_pin_mismatch"


def test_incomplete_dataset_population_cannot_pass() -> None:
    evidence = _evidence()
    partial_baseline = evidence.baseline.model_copy(
        update={
            "eligible_case_count": 148,
            "measured_case_count": 148,
            "unavailable_case_count": 0,
        }
    )
    partial_evidence = evidence.model_copy(update={"baseline": partial_baseline})
    result = evaluate_threshold_policy(
        policy=_policy(),
        evidence=partial_evidence,
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "unavailable"
    assert "population_incomplete" in result.reason_codes


def test_decision_required_threshold_never_passes() -> None:
    result = evaluate_threshold_policy(
        policy=_policy(threshold=0.8, approved=False).model_copy(
            update={"metrics": (_policy(approved=False).metrics[0].model_copy(update={"threshold": None}),)}
        ),
        evidence=_evidence(),
        safety_results=_safety(),
        required_safety_gates=("hard-safety",),
    )
    assert result.status == "not_approved"
