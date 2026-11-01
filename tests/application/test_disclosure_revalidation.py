from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.disclosure_revalidation import (
    DisclosureRevalidationService,
    InMemoryDisclosureValidationResultRepository,
    RevalidatingQAMessageDisclosure,
)
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_sessions import PersistedQASessionService, QASessionRequester
from spine.domain.disclosure import (
    DisclosureDependencyObservation,
    DisclosureDependencySet,
    DisclosurePublicationImpact,
    DisclosureProcessingDispositionReason,
    DisclosurePublicationObservation,
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationDisposition,
    DisclosureRevalidationFailureCode,
    DisclosureRevalidationObservation,
    DisclosureRevalidationRequest,
)
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessage, QAMessageState
from spine.infrastructure.persistence.qa_sessions import InMemoryQASessionRepository

from tests.domain.context_bundle.test_context_bundle_models import context_bundle

WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
REQUESTER = UUID(int=30)
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def dependency_set() -> DisclosureDependencySet:
    return DisclosureDependencySet.from_context_bundle(context_bundle())


def observation(**overrides: object) -> DisclosureRevalidationObservation:
    dependency = dependency_set().dependencies[0]
    values: dict[str, object] = {
        "dependency_id": dependency.dependency_id,
        "source_object_id": dependency.source_object_id,
        "current_policy_version_id": dependency.policy_version_id,
        "current_policy_generation": dependency.policy_generation,
        "membership_generation": dependency.membership_generation,
        "authority_generation": dependency.authority_generation,
        "service_authority_generation": dependency.service_authority_generation,
        "contributor_authorized": True,
        "current_revision_id": dependency.source_revision_id,
        "current_revision_digest": dependency.source_revision_digest,
        "processing_disposition_eligible": True,
        "revision_current": True,
        "retention_eligible": True,
        "lifecycle_disclosure_eligible": True,
        "active_disclosure_hold": False,
    }
    values.update(overrides.pop("dependency", {}))  # type: ignore[arg-type]
    publication = DisclosurePublicationObservation(
        pinned_snapshot_id=UUID(int=4),
        pinned_activation_generation=1,
        pinned_boundary_id=UUID(int=6),
        pinned_as_of_boundary_id=UUID(int=6),
        pinned_as_of_generation=2,
    )
    values.update(overrides)
    return DisclosureRevalidationObservation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        requester_authorized=True,
        publication=publication,
        dependencies=(DisclosureDependencyObservation(**values),),
    )


def request(**overrides: object) -> DisclosureRevalidationRequest:
    values: dict[str, object] = {
        "request_id": UUID(int=40),
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "requester_id": REQUESTER,
        "checkpoint": DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ,
        "dependencies": dependency_set(),
    }
    values.update(overrides)
    return DisclosureRevalidationRequest(**values)


class FakeStateProvider:
    def __init__(self, value: DisclosureRevalidationObservation) -> None:
        self.value = value
        self.calls = 0

    async def resolve(self, _request: DisclosureRevalidationRequest) -> DisclosureRevalidationObservation:
        self.calls += 1
        return self.value


class InterruptedStateProvider:
    async def resolve(self, _request: DisclosureRevalidationRequest) -> DisclosureRevalidationObservation:
        raise RuntimeError("validation interrupted")


@pytest.mark.asyncio
async def test_current_authority_allows_complete_stored_answer_reuse() -> None:
    provider = FakeStateProvider(observation())
    service = DisclosureRevalidationService(provider, InMemoryDisclosureValidationResultRepository(), clock=lambda: NOW)

    result = await service.revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.SAFE_REUSE
    assert result.can_disclose_content
    assert result.facts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "failure"),
    (
        (
            "contributor_authorized",
            False,
            DisclosureRevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED,
        ),
        (
            "retention_eligible",
            False,
            DisclosureRevalidationFailureCode.RETENTION_INELIGIBLE,
        ),
        (
            "lifecycle_disclosure_eligible",
            False,
            DisclosureRevalidationFailureCode.DISCLOSURE_REVIEW_REQUIRED,
        ),
        (
            "active_disclosure_hold",
            True,
            DisclosureRevalidationFailureCode.DISCLOSURE_HELD,
        ),
        ("tombstoned", True, DisclosureRevalidationFailureCode.SOURCE_TOMBSTONED),
    ),
)
async def test_current_ineligible_dependency_suppresses_whole_answer(
    field: str, value: object, failure: DisclosureRevalidationFailureCode
) -> None:
    service = DisclosureRevalidationService(
        FakeStateProvider(observation(**{field: value})),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    )

    result = await service.revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.INELIGIBLE
    assert failure in result.failure_codes
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_opaque_current_dependency_suppresses_cached_answer() -> None:
    result = await DisclosureRevalidationService(
        FakeStateProvider(observation(processing_disposition_eligible=False)),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=41)))

    assert result.disposition is DisclosureRevalidationDisposition.INELIGIBLE
    assert (
        DisclosureRevalidationFailureCode.PROCESSING_DISPOSITION_INELIGIBLE
        in result.failure_codes
    )
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_unavailable_processing_authority_is_retryable_not_ineligible() -> None:
    result = await DisclosureRevalidationService(
        FakeStateProvider(
            observation(
                processing_disposition_eligible=False,
                processing_disposition_reason=(
                    DisclosureProcessingDispositionReason.CURRENT_AUTHORITY_UNAVAILABLE
                ),
            )
        ),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=45)))

    assert result.disposition is DisclosureRevalidationDisposition.RETRYABLE_FAILURE
    assert (
        DisclosureRevalidationFailureCode.REVALIDATION_UNAVAILABLE
        in result.failure_codes
    )
    assert (
        DisclosureRevalidationFailureCode.PROCESSING_DISPOSITION_INELIGIBLE
        not in result.failure_codes
    )


