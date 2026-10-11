"""Canonical idempotency identity for one validated local invocation."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from spine.agents.runtime.contracts import AgentExecutionContext
from spine.application.persistence import (
    CommandDigest,
    PersistenceOperation,
    digest_command,
)
from spine.domain.agents import AgentInvocation


AGENT_INVOCATION_OPERATION: Final = PersistenceOperation("agent.invoke")
AGENT_INVOCATION_OPERATION_SCHEMA_VERSION: Final = 1
AGENT_RESULT_SCHEMA_VERSION: Final = 1


def digest_agent_invocation(
    *,
    context: AgentExecutionContext,
    invocation: AgentInvocation,
    validated_input: BaseModel,
) -> CommandDigest:
    """Digest every semantic field that may change an invocation's result.

    Workspace and Environment are also enforced by the canonical persistence
    scope. Keeping them in the digest makes the complete effective identity
    explicit and prevents an incorrectly scoped adapter from accepting a
    cross-tenant replay.
    """

    return digest_command(
        operation=AGENT_INVOCATION_OPERATION,
        operation_schema_version=AGENT_INVOCATION_OPERATION_SCHEMA_VERSION,
        payload={
            "workspace_id": context.workspace_id,
            "environment_id": context.environment_id,
            "capability": {
                "id": context.capability.id,
                "key": context.capability.key,
                "input_schema": {
                    "name": context.capability.input_schema.name,
                    "version": context.capability.input_schema.version,
                },
                "output_schema": {
                    "name": context.capability.output_schema.name,
                    "version": context.capability.output_schema.version,
                },
            },
            "agent_version": {
                "id": context.agent_version.id,
                "agent_id": context.agent_version.agent_id,
                "version": context.agent_version.version,
            },
            "run": {
                "work_item_id": context.work_item_id,
                "workflow_run_id": context.workflow_run_id,
                "step_run_id": context.step_run_id,
                "agent_run_id": context.agent_run_id,
            },
            "authority": {
                "acting_on_behalf_of": {
                    "kind": invocation.acting_on_behalf_of.kind,
                    "id": invocation.acting_on_behalf_of.id,
                },
                "acting_subject_id": context.trusted_identity.acting_subject_id,
                "service_principal_id": context.trusted_identity.service_principal_id,
                "authorization_generation": (
                    context.trusted_identity.authorization_generation
                ),
            },
            "input_artifact_ids": invocation.input_artifact_ids,
            "validated_input": validated_input.model_dump(mode="json"),
            "constraints": invocation.constraints,
            "context_request": invocation.context_request,
            "context_profile": context.context_profile,
            "permitted_tool_capabilities": context.permitted_tool_capabilities,
            "deadline": context.deadline,
        },
    )


__all__ = [
    "AGENT_INVOCATION_OPERATION",
    "AGENT_INVOCATION_OPERATION_SCHEMA_VERSION",
    "AGENT_RESULT_SCHEMA_VERSION",
    "digest_agent_invocation",
]
