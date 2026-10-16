from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.access_policies import StalePolicyBaselineError
from spine.infrastructure.persistence.access_policies import (
    InMemoryAccessPolicyRepository,
)
from spine.domain.sources.access_policy import SourceSecurityDomainBinding


def _id(value: int) -> UUID:
    return UUID(int=value)


def _binding(
    *, binding_id: int = 1, workspace_id: int = 2
) -> SourceSecurityDomainBinding:
    return SourceSecurityDomainBinding(
        binding_id=_id(binding_id),
        workspace_id=_id(workspace_id),
        environment_id=_id(3),
        source_object_id=_id(4),
        security_domain_reference="legal/eu",
        security_domain_version="security-domain.v1",
        binding_version=1,
        created_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        policy_activation_id=_id(40),
    )


@pytest.mark.asyncio
async def test_binding_repository_is_scoped_and_immutable() -> None:
    repository = InMemoryAccessPolicyRepository()
    binding = _binding()

    assert await repository.persist_security_domain_binding(binding) == binding
    assert await repository.resolve_security_domain_binding(
        binding.binding_id,
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
    ) == binding
    assert await repository.resolve_security_domain_binding(
        binding.binding_id,
        workspace_id=_id(99),
        environment_id=binding.environment_id,
    ) is None

    with pytest.raises(StalePolicyBaselineError):
        await repository.persist_security_domain_binding(
            binding.model_copy(update={"security_domain_reference": "legal/us"})
        )