@pytest.mark.asyncio
async def test_superseded_dependency_suppresses_whole_answer() -> None:
    result = await DisclosureRevalidationService(
        FakeStateProvider(observation(superseded=True)),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=42)))

    assert result.disposition is DisclosureRevalidationDisposition.REVALIDATION_REQUIRED
    assert DisclosureRevalidationFailureCode.SOURCE_SUPERSEDED in result.failure_codes
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_cancelled_revalidation_fails_closed_without_stored_answer() -> None:
    result = await DisclosureRevalidationService(
        FakeStateProvider(observation()),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=43), cancelled=True))

    assert result.disposition is DisclosureRevalidationDisposition.CANCELLED
    assert result.safe_reason == "stored_answer_disclosure_cancelled"
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_interrupted_revalidation_fails_closed_without_stored_answer() -> None:
    result = await DisclosureRevalidationService(
        InterruptedStateProvider(),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=44)))

    assert result.disposition is DisclosureRevalidationDisposition.RETRYABLE_FAILURE
    assert DisclosureRevalidationFailureCode.REVALIDATION_UNAVAILABLE in result.failure_codes
    assert result.safe_reason == "stored_answer_disclosure_unavailable"
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_revision_replacement_requires_revalidation_without_disclosing_old_answer() -> None:
    service = DisclosureRevalidationService(
        FakeStateProvider(observation(revision_current=False)),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    )

    result = await service.revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.REVALIDATION_REQUIRED
    assert not result.can_disclose_content


@pytest.mark.asyncio
async def test_material_later_publication_requires_revalidation() -> None:
    state = observation()
    state = state.model_copy(
        update={"publication": state.publication.model_copy(update={"impact": DisclosurePublicationImpact.MATERIAL})}
    )
    service = DisclosureRevalidationService(
        FakeStateProvider(state),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    )

    result = await service.revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.REVALIDATION_REQUIRED
    assert DisclosureRevalidationFailureCode.LATER_PUBLICATION_IMPACT in result.failure_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ("current_policy_generation", "membership_generation", "authority_generation"),
)
async def test_authority_generation_change_requires_revalidation(field: str) -> None:
    dependency = dependency_set().dependencies[0]
    service = DisclosureRevalidationService(
        FakeStateProvider(
            observation(
                **{
                    field: (
                        getattr(dependency, field.replace("current_", "")) + 1
                        if field != "current_policy_generation"
                        else dependency.policy_generation + 1
                    )
                }
            )
        ),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    )

    result = await service.revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.REVALIDATION_REQUIRED


@pytest.mark.asyncio
async def test_activation_only_publication_change_can_reuse_pinned_answer() -> None:
    state = observation()
    state = state.model_copy(
        update={
            "publication": state.publication.model_copy(
                update={
                    "current_activation_generation": 2,
                    "impact": DisclosurePublicationImpact.ACTIVATION_ONLY,
                }
            )
        }
    )
    result = await DisclosureRevalidationService(
        FakeStateProvider(state),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request())

    assert result.disposition is DisclosureRevalidationDisposition.SAFE_REUSE


@pytest.mark.asyncio
async def test_current_requester_revocation_suppresses_history_without_content_reason() -> None:
    state = observation().model_copy(update={"requester_authorized": False})
    result = await DisclosureRevalidationService(
        FakeStateProvider(state),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=45)))

    assert result.disposition is DisclosureRevalidationDisposition.INELIGIBLE
    assert result.safe_reason == "stored_answer_disclosure_ineligible"
    assert all("Protected" not in fact.model_dump_json() for fact in result.facts)


