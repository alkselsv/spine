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
        if name in {
            "AuthorizedQAStreamService",
            "QAEventLogStore",
            "QAResultEventPublisher",
        }:
            module_name = "spine.application.qa_streaming"
        module = import_module(module_name)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
