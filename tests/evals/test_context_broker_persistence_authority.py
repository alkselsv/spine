from uuid import UUID

from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.qa_worker_composition import (
    ContextBrokerPersistenceAuthority,
)


def test_broker_persistence_authority_has_full_exact_scope_rate() -> None:
    workspace_id = UUID(int=1)
    environment_id = UUID(int=2)
    service_principal_id = UUID(int=3)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID(int=4), secret=b"b" * 32
    )
    authority = ContextBrokerPersistenceAuthority(
        boundary=boundary,
        service_principal_id=service_principal_id,
    )

    observations = tuple(
        boundary.verify(
            authority.issue(
                workspace_id=workspace_id,
                environment_id=environment_id,
                trace_id=UUID(int=trace_id),
            )
        )
        for trace_id in range(10, 16)
    )
    exact_scope_rate = sum(
        context.service_principal_id == service_principal_id
        and context.scope.workspace_id == workspace_id
        and context.scope.environment_id == environment_id
        and context.purpose.value == "answer_question"
        and context.operation.value == "retrieve_context"
        for context in observations
    ) / len(observations)

    assert exact_scope_rate == 1.0
