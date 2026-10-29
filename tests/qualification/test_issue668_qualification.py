"""Executable offline acceptance evidence for issue #668."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCENARIOS: dict[str, tuple[str, ...]] = {
    "independent_media_inspection_and_resource_governance": (
        "tests/application/test_transcription.py::test_long_audio_is_inspected_chunked_and_reassembled_reproducibly",
        "tests/application/test_transcription.py::test_ineligible_inspected_media_stops_before_model_and_creates_no_artifact",
        "tests/application/test_transcription.py::test_malformed_or_decoded_oversized_media_cleans_up_without_model_work",
        "tests/application/test_transcription.py::test_decode_timeout_aborts_preparation_and_stops_before_model",
    ),
    "deterministic_chunking_overlap_decoder_and_original_timeline": (
        "tests/domain/artifacts/test_transcription.py::test_processing_profile_plans_reproducible_overlapping_chunks",
        "tests/application/test_transcription.py::test_long_audio_is_inspected_chunked_and_reassembled_reproducibly",
    ),
    "partial_retry_lost_response_and_duplicate_delivery": (
        "tests/application/test_transcription.py::test_transient_chunk_failure_retries_with_the_same_stable_identity",
        "tests/application/test_transcription.py::test_retry_exhaustion_persists_exact_partial_artifact_and_replays_it",
        "tests/application/test_transcription.py::test_exact_replay_returns_one_logical_job_and_artifact_without_second_model_call",
        "tests/application/test_transcription.py::test_invalid_later_chunk_preserves_completed_work_as_partial",
    ),
    "cancellation_and_stable_terminal_receipt": (
        "tests/application/test_transcription.py::test_cancellation_after_completed_chunk_stops_subsequent_model_work",
        "tests/application/test_transcription.py::test_cancellation_accepted_during_finalization_wins_atomically",
        "tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_persists_cancellation_and_exact_partial_artifact",
    ),
    "empty_silent_corrupt_unsupported_and_oversized_fail_closed": (
        "tests/application/test_transcription.py::test_ineligible_inspected_media_stops_before_model_and_creates_no_artifact",
        "tests/application/test_transcription.py::test_no_speech_output_cannot_create_transcription_artifact",
        "tests/application/test_transcription.py::test_malformed_or_decoded_oversized_media_cleans_up_without_model_work",
        "tests/application/test_transcription.py::test_noisy_synthetic_russian_fixture_preserves_adapter_uncertainty",
    ),
}


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_issue668_traceability_points_to_executable_public_seams() -> None:
    for node_id in (node for scenario in SCENARIOS.values() for node in scenario):
        relative_path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / relative_path), node_id


def test_transcription_processing_cannot_publish_requirements_evidence() -> None:
    application_source = (ROOT / "src/spine/application/transcription.py").read_text(
        encoding="utf-8"
    )
    domain_source = (ROOT / "src/spine/domain/artifacts/transcription.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "extract_requirements",
        "RequirementsArtifact",
        "ContextProjection",
        "spine.memory",
        "cognee",
    )

    for symbol in forbidden:
        assert symbol not in application_source
        assert symbol not in domain_source
