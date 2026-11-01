from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.document_ingestion import ProjectionRequestPayload
from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.application.projection_request_execution import (
    CanonicalProjectionRequestContext,
    PersistenceCanonicalProjectionRequestResolver,
    ProjectionRequestEnvelope,
)
from spine.application.projection_result_persistence import (
    CanonicalProjectionEvidenceRequest,
    ParsedArtifactCanonicalProjectionLocatorResolver,
)
from spine.application.projection_result_mapping import ProjectionMappingError
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingCoverage,
    ProcessingReceipt,
)
from spine.domain.projections import ProjectionCandidateState
from spine.domain.sources import (
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentSourceHead,
    ExactOpaqueApprovalReference,
    ProcessingAdmissionMode,
    RevisionKind,
)
from spine.ingestion.parsers import parse_document
from tests.domain.artifacts.test_parsed_documents import revision

from tests.application.test_projection_result_mapping import (
    BOUNDARY,
    CANDIDATE,
    CONFIG,
    ENVIRONMENT,
    NOW,
    PARTITION,
    REVISION,
    RUN,
    SOURCE,
    WORKSPACE,
    _command,
    _context,
)
from tests.application.persistence.test_processing_admission_read import (
    processing_admission as processor_admission,
)
ITEM = UUID(int=11)
INGESTION_RUN = UUID(int=12)
BINDING = UUID(int=13)
PROCESSING_RECEIPT = UUID(int=14)
ARTIFACT = UUID(int=15)


