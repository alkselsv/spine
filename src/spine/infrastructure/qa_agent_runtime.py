"""Durable OpenAI-compatible runtime for governed Q&A candidate artifacts."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import text

from spine.agents.runtime.artifacts import PersistedAgentInvocationResult
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.domain.agents import AgentInvocation, AgentResult, AgentRunStatus, AgentVersion
from spine.domain.artifacts import Artifact, ArtifactState
from spine.domain.capabilities import (
    AnswerClaim,
    AnswerClaimKind,
    AnswerClaimLocation,
    AnswerContent,
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
)
from spine.domain.capabilities.semantic_support import (
    SemanticSupportAssessment,
    SemanticSupportOutcome,
    SemanticSupportReasonCode,
    SemanticSupportRequest,
)
from spine.domain.common import ActorKind, ActorRef, SchemaRef


_SEMANTIC_SUPPORT_SYSTEM_PROMPT = (
    "Determine whether the claim is fully supported by the supplied evidence. "
    "Return JSON with outcome equal to supported, unsupported, or inconclusive. "
    "A claim that the documents are insufficient, omit a fact, or do not mention "
    "something is not supported unless the evidence explicitly establishes that absence. "
    "Do not add explanations or infer facts absent from the evidence."
)


class PostgreSQLQAArtifactStore:
    """Tenant-scoped immutable artifacts needed by durable Q&A recovery."""

    def __init__(self, session_factory: object, *, service_principal_id: UUID) -> None:
        self._session_factory = session_factory
        self._service_principal_id = service_principal_id

    async def persist_input(
        self,
        *,
        input_value: AnswerQuestionCandidateInput,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
        idempotency_key: str,
    ) -> UUID:
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-input:{workspace_id}:{environment_id}:{work_item_id}:{idempotency_key}",
        )
        artifact = Artifact(
            id=artifact_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            work_item_id=work_item_id,
            produced_by=ActorRef(kind=ActorKind.SERVICE, id=self._service_principal_id),
            artifact_schema=SchemaRef(
                name="answer-question.candidate-input", version="v1"
            ),
            content=input_value.model_dump(mode="json"),
        )
        await self._persist_artifact(artifact)
        return artifact_id

    async def persist_candidate(
        self,
        *,
        candidate: AnswerQuestionCandidateOutput,
        invocation: AgentInvocation,
        agent_version: AgentVersion,
        input_artifact_id: UUID,
    ) -> tuple[Artifact, UUID, AgentResult]:
        scope = candidate.question.scope
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-output:{scope.workspace_id}:{scope.environment_id}:"
            f"{invocation.work_item_id}:{invocation.idempotency_key}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-agent-result:{scope.workspace_id}:{scope.environment_id}:"
            f"{invocation.step_run_id}:{invocation.idempotency_key}",
        )
        evidence_ids = tuple(
            citation.evidence_id for citation in candidate.answer.citations
        )
        artifact = Artifact(
            id=artifact_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            work_item_id=invocation.work_item_id,
            produced_by=ActorRef(kind=ActorKind.AGENT, id=agent_version.agent_id),
            produced_by_agent_version_id=agent_version.id,
            artifact_schema=SchemaRef(
                name="answer-question.candidate-output", version="v1"
            ),
            content=candidate.model_dump(mode="json"),
            evidence_refs=evidence_ids,
            upstream_artifact_ids=(input_artifact_id,),
        )
        result = AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=(artifact_id,),
            evidence_refs=evidence_ids,
            trace_ref=f"qa-agent-result:{result_id}",
        )
        await self._persist_artifact(artifact)
        await self._persist_result(
            result_id=result_id,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            work_item_id=invocation.work_item_id,
            result=result,
        )
        return artifact, result_id, result

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None:
        row = await self._one(
            "SELECT artifact FROM spine.qa_agent_artifacts "
            "WHERE artifact_id=:identifier AND work_item_id=:work_item_id",
            workspace_id=workspace_id,
            environment_id=environment_id,
            parameters={"identifier": str(artifact_id), "work_item_id": str(work_item_id)},
        )
        return None if row is None else Artifact.model_validate(row.artifact)

    async def load_agent_result(
        self,
        result_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> AgentResult | None:
        row = await self._one(
            "SELECT result FROM spine.qa_agent_results "
            "WHERE result_id=:identifier AND work_item_id=:work_item_id",
            workspace_id=workspace_id,
            environment_id=environment_id,
            parameters={"identifier": str(result_id), "work_item_id": str(work_item_id)},
        )
        return None if row is None else AgentResult.model_validate(row.result)

    async def verify_lineage(
        self,
        artifact: Artifact,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> None:
        for upstream_id in artifact.upstream_artifact_ids:
            upstream = await self.load(
                upstream_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                work_item_id=work_item_id,
            )
            if upstream is None or upstream.state is not ArtifactState.ACTIVE:
                raise RuntimeError("Q&A artifact lineage is unavailable")

    async def _persist_artifact(self, artifact: Artifact) -> None:
        await self._insert_immutable(
            table="qa_agent_artifacts",
            identity_column="artifact_id",
            identity=artifact.id,
            workspace_id=artifact.workspace_id,
            environment_id=artifact.environment_id,
            work_item_id=artifact.work_item_id,
            payload_column="artifact",
            payload=artifact.model_dump(mode="json"),
        )

    async def _persist_result(
        self,
        *,
        result_id: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
        result: AgentResult,
    ) -> None:
        await self._insert_immutable(
            table="qa_agent_results",
            identity_column="result_id",
            identity=result_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            work_item_id=work_item_id,
            payload_column="result",
            payload=result.model_dump(mode="json"),
        )

    async def _insert_immutable(
        self,
        *,
        table: str,
        identity_column: str,
        identity: UUID,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
        payload_column: str,
        payload: object,
    ) -> None:
        session = self._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                inserted = await session.execute(
                    text(
                        f"INSERT INTO spine.{table} "
                        f"({identity_column}, workspace_id, environment_id, work_item_id, {payload_column}) "
                        f"VALUES (:identity, :workspace_id, :environment_id, :work_item_id, CAST(:payload AS jsonb)) "
                        f"ON CONFLICT ({identity_column}) DO NOTHING RETURNING {identity_column}"
                    ),
                    {
                        "identity": str(identity),
                        "workspace_id": str(workspace_id),
                        "environment_id": str(environment_id),
                        "work_item_id": str(work_item_id),
                        "payload": json.dumps(payload, ensure_ascii=False),
                    },
                )
                if inserted.scalar_one_or_none() is None:
                    row = (
                        await session.execute(
                            text(
                                f"SELECT {payload_column} FROM spine.{table} "
                                f"WHERE {identity_column}=:identity"
                            ),
                            {"identity": str(identity)},
                        )
                    ).mappings().one_or_none()
                    if row is None or row[payload_column] != payload:
                        raise RuntimeError("immutable Q&A artifact identity conflicts")

    async def _one(
        self,
        statement: str,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        parameters: dict[str, str],
    ) -> object | None:
        session = self._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await self._bind_scope(session, workspace_id, environment_id)
                return (await session.execute(text(statement), parameters)).mappings().one_or_none()

    @staticmethod
    async def _bind_scope(session: object, workspace_id: UUID, environment_id: UUID) -> None:
        await session.execute(  # type: ignore[attr-defined]
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true)"
            ),
            {
                "workspace_id": str(workspace_id),
                "environment_id": str(environment_id),
            },
        )


class ConfiguredLLMRuntimeProvider:
    def __init__(
        self,
        *,
        artifacts: PostgreSQLQAArtifactStore,
        api_key: str,
        model: str,
        endpoint: str,
    ) -> None:
        if not api_key or not model:
            raise RuntimeError("configured Q&A LLM is unavailable")
        self._artifacts = artifacts
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint.rstrip("/")

    def runtime_for(self, **values: object) -> "ConfiguredLLMRuntime":
        pin = values.get("pin")
        workspace_id = getattr(pin, "workspace_id", None)
        environment_id = getattr(pin, "environment_id", None)
        if not isinstance(workspace_id, UUID) or not isinstance(environment_id, UUID):
            raise RuntimeError("configured Q&A runtime scope is unavailable")
        return ConfiguredLLMRuntime(
            artifacts=self._artifacts,
            api_key=self._api_key,
            model=self._model,
            endpoint=self._endpoint,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )


class ConfiguredLLMRuntime:
    def __init__(
        self,
        *,
        artifacts: PostgreSQLQAArtifactStore,
        api_key: str,
        model: str,
        endpoint: str,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> None:
        self._artifacts = artifacts
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._workspace_id = workspace_id
        self._environment_id = environment_id

    async def invoke_with_reference(
        self, version: AgentVersion, invocation: AgentInvocation
    ) -> PersistedAgentInvocationResult:
        if len(invocation.input_artifact_ids) != 1:
            raise RuntimeError("Q&A candidate input is unavailable")
        artifact = await self._artifacts.load(
            invocation.input_artifact_ids[0],
            workspace_id=self._workspace_id,
            environment_id=self._environment_id,
            work_item_id=invocation.work_item_id,
        )
        if artifact is None:
            raise RuntimeError("Q&A candidate input is unavailable")
        candidate_input = AnswerQuestionCandidateInput.model_validate(artifact.content)
        evidence_text = await self._evidence_text(candidate_input)
        answer, evidence_ids = await self._complete(candidate_input, evidence_text)
        candidate = _candidate(candidate_input, answer, evidence_ids)
        _, result_id, result = await self._artifacts.persist_candidate(
            candidate=candidate,
            invocation=invocation,
            agent_version=version,
            input_artifact_id=artifact.id,
        )
        return PersistedAgentInvocationResult(
            result=result,
            reference=OpaqueResultReference(
                result_type="agent_invocation",
                result_id=result_id,
                schema_version=1,
            ),
        )

    async def invoke(self, version: AgentVersion, invocation: AgentInvocation) -> AgentResult:
        return (await self.invoke_with_reference(version, invocation)).result

    async def cancel(self) -> None:
        return None

    async def _evidence_text(
        self, value: AnswerQuestionCandidateInput
    ) -> dict[str, str]:
        ids = tuple(item.evidence_id for item in value.context_bundle.evidence_references)
        if not ids:
            return {}
        session = self._artifacts._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await self._artifacts._bind_scope(
                    session,
                    value.question.scope.workspace_id,
                    value.question.scope.environment_id,
                )
                rows = (
                    await session.execute(
                        text(
                            "SELECT CONCAT('segment-', REPLACE(CAST(segment_id AS text), '-', '')) "
                            "AS evidence_id, text "
                            "FROM spine.parsed_document_segments "
                            "WHERE workspace_id=:workspace_id AND environment_id=:environment_id "
                            "AND CONCAT('segment-', REPLACE(CAST(segment_id AS text), '-', '')) "
                            "= ANY(CAST(:evidence_ids AS text[]))"
                        ),
                        {
                            "workspace_id": str(value.question.scope.workspace_id),
                            "environment_id": str(value.question.scope.environment_id),
                            "evidence_ids": list(ids),
                        },
                    )
                ).mappings().all()
        return {row.evidence_id: row.text for row in rows if row.text.strip()}

    async def _complete(
        self,
        value: AnswerQuestionCandidateInput,
        evidence: dict[str, str],
    ) -> tuple[str, tuple[str, ...]]:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Answer only from the supplied evidence. Return JSON with keys "
                        "answer (non-empty string) and evidence_ids (non-empty array of exact IDs). "
                        "If the evidence does not answer the question, answer that the available "
                        "documents are insufficient and still cite the closest evidence."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": value.question.subject.text,
                            "evidence": evidence,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self._endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        decoded = json.loads(content)
        answer = str(decoded.get("answer", "")).strip()
        selected = tuple(
            str(item)
            for item in decoded.get("evidence_ids", ())
            if str(item) in evidence
        )
        if not answer or not selected:
            raise RuntimeError("configured Q&A LLM returned invalid grounded output")
        return answer, tuple(dict.fromkeys(selected))


class ConfiguredSemanticSupportValidator:
    """Perform the mandatory second-pass grounding check with the configured LLM."""

    def __init__(self, *, api_key: str, model: str, endpoint: str) -> None:
        if not api_key or not model:
            raise RuntimeError("configured semantic validator is unavailable")
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint.rstrip("/")

    async def assess(self, request: SemanticSupportRequest) -> SemanticSupportAssessment:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": _SEMANTIC_SUPPORT_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "claim": request.claim.text,
                            "evidence": [item.content for item in request.evidence],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self._endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
        decoded = json.loads(response.json()["choices"][0]["message"]["content"])
        raw_outcome = str(decoded.get("outcome", "")).strip().lower()
        outcome = {
            "supported": SemanticSupportOutcome.SUPPORTED,
            "unsupported": SemanticSupportOutcome.UNSUPPORTED,
            "inconclusive": SemanticSupportOutcome.INCONCLUSIVE,
        }.get(raw_outcome)
        if outcome is None:
            raise RuntimeError("configured semantic validator returned an invalid outcome")
        reason = {
            SemanticSupportOutcome.SUPPORTED: SemanticSupportReasonCode.SUPPORTED_BY_EVIDENCE,
            SemanticSupportOutcome.UNSUPPORTED: SemanticSupportReasonCode.UNSUPPORTED_CLAIM,
            SemanticSupportOutcome.INCONCLUSIVE: (
                SemanticSupportReasonCode.INSUFFICIENT_OR_CONTRADICTORY_EVIDENCE
            ),
        }[outcome]
        return SemanticSupportAssessment(
            claim_id=request.claim.claim_id,
            outcome=outcome,
            reason_code=reason,
            evidence_ids=tuple(item.citation.evidence_id for item in request.evidence),
            relationship_ids=tuple(item.relationship_id for item in request.relationships),
        )


def _candidate(
    value: AnswerQuestionCandidateInput,
    answer_text: str,
    evidence_ids: tuple[str, ...],
) -> AnswerQuestionCandidateOutput:
    question = value.question
    bundle = value.context_bundle
    answer_id = uuid5(question.correlation.run_id, "accepted-answer")
    claim_id = uuid5(answer_id, "claim-1")
    references = {
        item.evidence_id: item for item in bundle.evidence_references
    }
    citations = tuple(
        Citation(
            citation_id=uuid5(answer_id, f"citation:{evidence_id}"),
            answer_id=answer_id,
            context_bundle=bundle.lineage_reference,
            evidence_reference=references[evidence_id],
        )
        for evidence_id in evidence_ids
    )
    relationship_kind = (
        ClaimEvidenceRelationshipKind.INDEPENDENT
        if len(citations) == 1
        else ClaimEvidenceRelationshipKind.JOINT
    )
    claim = AnswerClaim(
        claim_id=claim_id,
        workspace_id=question.scope.workspace_id,
        environment_id=question.scope.environment_id,
        run_id=question.correlation.run_id,
        attempt_id=question.correlation.attempt_id,
        kind=AnswerClaimKind.FACTUAL,
        text=answer_text,
        location=AnswerClaimLocation(
            answer_id=answer_id, start_offset=0, end_offset=len(answer_text)
        ),
    )
    relationship = ClaimEvidenceRelationship(
        relationship_id=uuid5(answer_id, "relationship-1"),
        answer_id=answer_id,
        workspace_id=question.scope.workspace_id,
        environment_id=question.scope.environment_id,
        run_id=question.correlation.run_id,
        attempt_id=question.correlation.attempt_id,
        claim_id=claim_id,
        kind=relationship_kind,
        citation_ids=tuple(item.citation_id for item in citations),
    )
    return AnswerQuestionCandidateOutput(
        question=question,
        context_bundle=bundle.lineage_reference,
        answer=AnswerContent(
            answer_id=answer_id,
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            run_id=question.correlation.run_id,
            attempt_id=question.correlation.attempt_id,
            text=answer_text,
            claims=(claim,),
            citations=citations,
            relationships=(relationship,),
        ),
    )


__all__ = [
    "ConfiguredLLMRuntimeProvider",
    "ConfiguredSemanticSupportValidator",
    "PostgreSQLQAArtifactStore",
]
