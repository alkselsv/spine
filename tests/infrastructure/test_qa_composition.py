from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.disclosure_revalidation import RevalidatingQAMessageDisclosure
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.qa_sessions import QASessionRequester
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.qa_composition import PostgreSQLQASessionService, _citation_context


def test_session_read_composes_fresh_accepted_answer_revalidation() -> None:
    repository = object()
    service = PostgreSQLQASessionService._service(
        SimpleNamespace(
            qa_sessions=repository,
            answer_question_runs=object(),
            answer_disclosures=object(),
            disclosure_validations=object(),
        ),
        QASessionRequester(
            subject_id=UUID(int=1),
            authorization_generation=1,
        ),
    )

    eligibility = service.disclosure_eligibility
    assert eligibility.repository is repository
    assert isinstance(eligibility.accepted, RevalidatingQAMessageDisclosure)


def test_citation_context_accepts_only_the_authorized_environment_scope() -> None:
    workspace_id = UUID("00000000-0000-0000-0000-000000000517")
    environment_id = UUID("00000000-0000-0000-0000-000000005170")
    context = TrustedContextBoundary.for_testing(
        issuer_id=UUID("00000000-0000-0000-0000-000000000001"),
        secret=b"citation-context-test-secret-value",
    ).interactive(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        acting_subject_id=UUID("00000000-0000-0000-0000-000000000002"),
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_citations"),
        trace_id=UUID("00000000-0000-0000-0000-000000000003"),
    )

    assert (
        _citation_context(
            context,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
        is context
    )
    with pytest.raises(RuntimeError, match="citation excerpt scope is unavailable"):
        _citation_context(
            context,
            workspace_id=UUID("00000000-0000-0000-0000-000000000518"),
            environment_id=environment_id,
        )
