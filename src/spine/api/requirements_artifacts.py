"""Authorized readback for versioned requirements artifacts."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.requirements_artifacts import PersistedRequirementsArtifactService
from spine.application.requirements_evidence import (
    RequirementsEvidenceInspectionRequester,
    RequirementsEvidenceInspectionResult,
    RequirementsEvidenceInspectionStatus,
    RequirementsEvidenceValidationService,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.artifacts import GoogleSheetCellLocator, Locator
from spine.domain.workflows.requirements import RequirementsArtifact


class RequirementsArtifactVersionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    items: tuple[RequirementsArtifact, ...]


class RequirementsEvidenceInspectionEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["requirements-evidence-inspection-evidence:v1"] = (
        "requirements-evidence-inspection-evidence:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    segment_id: UUID
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_contract_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    locator: Locator | GoogleSheetCellLocator
    text: str = Field(min_length=1, max_length=65_536)
    validation_reference: UUID
    authorization_decision_reference: UUID


class RequirementsEvidenceInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["requirements-evidence-inspection-response:v1"] = (
        "requirements-evidence-inspection-response:v1"
    )
    status: RequirementsEvidenceInspectionStatus
    evidence: RequirementsEvidenceInspectionEvidenceResponse | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RequirementsEvidenceInspectionResponse":
        if self.status is RequirementsEvidenceInspectionStatus.AVAILABLE:
            if self.evidence is None:
                raise ValueError("available evidence inspection requires evidence")
        elif self.evidence is not None:
            raise ValueError("unavailable evidence inspection cannot contain evidence")
        return self


def install_requirements_artifact_routes(
    application: FastAPI,
    *,
    service: PersistedRequirementsArtifactService,
    evidence_service: RequirementsEvidenceValidationService | None = None,
) -> None:
    application.state.requirements_artifact_service = service
    application.state.requirements_evidence_service = evidence_service

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

    if evidence_service is not None:
        async def inspect_evidence(
            artifact_id: UUID,
            requirement_id: UUID,
            segment_id: UUID,
            request: Request,
            artifact_version: int | None = Query(default=None, ge=1),
            context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
            request_context: AuthorizedRequestContext[object] = Depends(
                get_authorized_request_context
            ),
        ) -> ApiResponseEnvelope[RequirementsEvidenceInspectionResponse]:
            artifact = await _service(request).get(
                context=context,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
            )
            if artifact is None:
                return JSONResponse(  # type: ignore[return-value]
                    status_code=404, content={"code": "artifact.not_found"}
                )
            evidence = next(
                (
                    item
                    for requirement in artifact.requirements
                    if requirement.requirement_id == requirement_id
                    for item in requirement.evidence_refs
                    if item.segment_id == segment_id
                ),
                None,
            )
            if evidence is None:
                result = RequirementsEvidenceInspectionResult(
                    status=RequirementsEvidenceInspectionStatus.MISSING
                )
            else:
                result = await _evidence_service(request).inspect(
                    context=context,
                    requester=RequirementsEvidenceInspectionRequester(
                        subject_id=request_context.acting_subject_id,
                        authorization_generation=request_context.authorization_generation,
                        effective_team_ids=request_context.effective_team_ids,
                    ),
                    evidence=evidence,
                )
            return ApiResponseEnvelope(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                trace_id=request_context.diagnostic_context.trace_id,
                data=_evidence_response(result),
            )

        add_protected_api_route(
            application,
            "/api/v1/artifacts/{artifact_id}/requirements/{requirement_id}/evidence/{segment_id}",
            inspect_evidence,
            route_id="artifacts.requirements.evidence.inspect",
            policy=RouteAuthorizationPolicy(
                required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
                purpose=PersistencePurpose("knowledge_control"),
                operation=PersistenceOperation("inspect_parsed_content"),
            ),
            methods=["GET"],
            response_model=ApiResponseEnvelope[RequirementsEvidenceInspectionResponse],
        )


def _service(request: Request) -> PersistedRequirementsArtifactService:
    service = getattr(request.app.state, "requirements_artifact_service", None)
    if not callable(getattr(service, "get", None)):
        raise RuntimeError("Requirements artifact service is unavailable.")
    return cast(PersistedRequirementsArtifactService, service)


def _evidence_service(request: Request) -> RequirementsEvidenceValidationService:
    service = getattr(request.app.state, "requirements_evidence_service", None)
    if not callable(getattr(service, "inspect", None)):
        raise RuntimeError("Requirements evidence inspection service is unavailable.")
    return cast(RequirementsEvidenceValidationService, service)


def _evidence_response(
    result: RequirementsEvidenceInspectionResult,
) -> RequirementsEvidenceInspectionResponse:
    evidence = result.evidence
    return RequirementsEvidenceInspectionResponse(
        status=result.status,
        evidence=(
            None
            if evidence is None
            else RequirementsEvidenceInspectionEvidenceResponse(
                source_object_id=evidence.source_object_id,
                source_revision_id=evidence.source_revision_id,
                segment_id=evidence.segment_id,
                segment_digest=evidence.segment_digest,
                locator_contract_version=evidence.locator_contract_version,
                locator=evidence.locator,
                text=evidence.text,
                validation_reference=evidence.validation_reference,
                authorization_decision_reference=evidence.authorization_decision_reference,
            )
        ),
    )
