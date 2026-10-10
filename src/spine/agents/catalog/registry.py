"""Explicit local handler registration and construction."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from spine.agents.runtime import AgentHandler, CapabilitySchemaIdentity
from spine.domain.agents import AgentRuntimeKind, AgentVersion
from spine.domain.capabilities import CapabilityDefinition


_IMPLEMENTATION_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")


class HandlerRegistryError(ValueError):
    """A deterministic, disclosure-safe local registration failure."""


@dataclass(frozen=True, slots=True)
class _HandlerRegistration:
    implementation_key: str
    capability: CapabilityDefinition
    schema_identity: CapabilitySchemaIdentity
    factory: Callable[[AgentVersion], AgentHandler[Any, Any]]


def is_valid_implementation_key(value: Any) -> bool:
    """Return whether a key names an approved implementation, not executable code."""

    return (
        isinstance(value, str)
        and not value.endswith(".py")
        and _IMPLEMENTATION_KEY.fullmatch(value) is not None
    )


def _is_handler(value: Any) -> bool:
    invoke = getattr(value, "invoke", None)
    if not isinstance(value, AgentHandler) or not inspect.iscoroutinefunction(invoke):
        return False
    try:
        parameters = tuple(inspect.signature(invoke).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(parameters) == 2 and all(
        parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    )


def _is_factory(value: Any) -> bool:
    if not callable(value):
        return False
    try:
        parameters = tuple(inspect.signature(value).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(parameters) == 1 and parameters[0].kind in (
        parameters[0].POSITIONAL_ONLY,
        parameters[0].POSITIONAL_OR_KEYWORD,
    )


class HandlerRegistry:
    """A composition-root-owned registry with an explicit finalization boundary."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], _HandlerRegistration] = {}
        self._finalized = False

    def register_handler(
        self,
        *,
        implementation_key: str,
        capability: CapabilityDefinition,
        schema_identity: CapabilitySchemaIdentity,
        handler: AgentHandler[Any, Any],
    ) -> None:
        """Register one already-constructed structural handler explicitly."""

        self._validate_registration(
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=schema_identity,
        )
        if not _is_handler(handler):
            raise HandlerRegistryError("local handler registration factory is invalid")

        def factory(selected_version: AgentVersion) -> AgentHandler[Any, Any]:
            del selected_version
            return handler

        self._store(
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=schema_identity,
            factory=factory,
        )

    def register_factory(
        self,
        *,
        implementation_key: str,
        capability: CapabilityDefinition,
        schema_identity: CapabilitySchemaIdentity,
        factory: Callable[[AgentVersion], AgentHandler[Any, Any]] | None,
    ) -> None:
        """Register an explicit in-process factory for one capability mapping."""

        self._validate_registration(
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=schema_identity,
        )
        if not _is_factory(factory):
            raise HandlerRegistryError("local handler registration factory is invalid")
        self._store(
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=schema_identity,
            factory=factory,
        )

    def finalize(self) -> None:
        """Freeze registration so construction is deterministic for this process."""

        self._registrations = dict(self._registrations)
        self._finalized = True

    def construct(
        self,
        *,
        implementation_key: str,
        capability: CapabilityDefinition,
        version: AgentVersion,
    ) -> AgentHandler[Any, Any]:
        """Construct a registered handler for one already-pinned AgentVersion."""

        if not self._finalized:
            raise HandlerRegistryError("local handler registry is not finalized")
        if not is_valid_implementation_key(implementation_key):
            raise HandlerRegistryError(
                "local handler implementation key is invalid or unregistered"
            )
        registration = self._registrations.get((implementation_key, capability.key))
        if registration is None:
            raise HandlerRegistryError(
                "local handler implementation key is invalid or unregistered"
            )
        if registration.schema_identity != CapabilitySchemaIdentity.from_capability(capability):
            raise HandlerRegistryError("local handler registration schema is incompatible")
        if not isinstance(version, AgentVersion):
            raise HandlerRegistryError("local handler construction failed")
        if version.runtime is not AgentRuntimeKind.CODE:
            raise HandlerRegistryError("local handler construction failed")
        if capability.key not in version.capabilities:
            raise HandlerRegistryError("local handler construction failed")
        runtime_config = version.runtime_config
        if (
            not isinstance(runtime_config, Mapping)
            or runtime_config.get("implementation_key") != implementation_key
        ):
            raise HandlerRegistryError(
                "local handler implementation key is invalid or unregistered"
            )
        try:
            handler = registration.factory(version)
        except Exception:
            raise HandlerRegistryError("local handler construction failed") from None
        if not _is_handler(handler):
            raise HandlerRegistryError("local handler construction failed")
        return handler

    def _validate_registration(
        self,
        *,
        implementation_key: str,
        capability: CapabilityDefinition,
        schema_identity: CapabilitySchemaIdentity,
    ) -> None:
        if self._finalized:
            raise HandlerRegistryError("local handler registry is finalized")
        if not is_valid_implementation_key(implementation_key):
            raise HandlerRegistryError(
                "local handler implementation key is invalid or unregistered"
            )
        if not isinstance(capability, CapabilityDefinition):
            raise HandlerRegistryError("local handler registration is invalid")
        if not isinstance(schema_identity, CapabilitySchemaIdentity):
            raise HandlerRegistryError("local handler registration schema is incompatible")
        if schema_identity != CapabilitySchemaIdentity.from_capability(capability):
            raise HandlerRegistryError("local handler registration schema is incompatible")

    def _store(
        self,
        *,
        implementation_key: str,
        capability: CapabilityDefinition,
        schema_identity: CapabilitySchemaIdentity,
        factory: Callable[[AgentVersion], AgentHandler[Any, Any]],
    ) -> None:
        registration_key = (implementation_key, capability.key)
        if registration_key in self._registrations:
            raise HandlerRegistryError("local handler registration is duplicate")
        self._registrations[registration_key] = _HandlerRegistration(
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=schema_identity,
            factory=factory,
        )
