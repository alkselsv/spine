from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

import pytest
from sqlalchemy.engine import Result

from spine.application.commit_fence import (
    CommitFenceBinding,
    canonical_commit_fence_lineage_digest,
)
from spine.application.persistence.context import EnvironmentScope
from spine.application.private_asr_corpus import PrivateArtifactSourceLineage
from spine.infrastructure.commit_fence_composition import (
    CommitFenceCompositionDenied,
    FenceAuthorityKind,
    FenceBindingUse,
    PostgreSQLFenceSource,
    ProductionCommitFenceComposition,
    QUALIFIED_POSTGRESQL_F1_OWNERS,
    build_production_commit_fence_composition,
    build_test_commit_fence_composition,
)
from spine.infrastructure.persistence.postgresql_commit_fence import (
    PostgreSQLFenceOwner,
    postgresql_f1_issuer_id,
)
from tests.infrastructure.fixtures.synthetic_commit_fence import (
    ManualCommitFenceClock,
    SyntheticCommitFenceAuthority,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
SCOPE = EnvironmentScope(workspace_id=UUID(int=1), environment_id=UUID(int=2))
USE = FenceBindingUse(purpose="asr_model_evaluation", action="publish_report")


class _PostgreSQLUnitOfWork:
    def __init__(self, scope: EnvironmentScope = SCOPE) -> None:
        self._scope = scope

    @property
    def scope(self) -> EnvironmentScope:
        return self._scope

    async def execute_statement(
        self, statement: object, parameters: object | None = None
    ) -> Result[Any]:
        del statement, parameters
        raise AssertionError("selection must not execute PostgreSQL statements")

    def register_commit_guard(self, guard: Callable[[], bool]) -> None:
        del guard
        raise AssertionError("selection must not register a guard before acquisition")


def _binding(**updates: object) -> CommitFenceBinding:
    lineage = (
        PrivateArtifactSourceLineage(
            source_object_id=UUID(int=4),
            source_revision_id=UUID(int=5),
            source_revision_digest="a" * 64,
        ),
    )
    values: dict[str, object] = {
        "issuer_id": postgresql_f1_issuer_id(PostgreSQLFenceOwner.ACCESS_POLICY),
        "approval_version": "policy-v1",
        "approval_digest": "b" * 64,
        "workspace_id": SCOPE.workspace_id,
        "environment_id": SCOPE.environment_id,
        "purpose": USE.purpose,
        "action": USE.action,
        "subject_id": UUID(int=4),
        "state_digest": "b" * 64,
        "source_lineage": lineage,
        "lineage_digest": canonical_commit_fence_lineage_digest(
            workspace_id=SCOPE.workspace_id,
            environment_id=SCOPE.environment_id,
            source_lineage=lineage,
        ),
        "generation": 1,
        "revocation_epoch": 1,
        "operation_id": UUID(int=6),
        "idempotency_key": "issue-980",
        "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=1),
    }
    values.update(updates)
    if {"workspace_id", "environment_id", "source_lineage"} & updates.keys():
        values["lineage_digest"] = canonical_commit_fence_lineage_digest(
            workspace_id=values["workspace_id"],  # type: ignore[arg-type]
            environment_id=values["environment_id"],  # type: ignore[arg-type]
            source_lineage=values["source_lineage"],  # type: ignore[arg-type]
        )
    return CommitFenceBinding.model_validate(values)


def test_production_composition_selects_named_f1_owner_for_exact_purpose() -> None:
    composition = build_production_commit_fence_composition(
        scope=SCOPE,
        postgresql_sources=(
            PostgreSQLFenceSource(
                owner=PostgreSQLFenceOwner.ACCESS_POLICY,
                allowed_uses=frozenset({USE}),
            ),
        ),
    )

    selected = composition.resolve(_binding(), postgresql_uow=_PostgreSQLUnitOfWork())

    assert selected.kind is FenceAuthorityKind.POSTGRESQL_F1
    assert selected.owner is PostgreSQLFenceOwner.ACCESS_POLICY


def test_production_f1_owner_allowlist_contains_only_demonstrated_pg_owners() -> None:
    assert QUALIFIED_POSTGRESQL_F1_OWNERS == {
        PostgreSQLFenceOwner.SOURCE_LIFECYCLE,
        PostgreSQLFenceOwner.ACCESS_POLICY,
        PostgreSQLFenceOwner.RETENTION_APPLICABILITY,
    }


@pytest.mark.parametrize(
    "updates",
    (
        {"purpose": "different_purpose"},
        {"action": "delete_artifact"},
        {"issuer_id": UUID(int=999)},
        {"workspace_id": UUID(int=998)},
        {"environment_id": UUID(int=997)},
    ),
)
def test_production_composition_denies_unqualified_or_cross_scope_binding(
    updates: dict[str, object],
) -> None:
    composition = build_production_commit_fence_composition(
        scope=SCOPE,
        postgresql_sources=(
            PostgreSQLFenceSource(
                owner=PostgreSQLFenceOwner.ACCESS_POLICY,
                allowed_uses=frozenset({USE}),
            ),
        ),
    )

    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        composition.resolve(_binding(**updates), postgresql_uow=_PostgreSQLUnitOfWork())


def test_production_composition_defaults_to_deny_when_authority_is_missing() -> None:
    composition = build_production_commit_fence_composition(scope=SCOPE)

    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        composition.resolve(_binding(), postgresql_uow=_PostgreSQLUnitOfWork())


def test_production_composition_denies_f1_without_transaction_bound_uow() -> None:
    composition = build_production_commit_fence_composition(
        scope=SCOPE,
        postgresql_sources=(
            PostgreSQLFenceSource(
                owner=PostgreSQLFenceOwner.ACCESS_POLICY,
                allowed_uses=frozenset({USE}),
            ),
        ),
    )

    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        composition.resolve(_binding())


def test_production_composition_denies_cross_workspace_uow() -> None:
    composition = build_production_commit_fence_composition(
        scope=SCOPE,
        postgresql_sources=(
            PostgreSQLFenceSource(
                owner=PostgreSQLFenceOwner.ACCESS_POLICY,
                allowed_uses=frozenset({USE}),
            ),
        ),
    )
    foreign_uow = _PostgreSQLUnitOfWork(
        EnvironmentScope(workspace_id=UUID(int=100), environment_id=UUID(int=101))
    )

    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        composition.resolve(_binding(), postgresql_uow=foreign_uow)


def test_production_composition_rejects_unknown_f1_owner_registration() -> None:
    with pytest.raises(ValueError, match="named PostgreSQL F1 owner"):
        PostgreSQLFenceSource(  # type: ignore[arg-type]
            owner="unknown_owner",
            allowed_uses=frozenset({USE}),
        )


def test_production_factory_rejects_synthetic_f2_registration() -> None:
    synthetic = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))

    with pytest.raises(ValueError, match="only qualified F1 sources"):
        build_production_commit_fence_composition(
            scope=SCOPE,
            postgresql_sources=(synthetic,),  # type: ignore[arg-type]
        )


