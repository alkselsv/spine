from __future__ import annotations

import pytest

from spine.memory.cognee.context_projection import CogneeContextProjection
from spine.memory.fake import DeterministicContextProjection, DeterministicPhysicalStore

from .adapter import (
    ContextProjectionContractAdapter,
    ContextProjectionFactory,
)


def create_deterministic_projection(
    *,
    fail_once_for: tuple[str, ...] = (),
    fail_for: tuple[str, ...] = (),
) -> DeterministicContextProjection:
    return DeterministicContextProjection(
        physical_store=DeterministicPhysicalStore(
            fail_once_for=fail_once_for,
            fail_for=fail_for,
        )
    )


def create_deterministic_context_projection() -> ContextProjectionContractAdapter:
    projection = create_deterministic_projection()
    return ContextProjectionContractAdapter(
        projection=projection,
        seed_retrieval=projection.seed_retrieval,
        create_projection=create_deterministic_projection,
    )


def create_composed_projection(
    *,
    fail_once_for: tuple[str, ...] = (),
    fail_for: tuple[str, ...] = (),
) -> CogneeContextProjection:
    retrieval = DeterministicContextProjection()
    return CogneeContextProjection(
        effect_store=DeterministicPhysicalStore(
            fail_once_for=fail_once_for,
            fail_for=fail_for,
        ),
        graph_effect_store=DeterministicPhysicalStore(),
        retrieval=retrieval,
    )


def create_composed_context_projection() -> ContextProjectionContractAdapter:
    retrieval = DeterministicContextProjection()
    projection = CogneeContextProjection(
        effect_store=DeterministicPhysicalStore(),
        graph_effect_store=DeterministicPhysicalStore(),
        retrieval=retrieval,
    )
    return ContextProjectionContractAdapter(
        projection=projection,
        seed_retrieval=retrieval.seed_retrieval,
        create_projection=create_composed_projection,
    )


@pytest.fixture(
    params=[
        create_deterministic_context_projection,
        create_composed_context_projection,
    ],
    ids=["deterministic-fake", "cognee-composition"],
)
def context_projection_factory(
    request: pytest.FixtureRequest,
) -> ContextProjectionFactory:
    """Adapters extend this parameter list to run the suite unchanged."""

    return request.param
