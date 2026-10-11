from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.domain.ingestion import (
    FailureStage,
    IngestionFailure,
    IngestionItemState,
    IngestionRunSource,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingCoverage,
    ProcessingReceipt,
    ProcessingExclusion,
    SafeDiagnosticDetail,
    WarningStage,
)


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def identifier(value: int) -> UUID:
    return UUID(int=value)


def item() -> IngestionRunSource:
    return IngestionRunSource(
        ingestion_run_source_id=identifier(1), ingestion_run_id=identifier(2),
        workspace_id=identifier(3), environment_id=identifier(4), input_ordinal=1,
        input_identity="input-1", input_digest="a" * 64, command_identity="cmd-1",
        command_digest="b" * 64, created_at=NOW,
    )


def detail() -> SafeDiagnosticDetail:
    return SafeDiagnosticDetail(schema_version="ingestion-safe-detail:v1", attempt=1)


def test_item_transition_requires_item_state_and_item_target() -> None:
    transition = IngestionTransition(
        ingestion_transition_id=identifier(10), workspace_id=identifier(3),
        environment_id=identifier(4), target_kind=IngestionTargetKind.ITEM,
        ingestion_run_id=identifier(2), ingestion_run_source_id=identifier(1),
        state=IngestionItemState.OBSERVED, attempt=1, actor_or_service="parser",
        trace_id=identifier(11), occurred_at=NOW,
    )

    assert transition.state is IngestionItemState.OBSERVED


def test_item_processing_transition_uses_item_state_enum() -> None:
    transition = IngestionTransition(
        ingestion_transition_id=identifier(12), workspace_id=identifier(3), environment_id=identifier(4),
        target_kind=IngestionTargetKind.ITEM, ingestion_run_id=identifier(2), ingestion_run_source_id=identifier(1),
        state="processing", attempt=1, actor_or_service="parser", trace_id=identifier(13), occurred_at=NOW,
    )

    assert transition.state is IngestionItemState.PROCESSING


def test_transition_rejects_run_target_with_item_state() -> None:
    with pytest.raises(ValueError, match="valid IngestionRunState"):
        IngestionTransition(
            ingestion_transition_id=identifier(10), workspace_id=identifier(3),
            environment_id=identifier(4), target_kind=IngestionTargetKind.RUN,
            ingestion_run_id=identifier(2), state=IngestionItemState.OBSERVED,
            attempt=1, actor_or_service="parser", trace_id=identifier(11), occurred_at=NOW,
        )


def test_safe_diagnostic_rejects_unallowlisted_free_text() -> None:
    with pytest.raises(ValueError):
        SafeDiagnosticDetail(
            schema_version="ingestion-safe-detail:v1", error_message="parser leaked text"
        )


def test_failed_processing_receipt_cannot_pin_output_digest() -> None:
    with pytest.raises(ValueError, match="failed processing"):
        ProcessingReceipt(
            processing_receipt_id=identifier(20), workspace_id=identifier(3),
            environment_id=identifier(4), ingestion_run_source_id=identifier(1),
            binding_id=identifier(5), source_object_id=identifier(6),
            source_revision_id=identifier(7), processor_kind="document_processor",
            parser_name="parser", parser_version="v1", parser_config_reference="config-v1",
            locator_contract_version="locator-v1", original_sha256="c" * 64,
            output_kind="parsed-document", output_digest="d" * 64,
            coverage=ProcessingCoverage.FAILED, command_identity="process-1",
            command_digest="e" * 64, idempotency_receipt_id=identifier(8), completed_at=NOW,
        )


def test_partial_processing_receipt_requires_exclusion_or_safe_output() -> None:
    receipt = ProcessingReceipt(
        processing_receipt_id=identifier(20), workspace_id=identifier(3),
        environment_id=identifier(4), ingestion_run_source_id=identifier(1),
        binding_id=identifier(5), source_object_id=identifier(6), source_revision_id=identifier(7),
        processor_kind="document_processor", parser_name="parser", parser_version="v1",
        parser_config_reference="config-v1", locator_contract_version="locator-v1",
        original_sha256="c" * 64, output_kind="parsed-document", output_digest="d" * 64,
        output_reference=identifier(21),
        coverage=ProcessingCoverage.PARTIAL,
        exclusions=(ProcessingExclusion(schema_version="ingestion-exclusion:v1", code="invalid_locator", count=1),),
        command_identity="process-1", command_digest="e" * 64,
        idempotency_receipt_id=identifier(8), completed_at=NOW,
    )

    assert receipt.coverage is ProcessingCoverage.PARTIAL


def test_failure_codes_are_bounded_and_content_free() -> None:
    failure = IngestionFailure(
        ingestion_failure_id=identifier(30), workspace_id=identifier(3), environment_id=identifier(4),
        ingestion_run_source_id=identifier(1), attempt=1, stage=FailureStage.PARSING,
        code="parser_failed", retryable=True, safe_detail=detail(), occurred_at=NOW,
    )
    assert failure.code == "parser_failed"
