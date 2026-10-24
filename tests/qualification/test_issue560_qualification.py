from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from spine.evals.semantic_support_prompt import SemanticSupportPromptEvaluationReport


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = (
    ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness-v1.json"
)
SUMMARY_PATH = ROOT / "docs/qualification/issue-560-q16-01-runtime-readiness.md"
SEMANTIC_EVAL_PATH = (
    ROOT / "docs/qualification/issue-560-semantic-support-eval-v2.json"
)


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_issue560_report_is_exact_metadata_only_and_passes() -> None:
    report = _report()

    assert report["schema_version"] == "q16-01-runtime-readiness:v1"
    assert report["issue"] == 560
    assert report["decision"] == "PASS"
    assert len(report["target"]["commit_sha"]) == 40
    assert len(report["target"]["deployment_fingerprint"]) == 64
    assert report["privacy"] == {
        "metadata_only": True,
        "credentials_recorded": False,
        "network_locations_recorded": False,
        "content_recorded": False,
        "provider_payloads_recorded": False,
        "raw_errors_recorded": False,
    }


def test_issue560_report_preserves_each_readiness_dimension() -> None:
    report = _report()
    readiness = report["readiness"]

    assert readiness["configuration"] == "PASS"
    assert readiness["reachability"] == "PASS"
    assert readiness["authorization"] == "PASS"
    assert readiness["runtime_provider_bindings"] == "PASS"
    assert readiness["qualification_authority_binding"] == "PASS"
    assert readiness["qualification_target_binding"] == "PASS"
    assert readiness["application_execution"] == "PASS"
    assert readiness["qualification"] == "PASS"
    assert readiness["observer_capture"] == "PASS_AUTHENTICATED_SAME_EXECUTION"
    assert readiness["startup_readiness_provider_calls_observed"] == 0
    assert readiness["qualification_provider_execution"] == (
        "PASS_BOUNDED_BY_SCENARIO"
    )

    observations = report["authorization_observations"]
    assert observations["approved_scope_a"] == "PASS_200"
    assert observations["approved_scope_b"] == "PASS_200"
    assert observations["cross_workspace_source"] == (
        "PASS_NOT_FOUND_NO_IDENTITY_DISCLOSURE"
    )
    assert observations["stale_authorization_generation"] == (
        "PASS_DENIED_PRE_ATTEMPT"
    )
    assert report["authority_bypass_observations"] == {
        "wrong_environment_selector": "PASS_DENIED_403",
        "foreign_token_credential": "PASS_DENIED_401",
        "revoked_administrator": "PASS_DENIED_RUNTIME_VERIFIER",
        "expired_administrator": "PASS_DENIED_RUNTIME_VERIFIER",
        "request_controlled_claims": "PASS_DENIED_SCOPE_RESOLUTION",
        "backup_authority": "PASS_NO_AUTHENTICATION_ALIAS",
        "runtime_identity_content_authority": (
            "PASS_NO_HUMAN_AUTHORITY_INHERITANCE"
        ),
        "access_policy_and_rls": "PASS_ALLOWED_AND_ISOLATED",
    }

    provisioning = report["fixture_provisioning"]
    assert provisioning["mode"] == "explicit_operator_profile_before_runtime_start"
    assert provisioning["idempotent_replay"] == "PASS"
    assert provisioning["partial_failure_retry_contract"] == "PASS"
    assert provisioning["immutable_conflict_detection"] == "PASS"
    assert provisioning["stale_generation_fixture"] == "PASS"
    assert provisioning["generation_after_advance"] == (
        provisioning["generation_before_advance"] + 1
    )
    assert provisioning["generation_after_advance_retry"] == provisioning[
        "generation_after_advance"
    ]
    assert provisioning["counts_unchanged_after_runtime_verification"] is True

    manifest = report["manifest"]
    assert manifest["capture_service_profile_id"] == "api-qa-capture-v1"
    assert manifest["capture_worker_workload_id"] == "spine-qa-worker"
    assert manifest["capture_observer_workload_id"] == "spine-qa-observer"
    browser = report["browser_qualification"]
    assert browser["result"] == "PASS"
    assert browser["scenario_count"] == 10
    assert browser["terminal_run_count"] == 4
    assert browser["citation_locator_validation"] == "PASS"
    assert browser["runtime_authority_verifier"] == "PASS"
    assert browser["runtime_authority_verifier_case_count"] == 9
    contracts = report["component_contracts"]
    assert contracts["semantic_support_policy_binding"] == "qa.semantic-support:r2"
    assert len(contracts["semantic_support_policy_digest"]) == 64
    assert contracts["chunk_projection_mapping_contract"] == (
        "chunk-projection-mapping:v3"
    )


