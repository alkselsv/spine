from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.persistence.answer_question_runs import AnswerQuestionRunPage
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.qa_run_history import (
    PersistedQARunHistoryService,
    QARunHistoryFilters,
    QARunHistoryRequester,
)
from spine.domain.common import ContextOrigin


SUBJECT_ID = UUID(int=536)


def context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=UUID(int=1), environment_id=UUID(int=2)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_run_history"),
        trace_id=UUID(int=3),
        acting_subject_id=SUBJECT_ID,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID(int=4),
            signature=b"trusted",
        ),
    )


@pytest.mark.asyncio
async def test_empty_persisted_run_page_returns_valid_collection() -> None:
    class AnswerQuestionRuns:
        async def list_runs(self, **kwargs) -> AnswerQuestionRunPage:
            assert kwargs["limit"] == 25
            assert kwargs["after"] is None
            return AnswerQuestionRunPage(runs=(), has_more=False)

    class UnitOfWork:
        answer_question_runs = AnswerQuestionRuns()
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

        async def commit(self) -> None:
            self.committed = True

    uow = UnitOfWork()
    service = PersistedQARunHistoryService(
        uow_factory=lambda persistence_context: uow  # type: ignore[arg-type]
    )

    page = await service.list_runs(
        context=context(),
        requester=QARunHistoryRequester(subject_id=SUBJECT_ID),
        filters=QARunHistoryFilters(),
        limit=25,
        after=None,
    )

    assert page.items == ()
    assert page.has_more is False
    assert uow.committed is True


@pytest.mark.asyncio
async def test_attempt_contexts_are_materialized_in_attempt_order() -> None:
    first_reference = SimpleNamespace(
        bundle_id="first-id",
        bundle_version=1,
        bundle_digest="first-digest",
    )
    second_reference = SimpleNamespace(
        bundle_id="second-id",
        bundle_version=2,
        bundle_digest="second-digest",
    )
    resolved = [object(), object()]

    class ContextBundles:
        async def resolve(self, *, bundle_id, bundle_version, bundle_digest):
            index = 0 if bundle_id == "first-id" else 1
            assert bundle_version == index + 1
            assert bundle_digest == ("first-digest" if index == 0 else "second-digest")
            return resolved[index]

    service = PersistedQARunHistoryService(uow_factory=object())  # type: ignore[arg-type]
    uow = SimpleNamespace(context_bundles=ContextBundles())
    attempts = (
        SimpleNamespace(context_bundle_ref=first_reference),
        SimpleNamespace(context_bundle_ref=second_reference),
    )

    contexts = await service._contexts(uow, attempts)  # type: ignore[arg-type]

    assert contexts == tuple(resolved)
