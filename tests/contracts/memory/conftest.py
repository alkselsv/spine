from __future__ import annotations

import pytest

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


@pytest.fixture(
    params=[create_deterministic_context_projection],
    ids=["deterministic-fake"],
)
def context_projection_factory(
    request: pytest.FixtureRequest,
) -> ContextProjectionFactory:
    """Adapters extend this parameter list to run the suite unchanged."""

    return request.param
