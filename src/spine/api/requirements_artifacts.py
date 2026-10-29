"""Authorized readback for versioned requirements artifacts."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import add_protected_api_route, get_authorized_persistence_context
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.requirements_artifacts import PersistedRequirementsArtifactService
from spine.auth import AuthorizationRole, AuthorizationScopeRequirement, RouteAuthorizationPolicy
from spine.domain.workflows.requirements import RequirementsArtifact


class RequirementsArtifactVersionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    items: tuple[RequirementsArtifact, ...]


def install_requirements_artifact_routes(
    application: FastAPI, *, service: PersistedRequirementsArtifactService
) -> None:
    application.state.requirements_artifact_service = service

    async def by_run(
        run_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> RequirementsArtifact:
        artifact = await _service(request).get_for_run(context=context, run_id=run_id)
        if artifact is None:
            return JSONResponse(status_code=404, content={"code": "artifact.not_found"})  # type: ignore[return-value]
        return artifact

    async def detail(
        artifact_id: UUID,
        request: Request,
        artifact_version: int | None = Query(default=None, ge=1),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> RequirementsArtifact:
        artifact = await _service(request).get(
            context=context, artifact_id=artifact_id, artifact_version=artifact_version
        )
        if artifact is None:
            return JSONResponse(status_code=404, content={"code": "artifact.not_found"})  # type: ignore[return-value]
        return artifact

    async def versions(
        artifact_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> RequirementsArtifactVersionsResponse:
        artifacts = await _service(request).versions(context=context, artifact_id=artifact_id)
        if not artifacts:
            return JSONResponse(status_code=404, content={"code": "artifact.not_found"})  # type: ignore[return-value]
        return RequirementsArtifactVersionsResponse(items=artifacts)

    policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.read"),
    )
    for path, endpoint, name in (
        ("/api/v1/runs/{run_id}/requirements", by_run, "requirements.by_run"),
        ("/api/v1/artifacts/{artifact_id}", detail, "artifacts.requirements.read"),
        ("/api/v1/artifacts/{artifact_id}/versions", versions, "artifacts.requirements.versions"),
    ):
        add_protected_api_route(application, path, endpoint, route_id=name, policy=policy,
                                methods=["GET"], response_model=(
                                    RequirementsArtifactVersionsResponse if name.endswith("versions")
                                    else RequirementsArtifact
                                ))


def _service(request: Request) -> PersistedRequirementsArtifactService:
    service = getattr(request.app.state, "requirements_artifact_service", None)
    if not callable(getattr(service, "get", None)):
        raise RuntimeError("Requirements artifact service is unavailable.")
    return cast(PersistedRequirementsArtifactService, service)
