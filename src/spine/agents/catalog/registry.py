"""Explicit local handler registration and construction."""

from __future__ import annotations

import inspect
import re
from asyncio import CancelledError
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from spine.agents.runtime import AgentHandler, CapabilitySchemaIdentity
from spine.agents.runtime.context import AgentExecutionContextAdmission
from spine.agents.runtime.contracts import AgentExecutionContext, AgentRequest, AgentResponse
from spine.domain.agents import AgentRuntimeKind, AgentVersion
from spine.domain.capabilities import CapabilityDefinition

if TYPE_CHECKING:
    from spine.agents.catalog.resolver import ResolvedAgentBinding


_IMPLEMENTATION_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")


class HandlerRegistryError(ValueError):
    """A deterministic, disclosure-safe local registration failure."""


class HandlerSchemaError(HandlerRegistryError):
    """A registration does not match the requested capability schemas."""


class HandlerConstructionError(HandlerRegistryError):
    """A registered factory failed or returned an invalid handler."""


def is_valid_implementation_key(value: Any) -> bool:
    """Return whether a key names an approved implementation, not executable code."""

    return (
        isinstance(value, str)
        and not value.endswith(".py")
        and _IMPLEMENTATION_KEY.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True)
class AgentImplementationRegistration:
    """Trusted immutable metadata for one version/capability implementation."""

    agent_version_id: UUID
    agent_id: UUID
    runtime: AgentRuntimeKind
    implementation_key: str
    capability: CapabilityDefinition
    schema_identity: CapabilitySchemaIdentity

    @classmethod
    def from_version(
        cls,
        *,
        version: AgentVersion,
        capability: CapabilityDefinition,
        implementation_key: str,
    ) -> AgentImplementationRegistration:
        return cls(
            agent_version_id=version.id,
            agent_id=version.agent_id,
            runtime=version.runtime,
            implementation_key=implementation_key,
            capability=capability,
            schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.agent_version_id, UUID) or self.agent_version_id.int == 0:
            raise HandlerRegistryError("local handler registration version is invalid")
        if not isinstance(self.agent_id, UUID) or self.agent_id.int == 0:
            raise HandlerRegistryError("local handler registration agent is invalid")
        if self.runtime is AgentRuntimeKind.EXTERNAL:
            raise HandlerRegistryError("local handler registration runtime is unsupported")
        if self.runtime not in (AgentRuntimeKind.CODE, AgentRuntimeKind.LLM):
            raise HandlerRegistryError("local handler registration runtime is invalid")
        if not is_valid_implementation_key(self.implementation_key):
            raise HandlerRegistryError(
                "local handler implementation key is invalid or unregistered"
            )
        if not isinstance(self.capability, CapabilityDefinition):
            raise HandlerRegistryError("local handler registration is invalid")
        if not isinstance(self.schema_identity, CapabilitySchemaIdentity):
            raise HandlerSchemaError("local handler registration schema is incompatible")
        if self.schema_identity != CapabilitySchemaIdentity.from_capability(self.capability):
            raise HandlerSchemaError("local handler registration schema is incompatible")

    def validate_for(
        self,
        *,
        version: AgentVersion,
        capability: CapabilityDefinition,
    ) -> None:
        """Require exact version, runtime, capability and schema identity."""

        if not isinstance(version, AgentVersion) or not isinstance(
            capability, CapabilityDefinition
        ):
            raise HandlerRegistryError("local handler registration is incompatible")
        if self.agent_version_id != version.id or self.agent_id != version.agent_id:
            raise HandlerRegistryError("local handler registration is incompatible")
        if self.runtime is not version.runtime:
            raise HandlerRegistryError("local handler registration is incompatible")
        if capability.key not in version.capabilities:
            raise HandlerRegistryError("local handler capability is unsupported")
        if self.capability.key != capability.key:
            raise HandlerRegistryError("local handler registration is incompatible")
        if self.schema_identity != CapabilitySchemaIdentity.from_capability(capability):
            raise HandlerSchemaError("local handler registration schema is incompatible")


@dataclass(frozen=True, slots=True)
class _HandlerRegistration:
    descriptor: AgentImplementationRegistration
    factory: Callable[[AgentVersion], AgentHandler[Any, Any]]


def _is_handler(value: Any) -> bool:
    invoke = getattr(value, "invoke", None)
    if not isinstance(value, AgentHandler) or not inspect.iscoroutinefunction(invoke):
        return False
    try:
        parameters = tuple(inspect.signature(invoke).parameters.values())
    except (TypeError, ValueError):
        return False
    if len(parameters) != 2:
        return False
    first, second = parameters
    return (
        first.kind in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD)
        and second.kind in (second.POSITIONAL_ONLY, second.POSITIONAL_OR_KEYWORD)
    )


