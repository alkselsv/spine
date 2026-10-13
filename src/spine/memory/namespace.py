"""Provider-neutral physical namespace routing and lifecycle isolation."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel

from .contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionCommand,
    ProjectionConfigVersion,
    ProjectionNamespace,
    ProjectionNamespaceState,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class NamespaceRoutingError(ValueError):
    """The physical namespace does not match its trusted logical context."""


class NamespaceLifecycleError(ValueError):
    """A namespace lifecycle transition or lookup is not allowed."""


class ProjectionNamespaceContext(DefinitionModel):
    """Trusted logical inputs from which a physical namespace is derived."""

    schema_version: Literal["projection-namespace-context:v1"] = (
        "projection-namespace-context:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(
        default="context",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    )
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    state: ProjectionNamespaceState = ProjectionNamespaceState.SHADOW

    @model_validator(mode="after")
    def validate_scope(self) -> "ProjectionNamespaceContext":
        for field_name in ("workspace_id", "environment_id"):
            value = getattr(self, field_name)
            if value.int == 0:
                raise ValueError(f"{field_name} must be a non-zero UUID")
        return self

    @classmethod
    def from_command(cls, command: ProjectionCommand) -> "ProjectionNamespaceContext":
        """Reconstruct the routing context carried by a projection command."""

        return cls(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            projection_kind=command.physical_namespace.projection_kind,
            projection_candidate=command.projection_candidate,
            projection_config=command.projection_config,
            canonical_boundary=command.canonical_boundary,
            state=command.physical_namespace.state,
        )


def namespace_identity_digest(context: ProjectionNamespaceContext) -> str:
    """Return the stable digest for the context, excluding mutable lifecycle."""

    payload = context.model_dump(mode="json", exclude={"state"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class ProjectionNamespaceRouter:
    """Derive and validate opaque namespaces from trusted logical context."""

    def route(self, context: ProjectionNamespaceContext) -> ProjectionNamespace:
        digest = namespace_identity_digest(context)
        return ProjectionNamespace(
            namespace=f"spine/{context.projection_kind}/v1/{digest}",
            namespace_digest=digest,
            projection_kind=context.projection_kind,
            state=context.state,
        )

    def validate(
        self,
        namespace: ProjectionNamespace,
        context: ProjectionNamespaceContext,
    ) -> ProjectionNamespace:
        """Validate provider input against server-derived identity and state."""

        expected = self.route(context)
        if namespace != expected:
            raise NamespaceRoutingError(
                "physical namespace does not match the trusted projection context"
            )
        return expected

    @staticmethod
    def is_ordinary_retrieval(namespace: ProjectionNamespace) -> bool:
        return namespace.state is ProjectionNamespaceState.ACTIVE


_ALLOWED_TRANSITIONS: dict[ProjectionNamespaceState, frozenset[ProjectionNamespaceState]] = {
    ProjectionNamespaceState.ACTIVE: frozenset({ProjectionNamespaceState.RETIRED}),
    ProjectionNamespaceState.SHADOW: frozenset(
        {
            ProjectionNamespaceState.REBUILDING,
            ProjectionNamespaceState.ACTIVE,
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }
    ),
    ProjectionNamespaceState.REBUILDING: frozenset(
        {
            ProjectionNamespaceState.ACTIVE,
            ProjectionNamespaceState.FAILED,
            ProjectionNamespaceState.RETIRED,
        }
    ),
    ProjectionNamespaceState.FAILED: frozenset(
        {ProjectionNamespaceState.REBUILDING, ProjectionNamespaceState.RETIRED}
    ),
    ProjectionNamespaceState.RETIRED: frozenset(),
}


class ProjectionNamespaceRegistry:
    """Deterministic in-memory lifecycle catalog for adapter qualification."""

    def __init__(self, router: ProjectionNamespaceRouter | None = None) -> None:
        self._router = router or ProjectionNamespaceRouter()
        self._contexts: dict[str, ProjectionNamespaceContext] = {}

    def register(self, context: ProjectionNamespaceContext) -> ProjectionNamespace:
        namespace = self._router.route(context)
        existing = self._contexts.get(namespace.namespace_digest)
        if existing is not None:
            if existing != context:
                raise NamespaceLifecycleError(
                    "namespace identity is already registered with another lifecycle state"
                )
            return namespace
        self._contexts[namespace.namespace_digest] = context
        return namespace

    def transition(
        self,
        namespace: ProjectionNamespace,
        state: ProjectionNamespaceState,
    ) -> ProjectionNamespace:
        context = self._contexts.get(namespace.namespace_digest)
        if context is None:
            raise NamespaceLifecycleError("namespace is not registered")
        self._router.validate(namespace, context)
        if state not in _ALLOWED_TRANSITIONS[context.state]:
            raise NamespaceLifecycleError(
                f"cannot transition namespace from {context.state.value} to {state.value}"
            )
        updated = context.model_copy(update={"state": state})
        self._contexts[namespace.namespace_digest] = updated
        return self._router.route(updated)

    def require(
        self,
        namespace: ProjectionNamespace,
        context: ProjectionNamespaceContext,
    ) -> ProjectionNamespace:
        """Require a registered namespace and exact trusted context match."""

        self._router.validate(namespace, context)
        stored = self._contexts.get(namespace.namespace_digest)
        if stored != context:
            raise NamespaceLifecycleError("namespace is not registered for this context")
        return self._router.route(stored)

    def active_candidates(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_kind: str = "context",
    ) -> tuple[ProjectionNamespace, ...]:
        """Return only active namespaces eligible for ordinary retrieval."""

        contexts: Iterable[ProjectionNamespaceContext] = (
            context
            for context in self._contexts.values()
            if context.workspace_id == workspace_id
            and context.environment_id == environment_id
            and context.projection_kind == projection_kind
            and context.state is ProjectionNamespaceState.ACTIVE
        )
        return tuple(self._router.route(context) for context in contexts)

    def active_namespaces(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_kind: str = "context",
    ) -> tuple[ProjectionNamespace, ...]:
        """Compatibility alias for callers that ask for active namespaces."""

        return self.active_candidates(
            workspace_id=workspace_id,
            environment_id=environment_id,
            projection_kind=projection_kind,
        )


__all__ = [
    "NamespaceLifecycleError",
    "NamespaceRoutingError",
    "ProjectionNamespaceContext",
    "ProjectionNamespaceRegistry",
    "ProjectionNamespaceRouter",
    "namespace_identity_digest",
]
