"""Use cases coordinating domain objects through abstract ports.

The public names from the authorized broker are loaded lazily.  Memory
contracts import individual application ports, so eagerly importing the
orchestrator here would create a package-initialization cycle.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .answer_question_context import (
        AnswerQuestionAuthorizedContext,
        AnswerQuestionContextBroker,
        AnswerQuestionContextRequest,
        AnswerQuestionContextResolution,
        AnswerQuestionContextService,
        AnswerQuestionDisclosureCheckpoint,
        AnswerQuestionDisclosureGate,
        AnswerQuestionDisclosureRevalidator,
        ContextRevalidationFailure,
        map_context_outcome_to_retrieval_outcome,
    )
    from .authorized_context_broker import (
        AuthorizedContextBrokerApplicationService,
        AuthorizedContextRequest,
        ContextBrokerReadModel,
        ContextBrokerReadPort,
        ContextBundleRevalidator,
        ContextBundleStore,
        InMemoryContextBundleStore,
        SavedContextBundleAccess,
        SavedContextBundlePort,
        context_bundle_id_for_request,
        request_command_digest,
    )
    from .production_qa_capability import (
        ProductionAnswerQuestionCapability,
    )
    from .qualification_provisioning import (
        InMemoryQualificationProvisioningStore,
        QualificationInvocationRejected,
        QualificationInvocationSource,
        QualificationOperationConflict,
        QualificationOperationRequest,
        QualificationOperationUnavailable,
        QualificationOperatorDenied,
        QualificationProfileRejected,
        QualificationProvisioningService,
    )
    from .google_drive_lifecycle import (
        DriveLifecycleDecisionState,
        DriveLifecycleResult,
        DriveLifecycleSignal,
        DriveReconciliationEvidence,
        DriveReconciliationState,
        DriveRemovalDecision,
        DriveRemovalPolicy,
        DriveReappearanceEvidence,
        GoogleDriveLifecycleCoordinator,
        InMemoryDriveLifecycleDecisionRepository,
    )
    from .google_drive_reconciliation import (
        DriveFullReconciliationCommand,
        DriveFullReconciliationCoordinator,
        DriveFullReconciliationResult,
    )
    from .google_sheets_lifecycle import (
        GoogleSheetsAbsenceCandidate,
        GoogleSheetsDocumentTombstoneComposer,
        GoogleSheetsLifecycleCoordinator,
        GoogleSheetsLifecycleResult,
        GoogleSheetsRemovalDecision,
        GoogleSheetsRemovalPolicy,
        GoogleSheetsRemovalState,
        InMemoryGoogleSheetsLifecycleRepository,
    )

__all__ = [
    "AnswerQuestionAuthorizedContext",
    "AnswerQuestionContextBroker",
    "AnswerQuestionContextRequest",
    "AnswerQuestionContextResolution",
    "AnswerQuestionContextService",
    "AnswerQuestionDisclosureCheckpoint",
    "AnswerQuestionDisclosureGate",
    "AnswerQuestionDisclosureRevalidator",
    "ContextRevalidationFailure",
    "map_context_outcome_to_retrieval_outcome",
    "AuthorizedContextBrokerApplicationService",
    "AuthorizedContextRequest",
    "ContextBrokerReadModel",
    "ContextBrokerReadPort",
    "ContextBundleRevalidator",
    "ContextBundleStore",
    "InMemoryContextBundleStore",
    "SavedContextBundleAccess",
    "SavedContextBundlePort",
    "context_bundle_id_for_request",
    "request_command_digest",
    "ProductionAnswerQuestionCapability",
    "InMemoryQualificationProvisioningStore",
    "QualificationInvocationRejected",
    "QualificationInvocationSource",
    "QualificationOperationConflict",
    "QualificationOperationRequest",
    "QualificationOperationUnavailable",
    "QualificationOperatorDenied",
    "QualificationProfileRejected",
    "QualificationProvisioningService",
    "CandidateOutputStreamingError",
    "CursorScopeError",
    "InMemoryQAEventLog",
    "InMemoryQAEventStream",
    "QAEventLog",
    "QAEventLogError",
    "QAEventLogPort",
    "QAEventReplay",
    "QAEventStream",
    "AuthorizedQAStreamService",
    "QAEventLogStore",
    "QAResultEventPublisher",
    "ResumeCursorError",
    "ConnectorIngestionCommand",
    "ConnectorIngestionOrchestrator",
    "ConnectorIngestionResult",
    "ConnectorIngestionStatus",
    "GoogleAuthorizedObservationReader",
    "build_connector_canonical_acceptance_command",
    "DriveInitialSnapshotCommand",
    "DriveInitialSnapshotCoordinator",
    "DriveInitialSnapshotResult",
    "DriveChangeCommand",
    "DriveChangeCoordinator",
    "GoogleSheetsReconciliationCoordinator",
    "GoogleSheetsReconciliationCommand",
    "GoogleSheetsReconciliationScope",
    "GoogleSheetsAdmittedPageReader",
    "GoogleSheetsDriveWakeupAdapter",
    "DriveChangeResult",
    "DriveReconciliationCommand",
    "DriveLifecycleDecisionState",
    "DriveLifecycleResult",
    "DriveLifecycleSignal",
    "DriveReconciliationEvidence",
    "DriveReconciliationState",
    "DriveRemovalDecision",
    "DriveRemovalPolicy",
    "DriveReappearanceEvidence",
    "GoogleDriveLifecycleCoordinator",
    "InMemoryDriveLifecycleDecisionRepository",
    "DriveFullReconciliationCommand",
    "DriveFullReconciliationCoordinator",
    "DriveFullReconciliationResult",
    "GoogleSheetsAbsenceCandidate",
    "GoogleSheetsDocumentTombstoneComposer",
    "GoogleSheetsLifecycleCoordinator",
    "GoogleSheetsLifecycleResult",
    "GoogleSheetsRemovalDecision",
    "GoogleSheetsRemovalPolicy",
    "GoogleSheetsRemovalState",
    "InMemoryGoogleSheetsLifecycleRepository",
]


def __getattr__(name: str) -> object:
    if name in {
        "AnswerQuestionAuthorizedContext",
        "AnswerQuestionContextBroker",
        "AnswerQuestionContextRequest",
        "AnswerQuestionContextResolution",
        "AnswerQuestionContextService",
        "AnswerQuestionDisclosureCheckpoint",
        "AnswerQuestionDisclosureGate",
        "AnswerQuestionDisclosureRevalidator",
        "ContextRevalidationFailure",
        "map_context_outcome_to_retrieval_outcome",
    }:
        module = import_module("spine.application.answer_question_context")
        return getattr(module, name)
    if name in {
        "InMemoryQualificationProvisioningStore",
        "QualificationInvocationRejected",
        "QualificationInvocationSource",
        "QualificationOperationConflict",
        "QualificationOperationRequest",
        "QualificationOperationUnavailable",
        "QualificationOperatorDenied",
        "QualificationProfileRejected",
        "QualificationProvisioningService",
    }:
        module = import_module("spine.application.qualification_provisioning")
        return getattr(module, name)
    if name in __all__:
        module_name = (
            "spine.application.qa_events"
            if name
            in {
                "CandidateOutputStreamingError",
                "CursorScopeError",
                "InMemoryQAEventLog",
                "InMemoryQAEventStream",
                "QAEventLog",
                "QAEventLogError",
                "QAEventLogPort",
                "QAEventReplay",
                "QAEventStream",
                "ResumeCursorError",
            }
            else "spine.application.authorized_context_broker"
        )
        if name == "ProductionAnswerQuestionCapability":
            module_name = "spine.application.production_qa_capability"
        if name in {
            "ConnectorIngestionCommand",
            "ConnectorIngestionOrchestrator",
            "ConnectorIngestionResult",
            "ConnectorIngestionStatus",
            "GoogleAuthorizedObservationReader",
            "build_connector_canonical_acceptance_command",
        }:
            module_name = "spine.application.connector_ingestion"
        if name in {
            "DriveInitialSnapshotCommand",
            "DriveInitialSnapshotCoordinator",
            "DriveInitialSnapshotResult",
        }:
            module_name = "spine.application.google_drive_snapshot"
        if name in {
            "DriveChangeCommand",
            "DriveChangeCoordinator",
            "DriveChangeResult",
            "DriveReconciliationCommand",
        }:
            module_name = "spine.application.google_drive_changes"
        if name in {
            "GoogleSheetsReconciliationCoordinator",
            "GoogleSheetsReconciliationCommand",
            "GoogleSheetsReconciliationScope",
            "GoogleSheetsAdmittedPageReader",
            "GoogleSheetsDriveWakeupAdapter",
        }:
            module_name = "spine.application.google_sheets_reconciliation"
        if name in {
            "DriveLifecycleDecisionState",
            "DriveLifecycleResult",
            "DriveLifecycleSignal",
            "DriveReconciliationEvidence",
            "DriveReconciliationState",
            "DriveRemovalDecision",
            "DriveRemovalPolicy",
            "DriveReappearanceEvidence",
            "GoogleDriveLifecycleCoordinator",
            "InMemoryDriveLifecycleDecisionRepository",
        }:
            module_name = "spine.application.google_drive_lifecycle"
        if name in {
            "DriveFullReconciliationCommand",
            "DriveFullReconciliationCoordinator",
            "DriveFullReconciliationResult",
        }:
            module_name = "spine.application.google_drive_reconciliation"
        if name in {
            "GoogleSheetsAbsenceCandidate",
            "GoogleSheetsDocumentTombstoneComposer",
            "GoogleSheetsLifecycleCoordinator",
            "GoogleSheetsLifecycleResult",
            "GoogleSheetsRemovalDecision",
            "GoogleSheetsRemovalPolicy",
            "GoogleSheetsRemovalState",
            "InMemoryGoogleSheetsLifecycleRepository",
        }:
            module_name = "spine.application.google_sheets_lifecycle"
        if name in {
            "AuthorizedQAStreamService",
            "QAEventLogStore",
            "QAResultEventPublisher",
        }:
            module_name = "spine.application.qa_streaming"
        module = import_module(module_name)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
