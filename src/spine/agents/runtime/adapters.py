"""Explicit adapters for framework-independent local agent handlers."""

from __future__ import annotations

import inspect
import types
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Generic, TypeVar, Union, get_args, get_origin, get_type_hints

from pydantic import TypeAdapter, ValidationError
from pydantic.errors import PydanticErrorMixin

from spine.agents.runtime.contracts import AgentExecutionContext, AgentRequest, AgentResponse
from spine.agents.runtime.protocol import AgentHandler
from spine.domain.agents.contracts import AgentResult

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


def adapt_async_function(
    function: Callable[..., Awaitable[Any]],
    *,
    input_type: Any | None = None,
    output_type: Any | None = None,
) -> AgentHandler[InputT, OutputT]:
    """Adapt one explicitly typed coroutine to the local handler protocol.

    The callable must accept ``AgentRequest[InputT]`` and
    ``AgentExecutionContext`` as its two positional parameters. Its return
    annotation identifies ``OutputT`` and may be either a raw output or an
    ``AgentResponse[OutputT]`` (or a union of those two forms).

    ``input_type`` and ``output_type`` let an explicit composition root pin the
    contract when it has authoritative metadata beyond the function's
    annotations. They are checked against the annotations; they do not alter
    the request or response contracts.
    """

    contract = _inspect_function(function)
    declared_input, declared_output = contract

    if input_type is not None and declared_input != input_type:
        raise TypeError("async handler input contract is incompatible")
    if output_type is not None and declared_output != output_type:
        raise TypeError("async handler output contract is incompatible")

    return _AsyncFunctionHandler(
        function=function,
        input_type=input_type if input_type is not None else declared_input,
        output_type=output_type if output_type is not None else declared_output,
    )


class _AsyncFunctionHandler(Generic[InputT, OutputT]):
    """Stateless structural handler around one validated coroutine."""

    def __init__(
        self,
        *,
        function: Callable[..., Awaitable[Any]],
        input_type: Any,
        output_type: Any,
    ) -> None:
        self._function = function
        self._input_type = input_type
        self._output_type = output_type
        self._input_adapter = _type_adapter(input_type)
        self._output_adapter = _type_adapter(output_type)

    async def invoke(
        self,
        request: AgentRequest[InputT],
        context: AgentExecutionContext,
    ) -> AgentResponse[OutputT]:
        """Invoke without retaining either call's request or execution context."""

        if not isinstance(request, AgentRequest):
            raise TypeError("async handler requires an AgentRequest")
        if not isinstance(context, AgentExecutionContext):
            raise TypeError("async handler requires an AgentExecutionContext")

        try:
            self._validate_input(request.input)
        except (TypeError, ValidationError):
            raise TypeError("async handler received an incompatible request input") from None

        result = await self._function(request, context)
        if isinstance(result, AgentResponse):
            return self._normalize_response(result, request)
        return self._normalize_raw_output(result, request)

    def _validate_input(self, value: Any) -> None:
        self._input_adapter.validate_python(value, strict=True)

    def _normalize_response(
        self,
        response: AgentResponse[Any],
        request: AgentRequest[InputT],
    ) -> AgentResponse[OutputT]:
        if response.schema_identity != request.schema_identity:
            raise ValueError("agent response schema identity does not match request")
        if response.output is not None:
            try:
                self._output_adapter.validate_python(response.output, strict=True)
            except (TypeError, ValidationError):
                raise TypeError("async handler returned an incompatible response output") from None
        return response

    def _normalize_raw_output(
        self,
        output: Any,
        request: AgentRequest[InputT],
    ) -> AgentResponse[OutputT]:
        try:
            validated_output = self._output_adapter.validate_python(output, strict=True)
        except (TypeError, ValidationError):
            raise TypeError("async handler returned an incompatible output") from None
        return AgentResponse(
            schema_identity=request.schema_identity,
            output=validated_output,
        )


def _inspect_function(function: Callable[..., Awaitable[Any]]) -> tuple[Any, Any]:
    if not inspect.iscoroutinefunction(function):
        raise TypeError("async handler requires a coroutine function")

    try:
        signature = inspect.signature(function)
        hints = get_type_hints(function)
    except (TypeError, ValueError, NameError):
        raise TypeError("async handler annotations could not be inspected") from None

    parameters = tuple(signature.parameters.values())
    if len(parameters) != 2 or any(
        parameter.kind not in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ):
        raise TypeError("async handler must accept request and context positionally")

    request_annotation = hints.get(parameters[0].name)
    context_annotation = hints.get(parameters[1].name)
    if request_annotation is None or context_annotation is None:
        raise TypeError("async handler request and context annotations are required")
    if context_annotation is not AgentExecutionContext:
        raise TypeError("async handler context contract is incompatible")

    input_type = _generic_argument(request_annotation, AgentRequest)
    if input_type is None:
        raise TypeError("async handler request annotation must be AgentRequest[InputT]")

    return_annotation = hints.get("return")
    if return_annotation is None:
        raise TypeError("async handler return annotation is required")
    output_type = _output_type_from_return(return_annotation)
    return input_type, output_type


def _generic_argument(annotation: Any, expected_origin: type[Any]) -> Any | None:
    if annotation is expected_origin:
        return None
    metadata = getattr(annotation, "__pydantic_generic_metadata__", None)
    if not metadata or metadata.get("origin") is not expected_origin:
        return None
    arguments = metadata.get("args", ())
    if len(arguments) != 1 or arguments[0] in (Any, object, type(None)):
        return None
    return arguments[0]


def _output_type_from_return(annotation: Any) -> Any:
    annotation = _unwrap_annotated(annotation)
    branches = get_args(annotation) if _is_union(annotation) else (annotation,)
    if not branches or type(None) in branches:
        raise TypeError("async handler return shape is unsupported")

    output_types: list[Any] = []
    response_types: list[Any] = []
    for branch in branches:
        branch = _unwrap_annotated(branch)
        metadata = getattr(branch, "__pydantic_generic_metadata__", None)
        if metadata and metadata.get("origin") is AgentResponse:
            response_type = _generic_argument(branch, AgentResponse)
            if response_type is None:
                raise TypeError("async handler return shape is unsupported")
            response_types.append(response_type)
            continue
        response_type = _generic_argument(branch, AgentResponse)
        if response_type is not None:
            response_types.append(response_type)
            continue
        if branch in (AgentResponse, AgentResult, Any, object):
            raise TypeError("async handler return shape is unsupported")
        output_types.append(branch)

    if len(response_types) > 1 or len(output_types) > 1:
        raise TypeError("async handler return shape is unsupported")
    all_types = response_types + output_types
    if not all_types or any(value != all_types[0] for value in all_types[1:]):
        raise TypeError("async handler response and output types are incompatible")
    return all_types[0]


def _type_adapter(annotation: Any) -> TypeAdapter[Any]:
    try:
        return TypeAdapter(annotation)
    except (PydanticErrorMixin, TypeError):
        raise TypeError("async handler type contract is unsupported") from None


def _unwrap_annotated(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def _is_union(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, types.UnionType)
