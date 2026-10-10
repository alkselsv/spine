from __future__ import annotations

import asyncio

import pytest

from spine.auth import AuthorizationResolution
from spine.infrastructure.auth import RequestAgentAuthorizationVerifier

from tests.infrastructure.auth.test_request_authorization_composition import (
    composition,
    credentials,
    diagnostic_context,
    scope,
)


@pytest.mark.asyncio
async def test_agent_verifier_rechecks_real_issue72_request_lifecycle() -> None:
    composer, _, request_boundary, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request_context = authorized.request_context
        resolution = AuthorizationResolution(
            acting_subject_id=request_context.acting_subject_id,
            scope=request_context.scope,
            roles=request_context.roles,
            authorization_generation=request_context.authorization_generation,
            authentication_configuration_version=(
                request_context.authentication_configuration_version
            ),
        )
        verifier = RequestAgentAuthorizationVerifier(
            boundary=request_boundary,
            authorized=authorized,
        )

        verifier.verify(request_context, resolution)

        async def use_in_child_task() -> None:
            verifier.verify(request_context, resolution)

        with pytest.raises(ValueError, match="authorization context is invalid"):
            await asyncio.create_task(use_in_child_task())

    with pytest.raises(ValueError, match="authorization context is invalid"):
        verifier.verify(request_context, resolution)