class _Uow:
    def __init__(
        self,
        *,
        runs=None,
        current_revision_id=REVISION,
        current_revision_kind=RevisionKind.CONTENT,
        additional_boundary_member=None,
        locator_contract_version="locator:v1",
        opaque_current=False,
        opaque_additional=False,
    ) -> None:
        command = _command()
        context = _context(command)
        source = revision(b"hello")
        additional_source = None
        if additional_boundary_member is not None:
            additional_source = source.model_copy(
                update={
                    "source_object_id": additional_boundary_member.source_object_id,
                    "revision_id": additional_boundary_member.source_revision_id,
                    "revision_digest": "f" * 64,
                }
            )
        self.ingestion = _Ingestion(
            source,
            additional_source,
            locator_contract_version=locator_contract_version,
        )
        self.sources = SimpleNamespace(resolve_revision=self._resolve_revision)
        self.canonical = SimpleNamespace(
            resolve_current=self._resolve_current,
            resolve_acceptance_read=self._resolve_acceptance_read,
        )
        self.boundaries = SimpleNamespace(
            resolve=lambda boundary_id: self._resolve_boundary(
                self._context, boundary_id
            ),
            resolve_members=lambda boundary_id: self._resolve_members(
                self._context, boundary_id
            ),
        )
        self.projections = SimpleNamespace(
            list_runs_for_source_revision=self._list_runs_for_source_revision,
            resolve_config_version=lambda config_id: self._resolve_config(
                context, config_id
            ),
            list_partitions=lambda run_id: self._resolve_partitions(context, run_id),
        )
        self._source = source
        self._additional_source = additional_source
        self._additional_boundary_member = additional_boundary_member
        self._current_revision_id = current_revision_id
        self._current_revision_kind = current_revision_kind
        self._opaque_current = opaque_current
        self._opaque_additional = opaque_additional
        self._context = context
        if additional_boundary_member is not None:
            self._context = context.model_copy(
                update={
                    "boundary_members": (
                        *context.boundary_members,
                        additional_boundary_member,
                    )
                }
            )
        self._runs = runs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def _resolve_revision(self, revision_id):
        if revision_id == REVISION:
            return self._source
        if (
            self._additional_source is not None
            and revision_id == self._additional_source.revision_id
        ):
            return self._additional_source
        return None

    async def _list_runs_for_source_revision(self, source_object_id, source_revision_id):
        return self._runs if self._runs is not None else (self._context.run,)

    async def _resolve_current(self, source_object_id):
        if (
            self._additional_source is not None
            and source_object_id == self._additional_source.source_object_id
        ):
            member = self._additional_boundary_member
            return CurrentSourceHead(
                source_object_id=member.source_object_id,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                current_revision_id=member.source_revision_id,
                current_revision_kind=RevisionKind.CONTENT,
                acceptance_id=member.acceptance_id,
                accepted_generation=member.accepted_generation,
                cas_version=1,
                changed_at=NOW,
            )
        if source_object_id != SOURCE:
            return None
        return CurrentSourceHead(
            source_object_id=SOURCE,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            current_revision_id=self._current_revision_id,
            current_revision_kind=self._current_revision_kind,
            acceptance_id=UUID(int=16),
            accepted_generation=1,
            cas_version=1,
            changed_at=NOW,
        )

    async def _resolve_acceptance_read(self, acceptance_id):
        source_id = SOURCE
        revision_id = self._current_revision_id
        generation = 1
        if (
            self._additional_source is not None
            and acceptance_id == self._additional_boundary_member.acceptance_id
        ):
            source_id = self._additional_source.source_object_id
            revision_id = self._additional_source.revision_id
            generation = self._additional_boundary_member.accepted_generation
        opaque = self._opaque_current or (
            self._opaque_additional
            and self._additional_source is not None
            and source_id == self._additional_source.source_object_id
        )
        current = await self._resolve_current(source_id)
        if current is None or current.acceptance_id != acceptance_id:
            return None
        admission = None
        acceptance = CanonicalAcceptance(
            canonical_acceptance_schema_version=2 if opaque else 1,
            canonical_acceptance_id=acceptance_id,
            source_object_id=source_id,
            source_revision_id=revision_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            accepted_generation=generation,
            ordering=CanonicalOrdering.EXPECTED_CURRENT,
            admission_decision_id=UUID(int=170) if opaque else None,
            access_policy_version_id=UUID(int=171) if opaque else None,
            processing_admission_id=UUID(int=172) if opaque else None,
            provenance_id=UUID(int=130 + generation),
            idempotency_receipt_id=UUID(int=140 + generation),
            audit_event_id=UUID(int=150 + generation),
            outbox_event_id=UUID(int=160 + generation),
            accepted_at=NOW,
        )
        if opaque:
            admission = processor_admission().model_copy(
                update={
                    "processing_admission_id": UUID(int=172),
                    "workspace_id": WORKSPACE,
                    "environment_id": ENVIRONMENT,
                    "source_object_id": source_id,
                    "source_revision_id": revision_id,
                    "mode": ProcessingAdmissionMode.OPAQUE_ACCEPTANCE,
                    "processor": None,
                    "opaque_approval": ExactOpaqueApprovalReference(
                        approval_id=UUID(int=173),
                        approval_version="opaque:v1",
                        approval_digest="9" * 64,
                        approved_purpose="archive",
                        security_controls_reference=UUID(int=174),
                        retention_applicability_reference=(
                            processor_admission().retention_applicability_reference
                        ),
                    ),
                }
            )
        return CanonicalAcceptanceReadResult(acceptance, admission, current)

    @staticmethod
    async def _resolve_boundary(context, boundary_id):
        return context.canonical_boundary if boundary_id == BOUNDARY else None

    @staticmethod
    async def _resolve_members(context, boundary_id):
        return context.boundary_members if boundary_id == BOUNDARY else ()

    @staticmethod
    async def _resolve_config(context, config_id):
        return context.projection_config if config_id == CONFIG else None

    @staticmethod
    async def _resolve_partitions(context, run_id):
        return (context.partition,) if run_id == RUN else ()


