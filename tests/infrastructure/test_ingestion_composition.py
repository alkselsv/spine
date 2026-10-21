from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from spine.application.diagnostics import AuditEventRegistry
from spine.application.document_ingestion import DocumentUploadCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.common import ContextOrigin
from spine.domain.sources import IdentityMode, SourceObject
from spine.domain.sources.access_policy import AccessPermission, PrincipalKind
from spine.infrastructure.ingestion_composition import (
    _InitialUploadPolicyEnsurer,
    register_ingestion_outbox_events,
)


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
SUBJECT_ID = UUID("30000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("40000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("50000000-0000-4000-8000-000000000001")
TRACE_ID = UUID("60000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def test_initial_upload_policy_authorizes_governed_retrieval_service() -> None:
    outbox = OutboxEventRegistry()
    register_ingestion_outbox_events(outbox)
    context = TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        acting_subject_id=SUBJECT_ID,
        service_principal_id=SERVICE_ID,
        purpose=PersistencePurpose("document.upload.v1"),
        operation=PersistenceOperation("canonical_acceptance"),
        trace_id=TRACE_ID,
        origin=ContextOrigin.INTERACTIVE,
        provenance=TrustedContextProvenance(
            issuer_id=UUID("80000000-0000-4000-8000-000000000001"),
            signature=b"test-signature",
        ),
    )
    ensurer = _InitialUploadPolicyEnsurer(
        session_factory=object(),
        uow_factory=object(),
        request_context=context,
        service_principal_id=SERVICE_ID,
        outbox_events=outbox,
        audit_events=AuditEventRegistry.with_default_families(),
        authority_alias_issuer="qualification",
        authority_alias_subject="administrator",
    )

    command = ensurer._command(
        request=DocumentUploadCommand(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            command_identity="document-upload",
            idempotency_key="document-1",
            upload_identity="uploaded-document",
            purpose="document.upload.v1",
            declared_media_type="text/plain",
            declared_filename="document.txt",
        ),
        source=SourceObject(
            source_object_id=SOURCE_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_kind="document",
            identity_mode=IdentityMode.UPLOAD,
            upload_identity="uploaded-document",
            created_at=NOW,
        ),
        context=context,
        subject_id=SUBJECT_ID,
        generation=1,
        snapshot_reference=SNAPSHOT_ID,
        activated_at=NOW,
    )

    assert any(
        grant.principal_kind is PrincipalKind.SERVICE
        and grant.principal_id == SERVICE_ID
        and grant.permission is AccessPermission.PROCESS_CONTENT
        and grant.purpose == "answer_question"
        and grant.operation == "retrieve_context"
        for grant in command.policy.grants
    )
