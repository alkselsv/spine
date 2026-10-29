from __future__ import annotations

from uuid import UUID

import httpx

from spine.api.requirements_artifacts import install_requirements_artifact_routes
from spine.application.requirements_evidence import (
    RequirementsEvidenceInspectionEvidence,
    RequirementsEvidenceInspectionResult,
    RequirementsEvidenceInspectionStatus,
)
from spine.domain.artifacts import ArtifactRef, PhysicalLineLocator
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    Requirement,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementsArtifact,
    RequirementState,
)
from tests.api.test_authorization import (
    ENVIRONMENT_ID, OTHER_WORKSPACE_ID, WORKSPACE_ID, authorized_app,
    authorization_entry,
)
from spine.auth import InMemoryAuthorizationDirectory


ARTIFACT_ID = UUID("64000000-0000-4000-8000-000000000001")
RUN_ID = UUID("64000000-0000-4000-8000-000000000002")


SEGMENT_ID = UUID("64000000-0000-4000-8000-000000000005")


def _artifact(workspace_id: UUID = WORKSPACE_ID) -> RequirementsArtifact:
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=UUID("64000000-0000-4000-8000-000000000006"),
        source_revision_id=UUID("64000000-0000-4000-8000-000000000007"),
        provenance_id=UUID("64000000-0000-4000-8000-000000000008"),
    )
    evidence = RequirementEvidenceRef(
        source_ref=source_ref,
        parsed_artifact_ref=ArtifactRef(
            id=UUID("64000000-0000-4000-8000-000000000009"),
            schema_name="parsed-document",
            schema_version="parsed-document:v1",
        ),
        segment_id=SEGMENT_ID,
        segment_digest="a" * 64,
        locator=PhysicalLineLocator(
            kind="physical_line", line_number=4, start_codepoint=0, end_codepoint=6
        ),
    )
    return RequirementsArtifact(
        artifact_id=ARTIFACT_ID, artifact_version=1,
        production_identity="requirements-extraction-v1", production_digest="a" * 64,
        workspace_id=workspace_id,
        environment_id=(ENVIRONMENT_ID if workspace_id == WORKSPACE_ID else UUID("30000000-0000-0000-0000-000000000074")),
        work_item_id=UUID("64000000-0000-4000-8000-000000000003"), run_id=RUN_ID,
        requirements=(Requirement(
            requirement_id=UUID("64000000-0000-4000-8000-000000000004"),
            category="functional", statement="Friday", source_refs=(source_ref,),
            evidence_refs=(evidence,), evidence_status=RequirementEvidenceStatus.UNVERIFIED,
        ),),
        state=RequirementState.CANDIDATE,
    )


class _Service:
    def __init__(self) -> None:
        self.artifact = _artifact()

    async def get(self, *, context, artifact_id, artifact_version=None):
        scope = context.scope
        if (artifact_id != ARTIFACT_ID or scope.workspace_id != self.artifact.workspace_id
                or scope.environment_id != self.artifact.environment_id
                or artifact_version not in (None, 1)):
            return None
        return self.artifact

    async def versions(self, *, context, artifact_id):
        item = await self.get(context=context, artifact_id=artifact_id)
        return (item,) if item else ()

    async def get_for_run(self, *, context, run_id):
        if run_id != RUN_ID:
            return None
        return await self.get(context=context, artifact_id=ARTIFACT_ID)


class _EvidenceService:
    def __init__(self, status: RequirementsEvidenceInspectionStatus) -> None:
        self.status = status

    async def inspect(self, **_kwargs) -> RequirementsEvidenceInspectionResult:
        if self.status is not RequirementsEvidenceInspectionStatus.AVAILABLE:
            return RequirementsEvidenceInspectionResult(status=self.status)
        return RequirementsEvidenceInspectionResult(
            status=RequirementsEvidenceInspectionStatus.AVAILABLE,
            evidence=RequirementsEvidenceInspectionEvidence(
                source_object_id=UUID("64000000-0000-4000-8000-000000000006"),
                source_revision_id=UUID("64000000-0000-4000-8000-000000000007"),
                segment_id=SEGMENT_ID,
                segment_digest="a" * 64,
                locator_contract_version="locator:v1",
                locator=PhysicalLineLocator(
                    kind="physical_line", line_number=4, start_codepoint=0, end_codepoint=6
                ),
                text="protected text",
                validation_reference=UUID("64000000-0000-4000-8000-000000000010"),
                authorization_decision_reference=UUID("64000000-0000-4000-8000-000000000011"),
            ),
        )


