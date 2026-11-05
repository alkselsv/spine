import pytest

from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.domain.sources import CurrentSourceHead, RevisionKind
from tests.application.test_tombstone_authorization import NOW, _authorization, _id
from tests.domain.sources.test_canonical_tombstone_v3 import acceptance


def test_v3_reader_returns_exact_authorization_and_current_head() -> None:
    authorization = _authorization(
        authorization_id=_id(8),
        workspace_id=_id(4),
        environment_id=_id(5),
        source_object_id=_id(2),
        tombstone_revision_id=_id(3),
        command_digest="a" * 64,
        transition_id=_id(9),
    )
    current = CurrentSourceHead(
        source_object_id=_id(2),
        workspace_id=_id(4),
        environment_id=_id(5),
        current_revision_id=_id(3),
        current_revision_kind=RevisionKind.TOMBSTONE,
        acceptance_id=_id(1),
        accepted_generation=2,
        cas_version=6,
        changed_at=NOW,
    )

    result = CanonicalAcceptanceReadResult(
        acceptance=acceptance(),
        processing_admission=None,
        current=current,
        tombstone_authorization=authorization,
    )

    assert result.is_current is True
    assert result.tombstone_authorization == authorization


def test_v3_reader_rejects_missing_or_substituted_authorization() -> None:
    with pytest.raises(ValueError, match="link is incomplete"):
        CanonicalAcceptanceReadResult(acceptance(), None, None)

    authorization = _authorization(
        authorization_id=_id(8),
        workspace_id=_id(4),
        environment_id=_id(5),
        source_object_id=_id(2),
        tombstone_revision_id=_id(3),
        command_digest="a" * 64,
        transition_id=_id(9),
    )
    with pytest.raises(ValueError, match="scope is inconsistent"):
        CanonicalAcceptanceReadResult(
            acceptance(),
            None,
            None,
            authorization.model_copy(update={"transition_id": _id(99)}),
        )
