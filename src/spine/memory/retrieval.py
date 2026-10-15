"""Execute broker-owned physical read plans through the existing read port."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID

from spine.domain.projections import SourceProjectionReference

from .contracts import (
    CanonicalBoundary,
    ContextProjection,
    ProjectionCandidate,
    ProjectionChunk,
    ProjectionDiagnostic,
    ProjectionEntity,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionOutcome,
    ProjectionRelation,
    ProjectionRetrievalResult,
    ProjectionConfigVersion,
    RetrievalReadContext,
    RetrievalSource,
    RetrieveCommand,
)
from .read_plan import (
    PhysicalReadOperation,
    PhysicalReadPlan,
    PhysicalReadPlanOutcome,
    PhysicalReadPlanReason,
    PhysicalReadPlanResolution,
)


class StructuredRetrievalProvider(Protocol):
    """The existing provider-neutral ContextProjection read seam."""

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult: ...


class PhysicalReadPlanExecutor:
    """Fan out typed reads and merge only structured, pinned results."""

    def __init__(self, provider: StructuredRetrievalProvider | ContextProjection) -> None:
        self._provider = provider
        self._replays: dict[str, ProjectionRetrievalResult] = {}

    async def execute(
        self,
        plan_or_resolution: PhysicalReadPlan | PhysicalReadPlanResolution,
    ) -> ProjectionRetrievalResult:
        """Execute a ready plan, or return a safe result for a rejected plan."""

        if isinstance(plan_or_resolution, PhysicalReadPlanResolution):
            if (
                plan_or_resolution.outcome is not PhysicalReadPlanOutcome.READY
                or plan_or_resolution.plan is None
            ):
                return self._plan_failure(plan_or_resolution)
            plan = plan_or_resolution.plan
        else:
            plan = plan_or_resolution

        plan_digest = self._digest(plan)
        previous = self._replays.get(plan_digest)
        if previous is not None:
            return previous.model_copy(update={"outcome": ProjectionOutcome.REPLAYED})

        invalid_plan = self._validate_plan(plan)
        if invalid_plan is not None:
            result = self._failure_result(
                plan_digest,
                invalid_plan,
            )
            self._replays[plan_digest] = result
            return result

        results: list[ProjectionRetrievalResult] = []
        for operation in plan.operations:
            try:
                command = self._command(plan, operation)
            except Exception:
                result = self._failure_result(
                    plan_digest,
                    ProjectionFailureCode.INCOMPLETE_EVIDENCE,
                )
                self._replays[plan_digest] = result
                return result
            try:
                result = await self._provider.retrieve(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = self._failure(command, ProjectionFailureCode.PHYSICAL_FAILURE)
            results.append(self._attach_context(result, command.read_context))

        merged = self._merge(plan, plan_digest, results)
        if merged.outcome is not ProjectionOutcome.PARTIAL:
            self._replays[plan_digest] = merged
        return merged

    async def retrieve(
        self,
        plan_or_resolution: PhysicalReadPlan | PhysicalReadPlanResolution,
    ) -> ProjectionRetrievalResult:
        """Compatibility spelling for callers that already use ``retrieve``."""

        return await self.execute(plan_or_resolution)

    @classmethod
    def _command(
        cls,
        plan: PhysicalReadPlan,
        operation: PhysicalReadOperation,
    ) -> RetrieveCommand:
        pin = plan.context.publication_pin
        config = pin.projection_config_version
        candidate = ProjectionCandidate(
            candidate_id=operation.projection_run.projection_candidate_id,
            run_id=operation.projection_run.projection_run_id,
            partition_id=operation.projection_partition.projection_partition_id,
            snapshot_id=operation.projection_snapshot_id,
        )
        provider_config = ProjectionConfigVersion(
            config_id=config.projection_config_version_id,
            version=str(config.version),
            config_digest=config.config_digest,
            ontology_version=str(config.ontology_version_id),
            adapter_contract_version=config.adapter_contract_version,
        )
        boundary = CanonicalBoundary(
            boundary_id=pin.canonical_boundary.boundary_id,
            boundary_digest=pin.canonical_boundary.boundary_digest,
            accepted_through_generation=pin.canonical_boundary.accepted_through_generation,
        )
        read_context = RetrievalReadContext(
            workspace_id=operation.workspace_id,
            environment_id=operation.environment_id,
            publication_pin_id=operation.publication_pin_id,
            publication_pin_digest=operation.publication_pin_digest,
            projection_snapshot_id=operation.projection_snapshot_id,
            projection_activation_id=operation.projection_activation_id,
            activation_generation=operation.activation_generation,
            canonical_boundary_id=pin.canonical_boundary.boundary_id,
            canonical_boundary_digest=pin.canonical_boundary.boundary_digest,
            projection_run_id=operation.projection_run.projection_run_id,
            projection_candidate_id=operation.projection_run.projection_candidate_id,
            projection_partition_id=operation.projection_partition.projection_partition_id,
            projection_receipt_id=operation.projection_receipt.projection_receipt_id,
            physical_namespace=operation.physical_namespace.namespace,
            physical_namespace_digest=operation.physical_namespace.namespace_digest,
            source_object_ids=operation.source_object_ids,
            source_revision_ids=operation.source_revision_ids,
            provider_capability_reference=(
                operation.source_isolation.provider_capability_reference
            ),
            source_isolation_pre_retrieval_enforced=(
                operation.source_isolation.pre_retrieval_enforced
            ),
        )
        source_references = {
            (reference.source_object_id, reference.source_revision_id): reference
            for reference in operation.source_references
        }
        retrieval_sources = tuple(
            cls._retrieval_source(
                source_object_id,
                source_revision_id,
                source_references,
            )
            for source_object_id, source_revision_id in zip(
                operation.source_object_ids,
                operation.source_revision_ids,
            )
        )
        command_identity = (
            f"retrieve-{operation.projection_partition.projection_partition_id.hex}"
        )
        command_payload = {
            "context": read_context.model_dump(mode="json"),
            "query": operation.query,
            "limit": min(operation.result_limit, plan.result_limit),
        }
        command_digest = sha256(
            json.dumps(command_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return RetrieveCommand(
            workspace_id=operation.workspace_id,
            environment_id=operation.environment_id,
            projection_run_id=operation.projection_run.projection_run_id,
            projection_candidate=candidate,
            projection_config=provider_config,
            canonical_boundary=boundary,
            physical_namespace=operation.physical_namespace,
            command_identity=command_identity,
            command_digest=command_digest,
            query=operation.query,
            limit=min(operation.result_limit, plan.result_limit),
            retrieval_sources=retrieval_sources,
            read_context=read_context,
        )

    @staticmethod
    def _retrieval_source(
        source_object_id: UUID,
        source_revision_id: UUID,
        references: dict[tuple[UUID, UUID], SourceProjectionReference],
    ) -> RetrievalSource:
        reference = references[(source_object_id, source_revision_id)]
        return RetrievalSource(
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            processing_receipt_id=reference.processing_receipt_id,
            locator_contract_version=reference.locator_schema_version,
        )

    @staticmethod
    def _attach_context(
        result: ProjectionRetrievalResult,
        context: RetrievalReadContext | None,
    ) -> ProjectionRetrievalResult:
        if context is None:
            return result
        return result.model_copy(
            update={
                "chunks": tuple(
                    item.model_copy(
                        update={"read_context": context, "read_contexts": (context,)}
                    )
                    for item in result.chunks
                ),
                "entities": tuple(
                    item.model_copy(
                        update={"read_context": context, "read_contexts": (context,)}
                    )
                    for item in result.entities
                ),
                "relations": tuple(
                    item.model_copy(
                        update={"read_context": context, "read_contexts": (context,)}
                    )
                    for item in result.relations
                ),
                "read_context": context,
            }
        )

    @classmethod
    def _merge(
        cls,
        plan: PhysicalReadPlan,
        plan_digest: str,
        results: list[ProjectionRetrievalResult],
    ) -> ProjectionRetrievalResult:
        successful = [
            result
            for result in results
            if result.outcome
            in {
                ProjectionOutcome.SUCCEEDED,
                ProjectionOutcome.REPLAYED,
                ProjectionOutcome.PARTIAL,
            }
        ]
        failures = [
            failure
            for result in results
            for failure in (
                (result.failure,) if result.failure is not None else ()
            ) + result.partial_failures
        ]
        contexts = tuple(
            result.read_context
            for result in results
            if result.read_context is not None
        )
        unique_contexts = tuple(dict.fromkeys(contexts))
        chunks = cls._merge_items(
            (item for result in successful for item in result.chunks),
            key=lambda item: item.chunk_id,
        )
        entities = cls._merge_items(
            (item for result in successful for item in result.entities),
            key=lambda item: item.entity_id,
        )
        relations = cls._merge_items(
            (item for result in successful for item in result.relations),
            key=lambda item: item.relation_id,
        )
        evidence = cls._merge_evidence(
            reference
            for item in (*chunks, *entities, *relations)
            for reference in item.evidence
        )
        if failures and successful:
            outcome = ProjectionOutcome.PARTIAL
            failure = ProjectionFailure(
                code=ProjectionFailureCode.PARTIAL_FAILURE,
                retryable=True,
                safe_detail=ProjectionDiagnostic(),
            )
        elif failures and not successful:
            outcome = results[0].outcome
            failure = failures[0]
        elif not chunks and not entities and not relations:
            outcome = results[0].outcome if results else ProjectionOutcome.MISSING
            failure = results[0].failure if results else None
        else:
            outcome = ProjectionOutcome.SUCCEEDED
            failure = None
        return ProjectionRetrievalResult(
            outcome=outcome,
            command_identity=f"retrieve-plan-{plan_digest[:32]}",
            command_digest=plan_digest,
            chunks=chunks,
            entities=entities,
            relations=relations,
            evidence=evidence,
            retrieval_strategy=plan.context.authorized_scope.retrieval_strategy.value,
            logical_projection_version=str(
                plan.context.publication_pin.projection_snapshot.projection_snapshot_id
            ),
            physical_projection_version=",".join(
                context.physical_namespace_digest
                for context in unique_contexts
            ),
            read_context=unique_contexts[0] if len(unique_contexts) == 1 else None,
            read_contexts=unique_contexts,
            partial_failures=tuple(failures),
            failure=failure,
        )

    @classmethod
    def _merge_items(cls, items: Iterable[object], *, key) -> tuple[object, ...]:
        merged: list[object] = []
        for item in items:
            match = next(
                (
                    index
                    for index, existing in enumerate(merged)
                    if key(existing) == key(item)
                    and cls._compatible_items(existing, item)
                ),
                None,
            )
            if match is None:
                merged.append(item)
                continue
            existing = merged[match]
            references = cls._merge_evidence(
                (*existing.evidence, *item.evidence)
            )
            score = existing.score or item.score
            contexts = tuple(dict.fromkeys((*existing.read_contexts, *item.read_contexts)))
            merged[match] = existing.model_copy(
                update={"evidence": references, "score": score, "read_contexts": contexts}
            )
        contracts = {
            (
                item.score.metric,
                item.score.direction,
                item.score.provider_contract_version,
            )
            for item in merged
            if item.score is not None
        }
        comparable_scores = len(contracts) == 1 and all(
            item.score is not None for item in merged
        )
        if comparable_scores:
            direction = next(iter(contracts))[1]
            return tuple(
                sorted(
                    merged,
                    key=lambda item: (
                        -item.score.value if direction == "higher_is_better" else item.score.value,
                        item.read_context.projection_partition_id.hex
                        if item.read_context is not None
                        else "",
                        key(item),
                    ),
                )
            )
        return tuple(
            sorted(
                merged,
                key=lambda item: (
                    item.read_context.projection_partition_id.hex
                    if item.read_context is not None
                    else "",
                    key(item),
                ),
            )
        )

    @staticmethod
    def _compatible_items(left: object, right: object) -> bool:
        return (
            left.model_copy(
                update={
                    "evidence": (),
                    "score": None,
                    "read_context": None,
                    "read_contexts": (),
                }
            )
            == right.model_copy(
                update={
                    "evidence": (),
                    "score": None,
                    "read_context": None,
                    "read_contexts": (),
                }
            )
            and (
                left.score is None
                or right.score is None
                or left.score == right.score
            )
        )

    @staticmethod
    def _merge_evidence(items: Iterable[object]) -> tuple[object, ...]:
        result: list[object] = []
        for item in items:
            if item not in result:
                result.append(item)
        return tuple(sorted(result, key=lambda item: (item.evidence_id, item.evidence_digest)))

    @staticmethod
    def _digest(plan: PhysicalReadPlan) -> str:
        payload = plan.model_dump(mode="json")
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _validate_plan(plan: PhysicalReadPlan) -> ProjectionFailureCode | None:
        if not plan.operations:
            return ProjectionFailureCode.INCOMPLETE_EVIDENCE
        if not plan.context.publication_pin.has_valid_digest():
            return ProjectionFailureCode.INCOMPLETE_EVIDENCE
        if not plan.source_isolation.pre_retrieval_enforced:
            return ProjectionFailureCode.SOURCE_ISOLATION_UNPROVEN
        scope = plan.context.authorized_scope
        if (
            plan.context.publication_pin.workspace_id != scope.workspace_id
            or plan.context.publication_pin.environment_id != scope.environment_id
        ):
            return ProjectionFailureCode.INCOMPLETE_EVIDENCE
        for operation in plan.operations:
            if not operation.source_isolation.pre_retrieval_enforced:
                return ProjectionFailureCode.SOURCE_ISOLATION_UNPROVEN
            if operation.physical_namespace.state.value != "active":
                return ProjectionFailureCode.NAMESPACE_UNAVAILABLE
            if (
                operation.workspace_id != scope.workspace_id
                or operation.environment_id != scope.environment_id
                or operation.source_isolation.provider_capability_reference
                != plan.source_isolation.provider_capability_reference
                or set(
                    zip(
                        operation.source_object_ids,
                        operation.source_revision_ids,
                    )
                )
                != set(
                    zip(
                        operation.source_isolation.source_object_ids,
                        operation.source_isolation.source_revision_ids,
                    )
                )
                or not set(
                    zip(
                        operation.source_object_ids,
                        operation.source_revision_ids,
                    )
                ).issubset(
                    set(
                        zip(
                            plan.source_isolation.source_object_ids,
                            plan.source_isolation.source_revision_ids,
                        )
                    )
                )
            ):
                return ProjectionFailureCode.INCOMPLETE_EVIDENCE
        return None

    @staticmethod
    def _plan_failure(
        resolution: PhysicalReadPlanResolution,
    ) -> ProjectionRetrievalResult:
        code = {
            PhysicalReadPlanReason.SOURCE_ISOLATION_UNPROVEN: ProjectionFailureCode.SOURCE_ISOLATION_UNPROVEN,
            PhysicalReadPlanReason.INELIGIBLE_NAMESPACE: ProjectionFailureCode.NAMESPACE_UNAVAILABLE,
        }.get(resolution.reason, ProjectionFailureCode.INCOMPLETE_EVIDENCE)
        return PhysicalReadPlanExecutor._failure_result(resolution.reason.value, code)

    @staticmethod
    def _failure_result(identity: str, code: ProjectionFailureCode) -> ProjectionRetrievalResult:
        digest = sha256(identity.encode()).hexdigest()
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.FAILED,
            command_identity=f"retrieve-failure-{digest[:20]}",
            command_digest=digest,
            failure=ProjectionFailure(
                code=code,
                retryable=code
                in {
                    ProjectionFailureCode.PHYSICAL_FAILURE,
                    ProjectionFailureCode.PARTIAL_FAILURE,
                },
                safe_detail=ProjectionDiagnostic(),
            ),
        )

    @staticmethod
    def _failure(command: RetrieveCommand, code: ProjectionFailureCode) -> ProjectionRetrievalResult:
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.FAILED,
            command_identity=command.command_identity,
            command_digest=command.command_digest,
            failure=ProjectionFailure(
                code=code,
                retryable=code is ProjectionFailureCode.PHYSICAL_FAILURE,
                safe_detail=ProjectionDiagnostic(),
            ),
        )


__all__ = ["PhysicalReadPlanExecutor", "StructuredRetrievalProvider"]
