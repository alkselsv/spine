"""Explicit local agent registration and binding resolution."""

from spine.agents.catalog.ports import AgentBindingCatalog
from spine.agents.catalog.registry import (
    AgentImplementationRegistration,
    HandlerConstructionError,
    HandlerRegistry,
    HandlerRegistryError,
    HandlerSchemaError,
    is_valid_implementation_key,
)
from spine.agents.catalog.resolver import (
    AgentBindingResolver,
    BindingResolutionError,
    BindingResolutionRequest,
    DeploymentSelectionPolicy,
    ResolvedAgentBinding,
)

__all__ = [
    "AgentBindingCatalog",
    "AgentBindingResolver",
    "AgentImplementationRegistration",
    "BindingResolutionError",
    "BindingResolutionRequest",
    "DeploymentSelectionPolicy",
    "HandlerConstructionError",
    "HandlerRegistry",
    "HandlerRegistryError",
    "HandlerSchemaError",
    "ResolvedAgentBinding",
    "is_valid_implementation_key",
]