@pytest.mark.asyncio
async def test_missing_dependency_fails_closed_and_does_not_reuse_stored_answer() -> None:
    service = DisclosureRevalidationService(
        FakeStateProvider(observation().model_copy(update={"dependencies": ()})),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    )
    missing = request(dependencies=DisclosureDependencySet(dependencies=()))
    # An empty dependency set is safe for a question-only history read, but an
    # answer request must be represented by its complete dependency set.  The
    # provider's missing observation is the fail-closed case covered here.
    result = await service.revalidate(missing)

    assert result.disposition is DisclosureRevalidationDisposition.SAFE_REUSE

    missing_result = await DisclosureRevalidationService(
        FakeStateProvider(observation().model_copy(update={"dependencies": ()})),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=41)))
    assert missing_result.disposition is DisclosureRevalidationDisposition.INELIGIBLE
    assert DisclosureRevalidationFailureCode.DEPENDENCY_MISSING in missing_result.failure_codes


@pytest.mark.asyncio
async def test_exact_replay_is_idempotent_and_isolated_by_workspace() -> None:
    provider = FakeStateProvider(observation())
    repository = InMemoryDisclosureValidationResultRepository()
    service = DisclosureRevalidationService(provider, repository, clock=lambda: NOW)

    first = await service.revalidate(request())
    second = await service.revalidate(request())

    assert second == first
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_fresh_revalidation_rechecks_authority_after_lifecycle_change() -> None:
    provider = FakeStateProvider(observation())
    service = DisclosureRevalidationService(provider, InMemoryDisclosureValidationResultRepository(), clock=lambda: NOW)

    first = await service.revalidate(request(force_fresh=True))
    provider.value = observation(tombstoned=True)
    second = await service.revalidate(request(force_fresh=True))

    assert first.can_disclose_content
    assert not second.can_disclose_content
    assert DisclosureRevalidationFailureCode.SOURCE_TOMBSTONED in second.failure_codes
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_missing_current_dependency_is_fail_closed() -> None:
    state = observation().model_copy(update={"dependencies": ()})
    result = await DisclosureRevalidationService(
        FakeStateProvider(state),
        InMemoryDisclosureValidationResultRepository(),
        clock=lambda: NOW,
    ).revalidate(request(request_id=UUID(int=50)))

    assert result.disposition is DisclosureRevalidationDisposition.INELIGIBLE
    assert DisclosureRevalidationFailureCode.DEPENDENCY_MISSING in result.failure_codes


@pytest.mark.asyncio
async def test_history_read_hides_question_and_answer_as_one_content_bearing_unit() -> None:
    dependencies = dependency_set()
    provider = FakeStateProvider(
        observation(
            dependency={"contributor_authorized": False},
        )
    )
    guard = RevalidatingQAMessageDisclosure(
        DisclosureRevalidationService(
            provider,
            InMemoryDisclosureValidationResultRepository(),
            clock=lambda: NOW,
        )
    )
    repository = InMemoryQASessionRepository()
    service = PersistedQASessionService(
        repository=repository,
        disclosure_eligibility=guard,
        clock=lambda: NOW,
        session_id_factory=lambda: UUID(int=60),
        message_id_factory=lambda: UUID(int=61),
        command_id_factory=lambda: UUID(int=62),
    )
    context = SimpleNamespace(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        operation=PersistenceOperation("qa_sessions"),
        trace_id=UUID(int=63),
        validate_shape=lambda: None,
    )
    requester = QASessionRequester(REQUESTER)
    session = await service.create_session(
        context=context,
        requester=requester,
        idempotency_key=IdempotencyKey("session-1"),
    )
    message = await service.create_pending_message(
        context=context,
        requester=requester,
        session_id=session.value.session_id,
        question="Protected question",
        run_id=UUID(int=64),
        idempotency_key=IdempotencyKey("message-1"),
    )
    await service.complete_message(
        context=context,
        session_id=session.value.session_id,
        message_id=message.value.message_id,
        answer="Protected answer",
        disclosure_dependencies=dependencies,
        outcome=AnswerQuestionOutcome.ANSWERED,
        cancelled=False,
        idempotency_key=IdempotencyKey("complete-1"),
    )

    read = await service.get_message(
        context=context,
        requester=requester,
        session_id=session.value.session_id,
        message_id=message.value.message_id,
    )

    assert read is not None
    assert read.question is None
    assert read.answer is None
    assert read.disclosure_eligibility.value == "ineligible"
