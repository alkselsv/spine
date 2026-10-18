from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from spine.api.composition import DurableAskComposition
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin, EnvironmentKind
from spine.evals.durable_qa_observer import (
    DurableProductionQAAFlow,
    ObserverAuthorization,
)
from spine.evals.production_contract import (
    CaseExecutionDisposition,
    CaseExecutionFailureCategory,
    ProductionContractCaseRequest,
    ProductionContractObserver,
)
from tests.evals.test_production_observer import _request


_WORKSPACE = UUID("42000000-0000-0000-0000-000000000478")
_ENVIRONMENT = UUID("43000000-0000-0000-0000-000000000478")
_SUBJECT = UUID("44000000-0000-0000-0000-000000000478")


def _composition() -> DurableAskComposition:
    return DurableAskComposition(
        admission=object(),
        api_ask_service=object(),
        workflow_executor=object(),
        delivery_bindings=(),
        workflow_definition=object,
        activity_bindings=(object(), object()),
        observer_admission=object(),
        sessions=object(),
        capture_channel=object(),
    )  # type: ignore[arg-type]


def _context(workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id, environment_id),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("qa.ask"),
        operation=PersistenceOperation("qa.ask-admission-cancellation"),
        trace_id=UUID("45000000-0000-0000-0000-000000000478"),
        acting_subject_id=_SUBJECT,
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=UUID("46000000-0000-0000-0000-000000000478"),
            signature=b"test-signature",
        ),
    )


class _Authorization:
    def __init__(self, context: TrustedPersistenceContext) -> None:
        self.context = context

    async def authorize(self, *, request: ProductionContractCaseRequest):
        del request
        return ObserverAuthorization(
            context=self.context,
            environment=EnvironmentKind.PRODUCTION,
            authorization_generation=3,
        )


@pytest.mark.asyncio
async def test_durable_observer_denies_changed_runtime_scope_without_admission() -> None:
    request = _request()
    flow = DurableProductionQAAFlow(
        composition=_composition(),
        authorization=_Authorization(_context(_WORKSPACE, _ENVIRONMENT)),
        waiter=object(),  # type: ignore[arg-type]
        wait_timeout=timedelta(seconds=1),
    )

    record = await ProductionContractObserver(flow=flow).observe(request=request)

    assert record.disposition is CaseExecutionDisposition.DENIED
    assert record.failure_category is CaseExecutionFailureCategory.AUTHORIZATION_DENIED
    assert record.observation is None
    assert record.correlation_id is not None
    assert request.question not in record.model_dump_json()


@pytest.mark.asyncio
async def test_durable_observer_without_capture_composition_fails_closed() -> None:
    composition = DurableAskComposition(
        admission=object(),
        api_ask_service=object(),
        workflow_executor=object(),
        delivery_bindings=(),
        workflow_definition=object,
        activity_bindings=(object(), object()),
    )  # type: ignore[arg-type]
    flow = DurableProductionQAAFlow(
        composition=composition,
        authorization=_Authorization(_context(_WORKSPACE, _ENVIRONMENT)),
        waiter=object(),  # type: ignore[arg-type]
    )

    record = await ProductionContractObserver(flow=flow).observe(request=_request())

    assert record.disposition is CaseExecutionDisposition.UNAVAILABLE
    assert record.observation is None
    assert "protected" not in record.model_dump_json()
