from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)


WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000101")
ENVIRONMENT_ID = UUID("00000000-0000-0000-0000-000000000102")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000103")
CREATED_AT = datetime(2026, 10, 14, tzinfo=timezone.utc)


def profile(**overrides: object) -> ContextProfileVersion:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "profile_version": 1,
        "retrieval_strategy": RetrievalStrategy.HYBRID,
        "max_results": 20,
        "max_tokens": 4000,
        "max_relation_hops": 2,
        "freshness_max_age_seconds": 3600,
        "completeness_requirement": CompletenessRequirement.BEST_EFFORT,
        "partial_answer_fallback": PartialAnswerFallback.DISALLOWED,
        "server_approved": True,
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return ContextProfileVersion.create(**values)


def test_context_profile_version_digest_is_addressed() -> None:
    subject = profile()

    assert subject.profile_digest == ContextProfileVersion.calculate_digest(subject)


def test_context_profile_version_is_frozen() -> None:
    subject = profile()

    with pytest.raises(ValidationError):
        subject.profile_version = 2  # type: ignore[misc]


def test_context_profile_version_rejects_invalid_retrieval_bounds() -> None:
    with pytest.raises(ValidationError):
        profile(max_results=0)
    with pytest.raises(ValidationError):
        profile(max_relation_hops=9)
    with pytest.raises(ValidationError):
        profile(freshness_max_age_seconds=-1)


def test_context_profile_version_requires_explicit_server_approval() -> None:
    with pytest.raises(ValidationError, match="server-approved"):
        profile(server_approved=False)


def test_context_profile_version_requires_predecessor_for_replacement() -> None:
    with pytest.raises(ValidationError, match="predecessor"):
        profile(profile_version=2)


def test_context_profile_version_rejects_digest_for_different_configuration() -> None:
    subject = profile()
    with pytest.raises(ValidationError, match="digest"):
        ContextProfileVersion.model_validate(
            {
                **subject.model_dump(mode="python"),
                "max_results": 21,
            }
        )


def test_context_profile_version_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        profile(untrusted_content_permission=True)
