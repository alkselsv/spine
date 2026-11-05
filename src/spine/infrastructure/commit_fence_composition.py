"""Fail-closed production composition for commit-fence authorities.

The composition selects an already qualified PostgreSQL owner for one exact
binding. It is deliberately not an authority registry: sources are immutable
deployment inputs and an unknown or absent source is always denied. External F2
sources have no production enrollment seam until a named provider qualification
supplies the evidence required by #983.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Literal
from uuid import UUID

from spine.application.commit_fence import CommitFenceBinding
from spine.application.commit_fence_provider_port import ExternalCommitFencePort
from spine.application.persistence.context import EnvironmentScope
from spine.infrastructure.persistence.postgresql_commit_fence import (
    PostgreSQLCommitFence,
    PostgreSQLFenceOwner,
    PostgreSQLFenceUnitOfWork,
    postgresql_f1_issuer_id,
)

_SCOPE_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")

QUALIFIED_POSTGRESQL_F1_OWNERS = frozenset(
    {
        PostgreSQLFenceOwner.SOURCE_LIFECYCLE,
        PostgreSQLFenceOwner.ACCESS_POLICY,
        PostgreSQLFenceOwner.RETENTION_APPLICABILITY,
    }
)


class CommitFenceCompositionDenied(PermissionError):
    """Disclosure-safe denial from the composition boundary."""


class FenceAuthorityKind(str, Enum):
    POSTGRESQL_F1 = "postgresql_f1"
    TEST_FIXTURE = "test_fixture"


@dataclass(frozen=True, slots=True)
class FenceBindingUse:
    """One exact purpose/action pair allowed by a consuming composition."""

    purpose: str
    action: str

    def __post_init__(self) -> None:
        if (
            _SCOPE_TOKEN.fullmatch(self.purpose) is None
            or _SCOPE_TOKEN.fullmatch(self.action) is None
        ):
            raise ValueError("fence binding use must contain valid purpose and action")


@dataclass(frozen=True, slots=True)
class PostgreSQLFenceSource:
    """Versioned, purpose-scoped F1 source for one canonical PG owner."""

    owner: PostgreSQLFenceOwner
    allowed_uses: frozenset[FenceBindingUse]
    schema_version: Literal["postgresql-f1-composition-source:v1"] = (
        "postgresql-f1-composition-source:v1"
    )

    def __post_init__(self) -> None:
        if self.schema_version != "postgresql-f1-composition-source:v1":
            raise ValueError("PostgreSQL F1 source version is unsupported")
        if (
            type(self.owner) is not PostgreSQLFenceOwner
            or self.owner not in QUALIFIED_POSTGRESQL_F1_OWNERS
        ):
            raise ValueError("commit fence requires a named PostgreSQL F1 owner")
        if not self.allowed_uses or any(
            type(use) is not FenceBindingUse for use in self.allowed_uses
        ):
            raise ValueError("PostgreSQL F1 source requires explicit allowed uses")

    @property
    def issuer_id(self) -> UUID:
        return postgresql_f1_issuer_id(self.owner)


@dataclass(frozen=True, slots=True)
class PostgreSQLFenceSelection:
    kind: FenceAuthorityKind
    owner: PostgreSQLFenceOwner
    authority: PostgreSQLCommitFence


@dataclass(frozen=True, slots=True)
class TestFenceSelection:
    kind: FenceAuthorityKind
    authority: ExternalCommitFencePort


@dataclass(frozen=True, slots=True)
class ProductionCommitFenceComposition:
    """Versioned, immutable, default-deny selection for one deployment scope."""

    scope: EnvironmentScope
    postgresql_sources: tuple[PostgreSQLFenceSource, ...] = ()
    schema_version: Literal["commit-fence-production-composition:v1"] = (
        "commit-fence-production-composition:v1"
    )

    def __post_init__(self) -> None:
        if self.schema_version != "commit-fence-production-composition:v1":
            raise ValueError(
                "commit-fence production composition version is unsupported"
            )
        if type(self.scope) is not EnvironmentScope:
            raise ValueError(
                "commit-fence production composition requires environment scope"
            )
        if any(
            type(source) is not PostgreSQLFenceSource
            for source in self.postgresql_sources
        ):
            raise ValueError("production composition accepts only qualified F1 sources")
        identities = tuple(
            (source.issuer_id, use)
            for source in self.postgresql_sources
            for use in source.allowed_uses
        )
        if len(set(identities)) != len(identities):
            raise ValueError("commit-fence production sources must not overlap")

    def resolve(
        self,
        binding: CommitFenceBinding,
        *,
        postgresql_uow: PostgreSQLFenceUnitOfWork | None = None,
    ) -> PostgreSQLFenceSelection:
        if (
            binding.workspace_id,
            binding.environment_id,
        ) != (self.scope.workspace_id, self.scope.environment_id):
            raise CommitFenceCompositionDenied("Commit-fence authority is unavailable.")
        use = FenceBindingUse(purpose=binding.purpose, action=binding.action)
        matches = tuple(
            source
            for source in self.postgresql_sources
            if source.issuer_id == binding.issuer_id and use in source.allowed_uses
        )
        if len(matches) != 1 or postgresql_uow is None:
            raise CommitFenceCompositionDenied("Commit-fence authority is unavailable.")
        try:
            uow_scope = postgresql_uow.scope
        except Exception:
            raise CommitFenceCompositionDenied(
                "Commit-fence authority is unavailable."
            ) from None
        if type(uow_scope) is not EnvironmentScope or (
            uow_scope.workspace_id,
            uow_scope.environment_id,
        ) != (self.scope.workspace_id, self.scope.environment_id):
            raise CommitFenceCompositionDenied("Commit-fence authority is unavailable.")
        source = matches[0]
        return PostgreSQLFenceSelection(
            kind=FenceAuthorityKind.POSTGRESQL_F1,
            owner=source.owner,
            authority=PostgreSQLCommitFence(postgresql_uow),
        )


@dataclass(frozen=True, slots=True)
class TestCommitFenceComposition:
    """One exact fixture binding kept outside production source selection."""

    exact_binding: CommitFenceBinding
    authority: ExternalCommitFencePort
    schema_version: Literal["commit-fence-test-composition:v1"] = (
        "commit-fence-test-composition:v1"
    )

    def __post_init__(self) -> None:
        if self.schema_version != "commit-fence-test-composition:v1":
            raise ValueError("commit-fence test composition version is unsupported")
        required_operations = ("acquire", "validate_before_commit", "finish", "abort")
        if not all(
            callable(getattr(self.authority, name, None))
            for name in required_operations
        ):
            raise ValueError("test fence authority does not implement the F2 port")

    def resolve(self, binding: CommitFenceBinding) -> TestFenceSelection:
        if binding != self.exact_binding:
            raise CommitFenceCompositionDenied("Commit-fence authority is unavailable.")
        return TestFenceSelection(
            kind=FenceAuthorityKind.TEST_FIXTURE,
            authority=self.authority,
        )


def build_production_commit_fence_composition(
    *,
    scope: EnvironmentScope,
    postgresql_sources: tuple[PostgreSQLFenceSource, ...] = (),
) -> ProductionCommitFenceComposition:
    """Build a production selector; every external or absent source is DENY."""

    return ProductionCommitFenceComposition(
        scope=scope,
        postgresql_sources=postgresql_sources,
    )


def build_test_commit_fence_composition(
    *,
    exact_binding: CommitFenceBinding,
    authority: ExternalCommitFencePort,
) -> TestCommitFenceComposition:
    """Bind a fixture authority to one exact test binding only."""

    return TestCommitFenceComposition(exact_binding=exact_binding, authority=authority)


__all__ = [
    "CommitFenceCompositionDenied",
    "FenceAuthorityKind",
    "FenceBindingUse",
    "PostgreSQLFenceSelection",
    "PostgreSQLFenceSource",
    "ProductionCommitFenceComposition",
    "QUALIFIED_POSTGRESQL_F1_OWNERS",
    "TestCommitFenceComposition",
    "TestFenceSelection",
    "build_production_commit_fence_composition",
    "build_test_commit_fence_composition",
]
