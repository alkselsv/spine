"""Executable offline qualification evidence for issue #671."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_issue671_traceability_points_to_public_seams() -> None:
    expected = (
        "tests/connectors/test_bitrix24_crm_material.py::test_crm_activity_attachment_preserves_exact_binding_and_disk_revision",
        "tests/connectors/test_bitrix24_crm_material.py::test_crm_reader_rejects_unapproved_entity_and_never_calls_provider",
        "tests/connectors/test_bitrix24_crm_material.py::test_crm_reader_rejects_missing_scope_mismatched_file_and_cross_workspace",
        "tests/application/test_bitrix24_crm_material_service.py::test_observation_and_acceptance_are_idempotent_and_revision_scoped",
        "tests/application/test_bitrix24_crm_material_service.py::test_stale_target_revision_cannot_be_accepted",
        "tests/application/test_bitrix24_crm_material_service.py::test_relation_revocation_remains_visible_after_acceptance",
    )
    for node_id in expected:
        relative_path, test_name = node_id.split("::", 1)
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert f"def {test_name}" in source or f"async def {test_name}" in source


def test_crm_material_adapter_has_no_business_authority_or_content_disclosure_path() -> None:
    source = (ROOT / "src/spine/connectors/bitrix24_crm_material.py").read_text(
        encoding="utf-8"
    )
    assert "PricingBasis" not in source
    assert "download" not in source.lower()
    assert "spine.memory" not in source
