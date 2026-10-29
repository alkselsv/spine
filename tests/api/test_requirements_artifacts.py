from __future__ import annotations

from uuid import UUID

import httpx

from spine.api.requirements_artifacts import install_requirements_artifact_routes
from spine.domain.workflows import Requirement, RequirementsArtifact, RequirementState
from tests.api.test_authorization import (
    ENVIRONMENT_ID, OTHER_WORKSPACE_ID, WORKSPACE_ID, authorized_app,
    authorization_entry,
)
from spine.auth import InMemoryAuthorizationDirectory


ARTIFACT_ID = UUID("64000000-0000-4000-8000-000000000001")
RUN_ID = UUID("64000000-0000-4000-8000-000000000002")


def _artifact(workspace_id: UUID = WORKSPACE_ID) -> RequirementsArtifact:
    return RequirementsArtifact(
        artifact_id=ARTIFACT_ID, artifact_version=1,
        production_identity="requirements-extraction-v1", production_digest="a" * 64,
        workspace_id=workspace_id,
        environment_id=(ENVIRONMENT_ID if workspace_id == WORKSPACE_ID else UUID("30000000-0000-0000-0000-000000000074")),
        work_item_id=UUID("64000000-0000-4000-8000-000000000003"), run_id=RUN_ID,
        requirements=(Requirement(requirement_id=UUID("64000000-0000-4000-8000-000000000004"),
                                  category="functional", statement="Friday"),),
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


def _headers(workspace_id: UUID = WORKSPACE_ID) -> dict[str, str]:
    environment = ENVIRONMENT_ID if workspace_id == WORKSPACE_ID else UUID("30000000-0000-0000-0000-000000000074")
    return {"Authorization": "Bearer opaque-provider-token",
            "X-Workspace-ID": str(workspace_id), "X-Environment-ID": str(environment)}


def _app():
    other_entry = authorization_entry().model_copy(update={
        "workspace_id": OTHER_WORKSPACE_ID,
        "environment_id": UUID("30000000-0000-0000-0000-000000000074"),
        "environment_workspace_id": OTHER_WORKSPACE_ID,
    })
    app = authorized_app([], directory=InMemoryAuthorizationDirectory(
        entries=(authorization_entry(), other_entry)
    ))
    service = _Service()
    install_requirements_artifact_routes(app, service=service)  # type: ignore[arg-type]
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