def _headers(workspace_id: UUID = WORKSPACE_ID) -> dict[str, str]:
    environment = ENVIRONMENT_ID if workspace_id == WORKSPACE_ID else UUID("30000000-0000-0000-0000-000000000074")
    return {"Authorization": "Bearer opaque-provider-token",
            "X-Workspace-ID": str(workspace_id), "X-Environment-ID": str(environment)}


def _app(
    evidence_service: _EvidenceService | None = None,
):
    other_entry = authorization_entry().model_copy(update={
        "workspace_id": OTHER_WORKSPACE_ID,
        "environment_id": UUID("30000000-0000-0000-0000-000000000074"),
        "environment_workspace_id": OTHER_WORKSPACE_ID,
    })
    app = authorized_app([], directory=InMemoryAuthorizationDirectory(
        entries=(authorization_entry(), other_entry)
    ))
    service = _Service()
    install_requirements_artifact_routes(
        app, service=service, evidence_service=evidence_service  # type: ignore[arg-type]
    )
    return app


async def test_artifact_api_returns_exact_version_and_history() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(f"/api/v1/artifacts/{ARTIFACT_ID}", headers=_headers())
        versions = await client.get(f"/api/v1/artifacts/{ARTIFACT_ID}/versions", headers=_headers())
        assert response.status_code == 200
        assert response.json()["artifact_version"] == 1
        assert response.json()["requirements"][0]["statement"] == "Friday"
        exact = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}?artifact_version=1", headers=_headers()
        )
        missing = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}?artifact_version=2", headers=_headers()
        )
        assert versions.status_code == 200
        assert versions.json()["items"][0]["artifact_version"] == 1
        assert exact.status_code == 200
        assert missing.status_code == 404


async def test_artifact_api_hides_cross_workspace_records_as_not_found() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(f"/api/v1/artifacts/{ARTIFACT_ID}", headers=_headers(OTHER_WORKSPACE_ID))
        assert response.status_code == 404
        assert "Friday" not in response.text


async def test_run_artifact_readback_restores_requirements_without_request_content() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(f"/api/v1/runs/{RUN_ID}/requirements", headers=_headers())
        assert response.status_code == 200
        assert response.json()["run_id"] == str(RUN_ID)
        assert response.json()["requirements"][0]["statement"] == "Friday"


async def test_evidence_inspection_returns_protected_text_only_when_allowed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(_EvidenceService(RequirementsEvidenceInspectionStatus.AVAILABLE))
        ),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/requirements/64000000-0000-4000-8000-000000000004/evidence/{SEGMENT_ID}",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "available"
    assert response.json()["data"]["evidence"]["text"] == "protected text"


async def test_evidence_inspection_denial_has_no_protected_payload() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(_EvidenceService(RequirementsEvidenceInspectionStatus.DENIED))
        ),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/requirements/64000000-0000-4000-8000-000000000004/evidence/{SEGMENT_ID}",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "denied"
    assert response.json()["data"]["evidence"] is None
    assert "protected text" not in response.text


async def test_evidence_inspection_hides_cross_workspace_artifact() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(_EvidenceService(RequirementsEvidenceInspectionStatus.AVAILABLE))
        ),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/artifacts/{ARTIFACT_ID}/requirements/64000000-0000-4000-8000-000000000004/evidence/{SEGMENT_ID}",
            headers=_headers(OTHER_WORKSPACE_ID),
        )

    assert response.status_code == 404
    assert "protected text" not in response.text
    assert str(UUID("64000000-0000-4000-8000-000000000006")) not in response.text
