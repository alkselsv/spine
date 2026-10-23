from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.contracts.persistence.ids import synthetic_uuid
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PostgreSQLContractDatabase,
    postgresql_contract_database,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]


@dataclass(frozen=True, slots=True)
class ProductionParsedContentScenario:
    allowed: httpx.Response
    unavailable: tuple[httpx.Response, ...]
    tombstone: httpx.Response
    uploaded: dict[str, object]
    expected: dict[str, object]
    object_audits: tuple[dict[str, object], ...]
    content: bytes


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def production_parsed_content_scenario(
    postgresql_contract_database: PostgreSQLContractDatabase,
    tmp_path_factory: pytest.TempPathFactory,
) -> ProductionParsedContentScenario:
    from spine.api.runtime import DeployableApiSettings, create_production_app
    from spine.application.production_qa_deployment import (
        ProductionQAADeploymentProfile,
    )
    from spine.infrastructure.auth import LocalAuthenticationSettings
    from spine.infrastructure.db.readiness import load_migration_inventory
    from spine.infrastructure.db.settings import RuntimeDatabaseSettings
    from spine.scripts.qualification import (
        QualificationBootstrapSettings,
        provision as provision_qualification,
    )

    identity_id = synthetic_uuid(56840)
    workspace_id = synthetic_uuid(56841)
    foreign_workspace_id = synthetic_uuid(56842)
    environment_id = synthetic_uuid(56843)
    foreign_environment_id = synthetic_uuid(56844)
    alias_issuer = "https://issue-568-production.spine.invalid"
    alias_subject = "issue-568-production-operator"
    credential = "issue-568-production-credential-000000000000"
    qualification = QualificationBootstrapSettings(
        deployment_mode="development",
        migration_database_url=(
            postgresql_contract_database.migration_url.get_secret_value()
        ),
        identity_id=identity_id,
        authentication_alias_issuer=alias_issuer,
        authentication_alias_subject=alias_subject,
        workspace_a_id=workspace_id,
        workspace_b_id=foreign_workspace_id,
        environment_a_id=environment_id,
        environment_b_id=foreign_environment_id,
        authority_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        target_reference="deployment:source:issue-568-v1",
        target_scope_reference="scope:workspace-environment:issue-568-v1",
        secret_provider_reference="provider:issue-568:v1",
        secret_reference="secret:issue-568:v1",
        database_role_reference="postgres-role:spine-runtime:issue-568-v1",
    )
    await provision_qualification(qualification)

    authentication = LocalAuthenticationSettings(
        deployment_mode="development",
        configuration_version="issue-568-local",
        authentication_alias_issuer=alias_issuer,
        authentication_alias_subject=alias_subject,
        bearer_credential=credential,
    )
    api_settings = DeployableApiSettings(
        cursor_secret="issue-568-cursor-secret-00000000000000000000",
        build_version="issue-568-test",
        commit_sha="5" * 40,
        ingestion_service_principal_id=synthetic_uuid(56845),
        qa_service_principal_id=synthetic_uuid(56846),
        original_storage_root=(
            tmp_path_factory.mktemp("production-parsed-content") / "originals"
        ),
        database_profile_id="spine-source-postgresql-17",
        scope_policy_id="workspace-environment-v1",
        ingestion_service_alias_issuer=alias_issuer,
        ingestion_service_alias_subject=alias_subject,
        temporal_cluster_profile_id="local-temporal-v1",
        capture_service_profile_id="api-qa-capture-v1",
        capture_service_url="http://api:8080",
        projection_profile_id="r1-cognee-local-v1",
        model_profile_id="r1-provider-local-v1",
        qa_capture_worker_credential="worker-capture-credential-for-tests-0568",
        qa_capture_observer_credential="observer-capture-credential-for-tests-0568",
    )
    profile = ProductionQAADeploymentProfile(
        profile_id="spine-r1-local",
        profile_version="2026.10.23",
        deployment_fingerprint="5" * 64,
        release_identity=api_settings.build_version,
        commit_sha=api_settings.commit_sha,
        database_profile_id=api_settings.database_profile_id,
        migration_revision=load_migration_inventory().supported_head,
        authentication_profile_id=authentication.configuration_version,
        authentication_mode="local_development",
        scope_policy_id=api_settings.scope_policy_id,
        temporal_cluster_profile_id=api_settings.temporal_cluster_profile_id,
        temporal_namespace=api_settings.temporal_namespace,
        temporal_task_queue=api_settings.temporal_task_queue,
        workflow_contract_version="spine.qa.ask-admission:v1",
        capture_service_profile_id=api_settings.capture_service_profile_id,
        capture_worker_workload_id="spine-qa-worker",
        capture_observer_workload_id="spine-qa-observer",
        capture_contract_version="r1-qa-observer-capture:v1",
        projection_profile_id=api_settings.projection_profile_id,
        model_profile_id=api_settings.model_profile_id,
    )
    application = create_production_app(
        database_settings=RuntimeDatabaseSettings(
            url=postgresql_contract_database.runtime_url
        ),
        authentication_settings=authentication,
        api_settings=api_settings,
        deployment_profile=profile,
    )
    content = b"# Current handbook\n\nProduction evidence.\n"
    headers = {
        "Authorization": f"Bearer {credential}",
        "X-Workspace-ID": str(workspace_id),
        "X-Environment-ID": str(environment_id),
    }
    upload_headers = {
        **headers,
        "Idempotency-Key": "issue-568-production-upload",
        "X-Upload-Identity": "issue-568-handbook",
        "X-Filename": "handbook.md",
        "Content-Type": "text/markdown",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            upload = await client.post(
                "/api/v1/documents/uploads",
                headers=upload_headers,
                content=content,
            )
            assert upload.status_code == 202, upload.text
            uploaded = upload.json()
            assert uploaded["status"] == "succeeded"
            assert uploaded["processing_receipt_id"] is not None
            path = (
                "/api/v1/knowledge/documents/"
                f"{uploaded['source_object_id']}/revisions/"
                f"{uploaded['source_revision_id']}/parsed-content"
            )
            query = {"processing_receipt_id": uploaded["processing_receipt_id"]}
            allowed = await client.get(path, params=query, headers=headers)
            foreign = await client.get(
                path,
                params=query,
                headers={
                    **headers,
                    "X-Workspace-ID": str(foreign_workspace_id),
                    "X-Environment-ID": str(foreign_environment_id),
                },
            )
            missing = await client.get(
                path.replace(uploaded["source_revision_id"], str(synthetic_uuid(56847))),
                params=query,
                headers=headers,
            )
            tombstone = await client.post(
                f"/api/v1/documents/{uploaded['source_object_id']}/tombstone",
                headers={**headers, "Idempotency-Key": "issue-568-tombstone"},
                json={
                    "expected_current_revision_id": uploaded["source_revision_id"],
                    "deletion_reason": "source_deleted",
                },
            )
            tombstoned = await client.get(path, params=query, headers=headers)

        migration_engine = create_async_engine(
            postgresql_contract_database.migration_url.get_secret_value(),
            hide_parameters=True,
        )
        try:
            async with migration_engine.connect() as connection:
                expected = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT d.source_revision_digest, d.original_sha256, "
                                "d.artifact_id, d.artifact_digest, d.parser_name, "
                                "d.parser_version, d.parser_config_reference, "
                                "d.locator_contract_version, s.locator "
                                "FROM spine.parsed_documents d "
                                "JOIN spine.parsed_document_segments s "
                                "ON s.artifact_id = d.artifact_id "
                                "WHERE d.source_revision_id = :revision_id "
                                "ORDER BY s.segment_ordinal LIMIT 1"
                            ),
                            {"revision_id": UUID(uploaded["source_revision_id"])},
                        )
                    ).mappings().one()
                )
                object_audits = tuple(
                    dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT outcome, payload FROM spine.audit_events "
                                "WHERE event_type = 'access.decision' "
                                "AND schema_version = 3 "
                                "AND target_id = :source_object_id "
                                "ORDER BY appended_at, audit_event_id"
                            ),
                            {"source_object_id": UUID(uploaded["source_object_id"])},
                        )
                    ).mappings()
                )
        finally:
            await migration_engine.dispose()
    finally:
        await application.state.production_engine.dispose()

    return ProductionParsedContentScenario(
        allowed=allowed,
        unavailable=(foreign, missing, tombstoned),
        tombstone=tombstone,
        uploaded=uploaded,
        expected=expected,
        object_audits=object_audits,
        content=content,
    )