def test_production_constructor_cannot_bypass_synthetic_rejection() -> None:
    synthetic = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))

    with pytest.raises(ValueError, match="only qualified F1 sources"):
        ProductionCommitFenceComposition(
            scope=SCOPE,
            postgresql_sources=(synthetic,),  # type: ignore[arg-type]
        )


def test_production_composition_rejects_unknown_configuration_version() -> None:
    with pytest.raises(ValueError, match="version is unsupported"):
        ProductionCommitFenceComposition(
            scope=SCOPE,
            schema_version="commit-fence-production-composition:v2",  # type: ignore[arg-type]
        )


def test_synthetic_authority_is_available_only_in_exact_test_composition() -> None:
    binding = _binding(issuer_id=UUID(int=50))
    synthetic = SyntheticCommitFenceAuthority(clock=ManualCommitFenceClock(NOW))
    test_composition = build_test_commit_fence_composition(
        exact_binding=binding,
        authority=synthetic,
    )
    production = build_production_commit_fence_composition(scope=SCOPE)

    selected = test_composition.resolve(binding)

    assert selected.kind is FenceAuthorityKind.TEST_FIXTURE
    assert selected.authority is synthetic
    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        production.resolve(binding)
    with pytest.raises(CommitFenceCompositionDenied, match="unavailable"):
        test_composition.resolve(
            binding.model_copy(update={"operation_id": UUID(int=51)})
        )
