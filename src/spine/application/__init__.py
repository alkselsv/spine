"""Use cases coordinating domain objects through abstract ports.

The public names from the authorized broker are loaded lazily.  Memory
contracts import individual application ports, so eagerly importing the
orchestrator here would create a package-initialization cycle.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .authorized_context_broker import (
        AuthorizedContextBrokerApplicationService,
        AuthorizedContextRequest,
        ContextBrokerReadModel,
        ContextBrokerReadPort,
        ContextBundleRevalidator,
        ContextBundleStore,
        InMemoryContextBundleStore,
        SavedContextBundleAccess,
        SavedContextBundlePort,
        context_bundle_id_for_request,
        request_command_digest,
    )

__all__ = [
    "AuthorizedContextBrokerApplicationService",
    "AuthorizedContextRequest",
    "ContextBrokerReadModel",
    "ContextBrokerReadPort",
    "ContextBundleRevalidator",
    "ContextBundleStore",
    "InMemoryContextBundleStore",
    "SavedContextBundleAccess",
    "SavedContextBundlePort",
    "context_bundle_id_for_request",
    "request_command_digest",
]


def __getattr__(name: str) -> object:
    if name in __all__:
        module = import_module("spine.application.authorized_context_broker")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
