from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import pytest

from spine.auth.qualification_targets import (
    QualificationBindingDeniedError,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetBindingResolver,
)


QualificationTargetContractCase = Literal["exact", "target_mismatch", "revoked"]


@dataclass(frozen=True, slots=True)
class QualificationTargetResolverScenario:
    resolver: QualificationTargetBindingResolver
    request: QualificationTargetBindingRequest
    expected: QualificationTargetBinding | None


QualificationTargetResolverScenarioFactory = Callable[
    [QualificationTargetContractCase],
    Awaitable[QualificationTargetResolverScenario],
]


async def assert_qualification_target_binding_resolver_contract(
    factory: QualificationTargetResolverScenarioFactory,
) -> None:
    for case in ("exact", "target_mismatch", "revoked"):
        scenario = await factory(case)
        if scenario.expected is None:
            with pytest.raises(QualificationBindingDeniedError):
                await scenario.resolver.resolve(scenario.request)
        else:
            assert await scenario.resolver.resolve(scenario.request) == scenario.expected


__all__ = [
    "QualificationTargetContractCase",
    "QualificationTargetResolverScenario",
    "QualificationTargetResolverScenarioFactory",
    "assert_qualification_target_binding_resolver_contract",
]
