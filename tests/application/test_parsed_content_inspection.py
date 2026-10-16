from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.content_authorization import (
    AuthorizationDecisionKind,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
)
from spine.application.parsed_content_inspection import (
    ParsedContentInspectionQuery,
    ParsedContentInspectionRequester,
    ParsedContentInspectionStatus,
    PersistedParsedContentInspectionService,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
from spine.domain.common import ContextOrigin
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.sources import RevisionKind
from spine.infrastructure.persistence.parsed_artifacts import InMemoryParsedArtifactRepository
from spine.ingestion.parsers import parse_document
from tests.domain.artifacts.test_parsed_documents import revision


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
SUBJECT = UUID(int=9)


def context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=UUID(int=1), environment_id=UUID(int=2)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_parsed_content"),
        trace_id=UUID(int=10),
        acting_subject_id=SUBJECT,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=UUID(int=11), signature=b"trusted"),
    )


class Authorizer:
    def __init__(self) -> None:
        self.calls = 0

    async def decide_read_content(self, **kwargs):
        self.calls += 1
        return ContentAuthorizationDecision(
            allowed=True,
            decision_kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
            workspace_id=kwargs["context"].scope.workspace_id,
            environment_id=kwargs["context"].scope.environment_id,
            source_object_ids=(kwargs["source_object_id"],),
            policy_evidence=(
                AuthorizationEvidence(
                    source_object_id=kwargs["source_object_id"],
                    source_revision_id=kwargs["source_revision_id"],
                    policy_version_id=UUID(int=12),
                    policy_generation=1,
                ),
            ),
            membership_generation=1,
            authority_generation=1,
            authority_snapshot_reference=UUID(int=13),
            purpose="knowledge_control",
            operation="inspect_parsed_content",
            reason_code=AuthorizationReasonCode.ALLOWED,
            decision_reference=UUID(int=14),
            trace_id=kwargs["context"].trace_id,
            evaluated_at=NOW,
        )


class DenyAuthorizer:
    async def decide_read_content(self, **kwargs):
        return ContentAuthorizationDecision(
            allowed=False,
            decision_kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
            workspace_id=kwargs["context"].scope.workspace_id,
            environment_id=kwargs["context"].scope.environment_id,
            source_object_ids=(kwargs["source_object_id"],),
            membership_generation=1,
            authority_generation=1,
            authority_snapshot_reference=UUID(int=13),
            purpose="knowledge_control",
            operation="inspect_parsed_content",
            reason_code=AuthorizationReasonCode.AUTHORIZATION_DENIED,
            decision_reference=UUID(int=14),
            trace_id=kwargs["context"].trace_id,
            evaluated_at=NOW,
        )


class Uow:
    def __init__(self, document):
        source = SimpleNamespace(
            source_object_id=document.source_object_id,
            workspace_id=document.workspace_id,
            environment_id=document.environment_id,
        )
        self.sources = SimpleNamespace(
            resolve_source=lambda source_object_id: _return(source, source_object_id),
            resolve_revision=lambda revision_id: _return(
                revision_for(document), revision_id
            ),
        )
        self.canonical = SimpleNamespace(
            resolve_current=lambda source_object_id: _return(
                SimpleNamespace(
                    current_revision_id=document.source_revision_id,
                    current_revision_kind=RevisionKind.CONTENT,
                ),
                source_object_id,
            )
        )
        self.ingestion = SimpleNamespace(
            resolve_processing_receipt_by_id=lambda receipt_id: _return(
                receipt_for(document), receipt_id
            )
        )
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.committed = False


async def _return(value, expected):
    del expected
    return value


def revision_for(document):
    return revision(b"one\ntwo\n")


def receipt_for(document):
    return ProcessingReceipt(
        processing_receipt_id=UUID(int=20),
        workspace_id=document.workspace_id,
        environment_id=document.environment_id,
        ingestion_run_source_id=UUID(int=21),
        binding_id=UUID(int=22),
        source_object_id=document.source_object_id,
        source_revision_id=document.source_revision_id,
        processor_kind="document-ingestion",
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        parser_config_reference=document.parser_config_reference,
        locator_contract_version=document.locator_contract_version,
        original_sha256=document.original_sha256,
        output_kind="parsed-document",
        output_reference=document.artifact_id,
        output_digest=document.artifact_digest,
        coverage=ProcessingCoverage.COMPLETE,
        command_identity="process-1",
        command_digest="e" * 64,
        idempotency_receipt_id=UUID(int=23),
        completed_at=document.created_at,
    )


@pytest.mark.asyncio
async def test_authorized_current_receipt_returns_bounded_exact_segments() -> None:
    document = parse_document(revision(b"one\ntwo\n"), b"one\ntwo\n")
    repository = InMemoryParsedArtifactRepository()
    await repository.persist(ParsedArtifactCommand.create(document, command_identity="parse-1"))
    uow = Uow(document)
    authorizer = Authorizer()
    service = PersistedParsedContentInspectionService(
        uow_factory=lambda trusted: uow,
        parsed_artifacts=repository,
        authorizer=authorizer,
    )

    result = await service.inspect(
        context=context(),
        requester=ParsedContentInspectionRequester(subject_id=SUBJECT),
        query=ParsedContentInspectionQuery(
            source_object_id=document.source_object_id,
            source_revision_id=document.source_revision_id,
            processing_receipt_id=UUID(int=20),
            page_size=1,
        ),
    )

    assert result.status is ParsedContentInspectionStatus.READY
    assert result.metadata is not None
    assert [segment.text for segment in result.segments] == ["one"]
    assert result.next_after_segment_id == document.segments[0].segment_id
    assert uow.committed is True
    assert authorizer.calls == 2


@pytest.mark.asyncio
async def test_administrator_without_current_read_content_receives_no_segment() -> None:
    document = parse_document(revision(b"secret\n"), b"secret\n")
    repository = InMemoryParsedArtifactRepository()
    await repository.persist(ParsedArtifactCommand.create(document, command_identity="parse-2"))
    service = PersistedParsedContentInspectionService(
        uow_factory=lambda trusted: Uow(document),
        parsed_artifacts=repository,
        authorizer=DenyAuthorizer(),
    )

    result = await service.inspect(
        context=context(),
        requester=ParsedContentInspectionRequester(subject_id=SUBJECT),
        query=ParsedContentInspectionQuery(
            source_object_id=document.source_object_id,
            source_revision_id=document.source_revision_id,
            processing_receipt_id=UUID(int=20),
        ),
    )

    assert result.status is ParsedContentInspectionStatus.CONTENT_UNAVAILABLE
    assert result.segments == ()
    assert result.metadata is None