class _Ingestion:
    def __init__(
        self,
        source,
        additional_source=None,
        *,
        locator_contract_version="locator:v1",
    ) -> None:
        self._source = source
        self._additional_source = additional_source
        self._locator_contract_version = locator_contract_version

    async def resolve_item(self, item_id):
        return IngestionRunSource(
            ingestion_run_source_id=ITEM,
            ingestion_run_id=INGESTION_RUN,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            input_ordinal=1,
            input_identity="input-1",
            input_digest="a" * 64,
            command_identity="ingest-1",
            command_digest="b" * 64,
            created_at=NOW,
        ) if item_id == ITEM else None

    async def resolve_item_head(self, item_id):
        return IngestionTransition(
            ingestion_transition_id=UUID(int=17),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            target_kind=IngestionTargetKind.ITEM,
            ingestion_run_id=INGESTION_RUN,
            ingestion_run_source_id=ITEM,
            state=IngestionItemState.PROJECTION_REQUESTED,
            attempt=1,
            actor_or_service="worker",
            trace_id=UUID(int=18),
            occurred_at=NOW,
        ) if item_id == ITEM else None

    async def resolve_binding(self, item_id):
        return IngestionRunSourceBinding(
            binding_id=BINDING,
            ingestion_run_source_id=ITEM,
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            binding_command_identity="bind-1",
            binding_command_digest="c" * 64,
            bound_at=NOW,
        ) if item_id == ITEM else None

    async def resolve_processing_receipt(self, item_id):
        if self._additional_source is not None and item_id == UUID(int=111):
            return ProcessingReceipt(
                processing_receipt_id=UUID(int=114),
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                ingestion_run_source_id=UUID(int=111),
                binding_id=UUID(int=113),
                source_object_id=self._additional_source.source_object_id,
                source_revision_id=self._additional_source.revision_id,
                processor_kind="document-ingestion",
                parser_name="text",
                parser_version="parser:v1",
                parser_config_reference="parser-default:v1",
                locator_contract_version="locator:v1",
                original_sha256=self._additional_source.original_sha256,
                output_kind="parsed-document",
                output_reference=UUID(int=115),
                output_digest="1" * 64,
                coverage=ProcessingCoverage.COMPLETE,
                command_identity="process-2",
                command_digest="2" * 64,
                idempotency_receipt_id=UUID(int=119),
                completed_at=NOW,
            )
        original_sha256 = self._source.original_sha256
        return ProcessingReceipt(
            processing_receipt_id=PROCESSING_RECEIPT,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            ingestion_run_source_id=ITEM,
            binding_id=BINDING,
            source_object_id=SOURCE,
            source_revision_id=REVISION,
            processor_kind="document-ingestion",
            parser_name="text",
            parser_version="parser:v1",
            parser_config_reference="parser-default:v1",
            locator_contract_version=self._locator_contract_version,
            original_sha256=original_sha256,
            output_kind="parsed-document",
            output_reference=ARTIFACT,
            output_digest="d" * 64,
            coverage=ProcessingCoverage.COMPLETE,
            command_identity="process-1",
            command_digest="e" * 64,
            idempotency_receipt_id=UUID(int=19),
            completed_at=NOW,
        ) if item_id == ITEM else None

    async def list_bindings_for_source_revision(
        self, source_object_id, source_revision_id
    ):
        if (source_object_id, source_revision_id) == (SOURCE, REVISION):
            binding = await self.resolve_binding(ITEM)
            return (binding,)
        if (
            self._additional_source is not None
            and (source_object_id, source_revision_id)
            == (
                self._additional_source.source_object_id,
                self._additional_source.revision_id,
            )
        ):
            return (
                IngestionRunSourceBinding(
                    binding_id=UUID(int=113),
                    ingestion_run_source_id=UUID(int=111),
                    source_object_id=source_object_id,
                    source_revision_id=source_revision_id,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    binding_command_identity="bind-2",
                    binding_command_digest="3" * 64,
                    bound_at=NOW,
                ),
            )
        return ()


