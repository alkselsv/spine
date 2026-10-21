from types import SimpleNamespace

import pytest

from spine.application.qa_run_history import PersistedQARunHistoryService


@pytest.mark.asyncio
async def test_attempt_contexts_are_materialized_in_attempt_order() -> None:
    first_reference = SimpleNamespace(
        bundle_id="first-id",
        bundle_version=1,
        bundle_digest="first-digest",
    )
    second_reference = SimpleNamespace(
        bundle_id="second-id",
        bundle_version=2,
        bundle_digest="second-digest",
    )
    resolved = [object(), object()]

    class ContextBundles:
        async def resolve(self, *, bundle_id, bundle_version, bundle_digest):
            index = 0 if bundle_id == "first-id" else 1
            assert bundle_version == index + 1
            assert bundle_digest == ("first-digest" if index == 0 else "second-digest")
            return resolved[index]

    service = PersistedQARunHistoryService(uow_factory=object())  # type: ignore[arg-type]
    uow = SimpleNamespace(context_bundles=ContextBundles())
    attempts = (
        SimpleNamespace(context_bundle_ref=first_reference),
        SimpleNamespace(context_bundle_ref=second_reference),
    )

    contexts = await service._contexts(uow, attempts)  # type: ignore[arg-type]

    assert contexts == tuple(resolved)