def test_issue560_terminal_run_has_complete_same_execution_correlation() -> None:
    report = _report()
    run = report["synthetic_ask"]

    correlation_fields = (
        "command_id",
        "message_id",
        "run_id",
        "trace_id",
        "capture_id",
        "subscription_id",
        "outbox_event_id",
        "temporal_run_id",
    )
    assert all(run[field] for field in correlation_fields)
    assert run["api_admission"] == "PASS"
    assert run["canonical_outbox"] == "PASS_DELIVERED"
    assert run["dispatcher_delivery"] == "PASS_DELIVERED"
    assert run["temporal_admission"] == "PASS_COMPLETED"
    assert run["answer_question_capability"] == "PASS_ANSWERED"
    assert run["canonical_terminal_readback"] == "PASS_ANSWERED"
    assert run["same_execution_capture"] == (
        "PASS_AUTHENTICATED_ACKNOWLEDGED"
    )
    assert run["observer_registration"] == "PASS_BEFORE_ADMISSION"
    assert run["subscription_id_matches_capture"] is True
    assert run["capture_run_id_matches_terminal_run"] is True
    assert run["capture_message_id_matches_terminal_message"] is True
    assert run["capture_mode"] == "SAME_EXECUTION_IN_FLIGHT"
    assert run["second_execution_observed"] is False
    assert run["attempt_count"] == 1
    assert run["contract_expected_path_invocations"] == {
        "authorization": 1,
        "retrieval": 1,
        "generation": 1,
        "disclosure": 1,
    }
    assert run["path_cardinality_basis"] == (
        "CONTRACT_INFERENCE_NOT_OBSERVED_COUNTERS"
    )
    assert run["path_cardinality_contract"] == (
        "r1-qa-observer-capture:v1-single-attempt-contract"
    )
    assert run["safe_reason_code"] is None
    assert report["blockers"] == []
    assert report["operator_amendment"] == {
        "reference": "codex-session-issue562-r6-007-20261024",
        "scope": "disposable_source_profile_synthetic_authority",
        "runtime_startup_readiness_mutation": False,
        "production_authority": False,
    }

    stale = report["stale_generation_probe"]
    provisioning = report["fixture_provisioning"]
    assert stale["api_admission"] == (
        f"PASS_GENERATION_{provisioning['generation_before_advance']}"
    )
    assert stale["worker_authorization"] == (
        "PASS_FAIL_CLOSED_AT_GENERATION_"
        f"{provisioning['generation_after_advance']}"
    )
    assert stale["attempt_count"] == 0
    assert stale["provider_calls_observed"] == 0
    assert stale["protected_source_disclosure"] is False


def test_issue560_semantic_support_eval_is_pinned_content_free_and_passes() -> None:
    report = _report()
    evidence = report["semantic_support_evaluation"]
    raw = SEMANTIC_EVAL_PATH.read_bytes()
    evaluation = SemanticSupportPromptEvaluationReport.model_validate_json(raw)

    assert evidence["artifact"] == SEMANTIC_EVAL_PATH.name
    assert evidence["artifact_sha256"] == sha256(raw).hexdigest()
    assert evidence["result"] == "PASS"
    assert evaluation.dataset_version == evidence["dataset_version"] == "2.0.1"
    assert evaluation.configuration.model_profile_id == (
        evidence["qualified_model_profile_id"]
    )
    assert evaluation.prompt_digest == report["component_contracts"][
        "semantic_support_policy_digest"
    ]
    assert evaluation.total_cases == evidence["total_cases"] == 4
    assert evaluation.passed_cases == evidence["passed_cases"] == 4
    assert evaluation.failed_case_ids == ()
    assert all(item.passed for item in evaluation.case_outcomes)


def test_issue560_artifacts_omit_protected_or_secret_fields() -> None:
    report_text = REPORT_PATH.read_text(encoding="utf-8").lower()
    summary_text = SUMMARY_PATH.read_text(encoding="utf-8").lower()
    forbidden = (
        '"credential"',
        '"dsn"',
        '"endpoint"',
        '"question"',
        '"answer"',
        '"payload"',
        '"source_object_id"',
        '"filename"',
        '"raw_exception"',
    )

    assert all(value not in report_text for value in forbidden)
    assert all(value not in summary_text for value in forbidden)