class _LocatorUow:
    def __init__(self, receipt: ProcessingReceipt) -> None:
        self.ingestion = SimpleNamespace(
            resolve_processing_receipt_by_id=lambda receipt_id: self._resolve_receipt(
                receipt, receipt_id
            )
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    @staticmethod
    async def _resolve_receipt(receipt, receipt_id):
        return receipt if receipt.processing_receipt_id == receipt_id else None


class _ParsedArtifacts:
    def __init__(self, document) -> None:
        self._document = document

    async def get(self, *, workspace_id, environment_id, artifact_id):
        document = self._document
        if (
            document.workspace_id == workspace_id
            and document.environment_id == environment_id
            and document.artifact_id == artifact_id
        ):
            return document
        return None


def _request(
    *, locator_contract_version: str = "locator:v1"
) -> ProjectionRequestEnvelope:
    return ProjectionRequestEnvelope(
        event_id=UUID(int=20),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        payload=ProjectionRequestPayload(
            ingestion_run_id=INGESTION_RUN,
            ingestion_run_source_id=ITEM,
            source_revision_id=REVISION,
            processing_receipt_id=PROCESSING_RECEIPT,
            processor_kind="document-ingestion",
            parser_version="parser:v1",
            parser_config_reference="parser-default:v1",
            locator_contract_version=locator_contract_version,
        ),
    )


def _resolver(uow: _Uow) -> PersistenceCanonicalProjectionRequestResolver:
    return PersistenceCanonicalProjectionRequestResolver(
        uow_factory=lambda context: uow,
        context_factory=lambda request: object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_production_resolver_returns_detached_canonical_command_context() -> None:
    resolved = await _resolver(_Uow()).resolve(_request())

    assert isinstance(resolved, CanonicalProjectionRequestContext)
    assert resolved.projection_run_id == RUN
    assert resolved.projection_candidate.candidate_id == CANDIDATE
    assert resolved.projection_candidate.partition_id == PARTITION
    assert len(resolved.source_revisions) == 1
    assert resolved.source_revisions[0].source_revision_id == REVISION
    assert resolved.command_identity == "partition-project-1"
    assert resolved.physical_namespace.state.value == "shadow"


@pytest.mark.asyncio
async def test_opaque_current_revision_is_rejected_before_physical_projection() -> None:
    with pytest.raises(ProjectionMappingError, match="canonical projection request"):
        await _resolver(_Uow(opaque_current=True)).resolve(_request())


@pytest.mark.asyncio
async def test_opaque_boundary_member_is_excluded_from_projection_command() -> None:
    context = _context(_command())
    additional = context.boundary_members[0].model_copy(
        update={
            "source_object_id": UUID(int=121),
            "source_revision_id": UUID(int=122),
            "acceptance_id": UUID(int=123),
            "accepted_generation": 2,
        }
    )

    resolved = await _resolver(
        _Uow(
            additional_boundary_member=additional,
            opaque_additional=True,
        )
    ).resolve(_request())

    assert tuple(
        item.source_revision_id for item in resolved.source_revisions
    ) == (REVISION,)


@pytest.mark.asyncio
async def test_production_resolver_accepts_format_specific_locator_contract() -> None:
    locator_contract_version = "pdf-page-extracted-codepoint:v1"
    context = _context(_command())
    additional = context.boundary_members[0].model_copy(
        update={
            "source_object_id": UUID(int=121),
            "source_revision_id": UUID(int=122),
            "acceptance_id": UUID(int=123),
            "accepted_generation": 2,
        }
    )

    resolved = await _resolver(
        _Uow(
            additional_boundary_member=additional,
            locator_contract_version=locator_contract_version,
        )
    ).resolve(_request(locator_contract_version=locator_contract_version))

    assert {value.locator_contract_version for value in resolved.source_revisions} == {
        locator_contract_version,
        "locator:v1",
    }


@pytest.mark.asyncio
async def test_production_resolver_resumes_sealed_candidate_after_partial_publication() -> None:
    context = _context(_command())
    sealed_run = context.run.model_copy(
        update={"candidate_state": ProjectionCandidateState.SEALED}
    )

    resolved = await _resolver(_Uow(runs=(sealed_run,))).resolve(_request())

    assert resolved.projection_run_id == RUN
    assert resolved.projection_candidate.candidate_id == CANDIDATE


@pytest.mark.asyncio
async def test_production_resolver_projects_every_current_source_in_workspace_boundary() -> None:
    context = _context(_command())
    additional = context.boundary_members[0].model_copy(
        update={
            "source_object_id": UUID(int=101),
            "source_revision_id": UUID(int=102),
            "acceptance_id": UUID(int=103),
            "accepted_generation": 2,
        }
    )

    resolved = await _resolver(
        _Uow(additional_boundary_member=additional)
    ).resolve(_request())

    assert {item.source_object_id for item in resolved.source_revisions} == {
        SOURCE,
        additional.source_object_id,
    }
    assert {item.source_revision_id for item in resolved.source_revisions} == {
        REVISION,
        additional.source_revision_id,
    }


@pytest.mark.asyncio
async def test_production_resolver_fences_stale_current_revision_before_physical_io() -> None:
    with pytest.raises(ProjectionMappingError):
        await _resolver(_Uow(current_revision_id=UUID(int=21))).resolve(_request())


@pytest.mark.asyncio
async def test_production_resolver_fences_tombstoned_current_revision_before_physical_io() -> None:
    with pytest.raises(ProjectionMappingError):
        await _resolver(_Uow(current_revision_kind=RevisionKind.TOMBSTONE)).resolve(
            _request()
        )


@pytest.mark.asyncio
async def test_parsed_artifact_locator_resolver_returns_digest_bound_evidence() -> None:
    source = revision(b"hello")
    document = parse_document(source, b"hello")
    command = _command().model_copy(
        update={
            "source_revisions": (
                _command().source_revisions[0].model_copy(
                    update={
                        "revision_digest": source.revision_digest,
                        "locator_contract_version": document.locator_contract_version,
                    }
                ),
            )
        }
    )
    receipt = ProcessingReceipt(
        processing_receipt_id=command.source_revisions[0].processing_receipt_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        ingestion_run_source_id=ITEM,
        binding_id=BINDING,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        processor_kind="document-ingestion",
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        parser_config_reference=document.parser_config_reference,
        locator_contract_version=document.locator_contract_version,
        original_sha256=source.original_sha256,
        output_kind="parsed-document",
        output_reference=document.artifact_id,
        output_digest=document.artifact_digest,
        coverage=ProcessingCoverage.COMPLETE,
        command_identity="process-1",
        command_digest="e" * 64,
        idempotency_receipt_id=UUID(int=19),
        completed_at=NOW,
    )
    mapping = _context(command)
    resolver = ParsedArtifactCanonicalProjectionLocatorResolver(
        uow_factory=lambda context: _LocatorUow(receipt),
        context_factory=lambda value: object(),  # type: ignore[arg-type]
        parsed_artifacts=_ParsedArtifacts(document),  # type: ignore[arg-type]
    )

    locators = await resolver.resolve(
        CanonicalProjectionEvidenceRequest(
            command=command,
            run=mapping.run,
            partition=mapping.partition,
            projection_config=mapping.projection_config,
            ontology=object(),  # type: ignore[arg-type]
            canonical_boundary=mapping.canonical_boundary,
            boundary_members=mapping.boundary_members,
        )
    )

    assert len(locators) == len(document.segments)
    assert locators[0].source_revision_id == REVISION
    assert locators[0].locator.kind == "character_range"
    assert len(locators[0].locator_digest) == 64


@pytest.mark.asyncio
async def test_production_resolver_rejects_ambiguous_projection_runs() -> None:
    context = _context(_command())
    second = context.run.model_copy(
        update={
            "projection_run_id": UUID(int=22),
            "projection_candidate_id": UUID(int=23),
        }
    )
    with pytest.raises(ProjectionMappingError):
        await _resolver(_Uow(runs=(context.run, second))).resolve(_request())
