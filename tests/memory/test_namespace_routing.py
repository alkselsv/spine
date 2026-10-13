from __future__ import annotations

from uuid import UUID

import pytest

from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionNamespaceState,
)
from spine.memory.namespace import (
    NamespaceLifecycleError,
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRegistry,
    ProjectionNamespaceRouter,
)


WORKSPACE_A = UUID("10000000-0000-4000-8000-000000000001")
WORKSPACE_B = UUID("10000000-0000-4000-8000-000000000002")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
RUN = UUID("30000000-0000-4000-8000-000000000001")
PARTITION = UUID("40000000-0000-4000-8000-000000000001")
CONFIG_ID = UUID("50000000-0000-4000-8000-000000000001")


def namespace_context(
    *,
    workspace_id: UUID = WORKSPACE_A,
    candidate_id: UUID = UUID("60000000-0000-4000-8000-000000000001"),
    state: ProjectionNamespaceState = ProjectionNamespaceState.SHADOW,
) -> ProjectionNamespaceContext:
    return ProjectionNamespaceContext(
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        projection_kind="context",
        state=state,
        projection_candidate=ProjectionCandidate(
            candidate_id=candidate_id,
            run_id=RUN,
            partition_id=PARTITION,
            snapshot_id=UUID("70000000-0000-4000-8000-000000000001"),
        ),
        projection_config=ProjectionConfigVersion(
            config_id=CONFIG_ID,
            version="config-1",
            config_digest="a" * 64,
            ontology_version="ontology-1",
            adapter_contract_version="context-projection:v1",
        ),
        canonical_boundary=CanonicalBoundary(
            boundary_id=UUID("80000000-0000-4000-8000-000000000001"),
            boundary_digest="b" * 64,
            accepted_through_generation=1,
        ),
    )


def test_router_derives_stable_namespaces_from_full_security_scope() -> None:
    router = ProjectionNamespaceRouter()

    first = router.route(namespace_context())
    repeat = router.route(namespace_context())
    other_workspace = router.route(namespace_context(workspace_id=WORKSPACE_B))
    other_candidate = router.route(
        namespace_context(candidate_id=UUID("60000000-0000-4000-8000-000000000002"))
    )

    assert first == repeat
    assert first.namespace_digest == first.namespace.removeprefix("spine/context/v1/")
    assert first != other_workspace
    assert first != other_candidate


def test_router_rejects_a_namespace_selected_outside_the_trusted_context() -> None:
    router = ProjectionNamespaceRouter()
    trusted = namespace_context()
    issued = router.route(trusted)
    forged = issued.model_copy(update={"namespace": "provider-owned/anything"})

    with pytest.raises(NamespaceRoutingError):
        router.validate(forged, trusted)


def test_registry_excludes_failed_and_retired_namespaces_from_active_candidates() -> None:
    registry = ProjectionNamespaceRegistry()
    shadow = registry.register(namespace_context())
    active = registry.transition(shadow, ProjectionNamespaceState.ACTIVE)
    failed = registry.register(
        namespace_context(
            candidate_id=UUID("60000000-0000-4000-8000-000000000002"),
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    failed = registry.transition(failed, ProjectionNamespaceState.FAILED)
    retired = registry.register(
        namespace_context(
            candidate_id=UUID("60000000-0000-4000-8000-000000000003"),
            state=ProjectionNamespaceState.SHADOW,
        )
    )
    retired = registry.transition(retired, ProjectionNamespaceState.RETIRED)

    candidates = registry.active_candidates(
        workspace_id=WORKSPACE_A,
        environment_id=ENVIRONMENT,
        projection_kind="context",
    )

    assert candidates == (active,)
    assert failed not in candidates
    assert retired not in candidates


def test_registry_requires_explicit_lifecycle_transition() -> None:
    registry = ProjectionNamespaceRegistry()
    shadow = registry.register(namespace_context())

    with pytest.raises(NamespaceLifecycleError):
        registry.register(namespace_context(state=ProjectionNamespaceState.ACTIVE))

    active = registry.transition(shadow, ProjectionNamespaceState.ACTIVE)

    with pytest.raises(NamespaceLifecycleError):
        registry.transition(active, ProjectionNamespaceState.REBUILDING)