class _RegisteredHandler:
    """Unbound registry selection; direct execution always fails closed."""

    __slots__ = ("_handler", "_registration")

    def __init__(
        self,
        handler: AgentHandler[Any, Any],
        registration: AgentImplementationRegistration,
    ) -> None:
        self._handler = handler
        self._registration = registration

    @property
    def registration(self) -> AgentImplementationRegistration:
        return self._registration

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        del request, context
        raise TypeError("handler requires a trusted execution gate")

    async def _invoke_selected(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        selected = getattr(self._handler, "_invoke_selected", None)
        if callable(selected):
            return await selected(request, context)
        return await self._handler.invoke(request, context)


class _BoundAgentHandler:
    """Two-argument execution gate bound to one registry selection."""

    __slots__ = (
        "_selected",
        "_context",
        "_admission",
        "_trusted_authority",
        "_resolved_binding",
    )

    def __init__(
        self,
        *,
        selected: _RegisteredHandler,
        context: AgentExecutionContext,
        admission: AgentExecutionContextAdmission,
        trusted_authority: object,
        resolved_binding: ResolvedAgentBinding,
    ) -> None:
        self._selected = selected
        self._context = context
        self._admission = admission
        self._trusted_authority = trusted_authority
        self._resolved_binding = resolved_binding

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        if context is not self._context:
            raise TypeError("handler execution context is not admitted")
        try:
            self._admission.verify_selection(
                context,
                self._resolved_binding,
                self._trusted_authority,
            )
            self._admission.verify_request(request, context)
        except Exception:
            raise TypeError("handler execution context is not admitted") from None
        return await self._selected._invoke_selected(request, context)


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

    def __init__(self, *, execution_admission_authority: object | None = None) -> None:
        self._registrations: dict[tuple[UUID, str], _HandlerRegistration] = {}
        self._finalized = False
        self._execution_admission_authority = execution_admission_authority

    def register_handler(
        self,
        *,
        registration: AgentImplementationRegistration,
        handler: AgentHandler[Any, Any],
    ) -> None:
        """Register one already-constructed structural handler explicitly."""

        self._validate_registration(registration)
        if not _is_handler(handler):
            raise HandlerRegistryError("local handler registration factory is invalid")

        def factory(selected_version: AgentVersion) -> AgentHandler[Any, Any]:
            del selected_version
            return handler

        self._store(registration=registration, factory=factory)

    def register_factory(
        self,
        *,
        registration: AgentImplementationRegistration,
        factory: Callable[[AgentVersion], AgentHandler[Any, Any]] | None,
    ) -> None:
        """Register an explicit in-process factory for one version mapping."""

        self._validate_registration(registration)
        if not _is_factory(factory):
            raise HandlerRegistryError("local handler registration factory is invalid")
        self._store(registration=registration, factory=factory)

    def finalize(self) -> None:
        """Freeze registration so construction is deterministic for this process."""

        self._registrations = dict(self._registrations)
        self._finalized = True

    def registration_for(
        self,
        *,
        capability: CapabilityDefinition,
        version: AgentVersion,
    ) -> AgentImplementationRegistration:
        """Return the exact trusted descriptor for a pinned version and capability."""

        self._require_finalized()
        if not isinstance(capability, CapabilityDefinition) or not isinstance(
            version, AgentVersion
        ):
            raise HandlerRegistryError("local handler registration is incompatible")
        stored = self._registrations.get((version.id, capability.key))
        if stored is None:
            raise HandlerRegistryError("local handler implementation is unavailable")
        stored.descriptor.validate_for(version=version, capability=capability)
        return stored.descriptor

    def construct(
        self,
        *,
        capability: CapabilityDefinition,
        version: AgentVersion,
    ) -> AgentHandler[Any, Any]:
        """Construct a registered handler for one already-pinned AgentVersion."""

        registration = self.registration_for(capability=capability, version=version)
        stored = self._registrations[(registration.agent_version_id, capability.key)]
        try:
            handler = stored.factory(version)
        except CancelledError:
            raise
        except Exception as error:
            raise HandlerConstructionError("local handler construction failed") from error
        if not _is_handler(handler):
            raise HandlerConstructionError("local handler construction failed")
        return _RegisteredHandler(handler, registration)

    def bind_for_execution(
        self,
        *,
        handler: AgentHandler[Any, Any],
        context: AgentExecutionContext,
        admission: AgentExecutionContextAdmission,
        resolved_binding: ResolvedAgentBinding,
    ) -> AgentHandler[Any, Any]:
        """Bind one registry selection to a current trusted admission.

        This is the composition-owned execution gate. The registry's
        composition-bound authority must be the same authority captured by the
        factory that issued the admission. The returned
        handler keeps the approved two-argument public contract while retaining
        the privileged admission outside user-defined handler arguments.
        """

        if type(handler) is not _RegisteredHandler:
            raise HandlerRegistryError("handler selection is not registry-bound")
        if type(admission) is not AgentExecutionContextAdmission:
            raise HandlerRegistryError("handler execution admission is invalid")
        if self._execution_admission_authority is None:
            raise HandlerRegistryError("handler execution admission is unavailable")
        if handler is not getattr(resolved_binding, "handler", None):
            raise HandlerRegistryError("handler selection is not pinned")
        if handler.registration != getattr(resolved_binding, "registration", None):
            raise HandlerRegistryError("handler selection is not pinned")
        try:
            admission.verify_selection(
                context,
                resolved_binding,
                self._execution_admission_authority,
            )
        except Exception:
            raise HandlerRegistryError("handler execution admission is invalid") from None
        return _BoundAgentHandler(
            selected=handler,
            context=context,
            admission=admission,
            trusted_authority=self._execution_admission_authority,
            resolved_binding=resolved_binding,
        )

    def _validate_registration(self, registration: AgentImplementationRegistration) -> None:
        self._require_not_finalized()
        if not isinstance(registration, AgentImplementationRegistration):
            raise HandlerRegistryError("local handler registration is invalid")

    def _store(
        self,
        *,
        registration: AgentImplementationRegistration,
        factory: Callable[[AgentVersion], AgentHandler[Any, Any]],
    ) -> None:
        registration_key = (registration.agent_version_id, registration.capability.key)
        if registration_key in self._registrations:
            raise HandlerRegistryError("local handler registration is duplicate")
        self._registrations[registration_key] = _HandlerRegistration(
            descriptor=registration,
            factory=factory,
        )

    def _require_finalized(self) -> None:
        if not self._finalized:
            raise HandlerRegistryError("local handler registry is not finalized")

    def _require_not_finalized(self) -> None:
        if self._finalized:
            raise HandlerRegistryError("local handler registry is finalized")
