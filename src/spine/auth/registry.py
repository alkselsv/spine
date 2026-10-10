"""Explicit startup registry for protected-route authorization policies."""

from __future__ import annotations

import re

from pydantic import ValidationError

from spine.auth.contracts import (
    RouteAuthorizationPolicy,
    _snapshot_route_authorization_policy,
)


_ROUTE_ID = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class RoutePolicyRegistryError(ValueError):
    """Base class for static route-policy registration failures."""


class DuplicateRoutePolicyError(RoutePolicyRegistryError):
    def __init__(self) -> None:
        super().__init__("Route policy is already registered.")


class MissingRoutePolicyError(RoutePolicyRegistryError):
    def __init__(self, *, registration: bool = False) -> None:
        message = (
            "Protected route registration requires a policy."
            if registration
            else "Protected route policy is not registered."
        )
        super().__init__(message)


class InvalidRoutePolicyError(RoutePolicyRegistryError):
    def __init__(self) -> None:
        super().__init__("Route policy registration is invalid.")


class RoutePolicyRegistry:
    """Own one validated immutable policy for every protected route ID."""

    def __init__(self) -> None:
        self._policies: dict[str, RouteAuthorizationPolicy] = {}

    def register(
        self,
        route_id: str,
        policy: RouteAuthorizationPolicy | None,
    ) -> None:
        if policy is None:
            raise MissingRoutePolicyError(registration=True)
        if not isinstance(route_id, str) or _ROUTE_ID.fullmatch(route_id) is None:
            raise InvalidRoutePolicyError()
        if route_id in self._policies:
            raise DuplicateRoutePolicyError()
        try:
            if type(policy) is not RouteAuthorizationPolicy:
                raise InvalidRoutePolicyError()
            snapshot = _snapshot_route_authorization_policy(policy)
        except (AttributeError, TypeError, ValidationError, ValueError) as error:
            if isinstance(error, InvalidRoutePolicyError):
                raise
            raise InvalidRoutePolicyError() from None
        self._policies[route_id] = snapshot

    def require(self, route_id: str) -> RouteAuthorizationPolicy:
        if not isinstance(route_id, str) or _ROUTE_ID.fullmatch(route_id) is None:
            raise InvalidRoutePolicyError()
        try:
            return _snapshot_route_authorization_policy(self._policies[route_id])
        except KeyError:
            raise MissingRoutePolicyError() from None


__all__ = [
    "DuplicateRoutePolicyError",
    "InvalidRoutePolicyError",
    "MissingRoutePolicyError",
    "RoutePolicyRegistry",
    "RoutePolicyRegistryError",
]