async def test_current_uploaded_revision_exposes_exact_artifact_metadata(
    production_parsed_content_scenario: ProductionParsedContentScenario,
) -> None:
    scenario = production_parsed_content_scenario
    assert scenario.allowed.status_code == 200, scenario.allowed.text
    data = scenario.allowed.json()["data"]
    uploaded = scenario.uploaded
    expected = scenario.expected
    assert data["status"] == "ready"
    assert data["source_object_id"] == uploaded["source_object_id"]
    assert data["source_revision_id"] == uploaded["source_revision_id"]
    assert data["source_revision_digest"] == expected["source_revision_digest"]
    assert data["original_sha256"] == hashlib.sha256(scenario.content).hexdigest()
    assert data["processing_receipt_id"] == uploaded["processing_receipt_id"]
    assert data["artifact_id"] == str(expected["artifact_id"])
    assert data["artifact_digest"] == expected["artifact_digest"]
    assert data["parser_name"] == expected["parser_name"]
    assert data["parser_version"] == expected["parser_version"]
    assert data["parser_config_reference"] == expected["parser_config_reference"]
    assert data["locator_contract_version"] == expected["locator_contract_version"]
    assert data["items"][0]["locator"] == expected["locator"]
    assert data["items"][0]["locator"]["kind"] == "physical_line"
    assert data["items"][0]["text"] == "# Current handbook"


async def test_foreign_missing_and_tombstoned_revisions_do_not_leak_content(
    production_parsed_content_scenario: ProductionParsedContentScenario,
) -> None:
    scenario = production_parsed_content_scenario
    assert scenario.tombstone.status_code == 202, scenario.tombstone.text
    for unavailable in scenario.unavailable:
        assert unavailable.status_code == 200, unavailable.text
        assert unavailable.json()["data"] == {
            "schema_version": "knowledge-parsed-content-inspection:v1",
            "status": "content_unavailable",
            "source_object_id": None,
            "source_revision_id": None,
            "source_revision_digest": None,
            "original_sha256": None,
            "processing_receipt_id": None,
            "artifact_id": None,
            "artifact_digest": None,
            "parser_name": None,
            "parser_version": None,
            "parser_config_reference": None,
            "locator_contract_version": None,
            "items": [],
            "next_cursor": None,
        }
        assert "Production evidence" not in unavailable.text


async def test_object_decisions_persist_immutable_authority_and_policy_lineage(
    production_parsed_content_scenario: ProductionParsedContentScenario,
) -> None:
    scenario = production_parsed_content_scenario
    assert any(row["outcome"] == "allowed" for row in scenario.object_audits)
    assert any(row["outcome"] == "denied" for row in scenario.object_audits)
    allowed_audit = next(
        row for row in scenario.object_audits if row["outcome"] == "allowed"
    )
    assert allowed_audit["payload"]["source_revision_id"] == (
        scenario.uploaded["source_revision_id"]
    )
    assert allowed_audit["payload"]["policy_version_id"] is not None
