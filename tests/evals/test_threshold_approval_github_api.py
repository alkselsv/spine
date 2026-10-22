from __future__ import annotations

import httpx
import pytest

from spine.evaluations.release_gates.threshold_approval_verifier import (
    GitHubRestThresholdDecisionSource,
)


@pytest.mark.asyncio
async def test_github_source_uses_authenticated_api_and_maps_records() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/issues/17"):
            return httpx.Response(200, json={"user": {"id": 167317615}})
        if request.url.path.endswith("/issues/557/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "user": {"id": 167317615},
                        "body": "decision record",
                        "created_at": "2026-10-19T00:00:00Z",
                        "updated_at": "2026-10-19T00:00:00Z",
                        "html_url": "https://github.com/alkselsv/spine/issues/557#issuecomment-42",
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[{"event": "commented", "issue_comment_id": 42}],
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(respond),
    ) as client:
        source = GitHubRestThresholdDecisionSource(token="synthetic-token", client=client)
        assert await source.issue_author_id(17) == 167317615
        comments = await source.comments(557)
        events = await source.timeline(557)
    assert comments[0].comment_id == 42
    assert comments[0].updated_at == comments[0].created_at
    assert events[0].comment_id == 42
    assert all(
        request.headers["Authorization"] == "Bearer synthetic-token"
        for request in requests
    )


@pytest.mark.asyncio
async def test_github_source_rejects_pagination_outside_api_host() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={"Link": '<https://attacker.invalid/page>; rel="next"'},
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(respond),
    ) as client:
        source = GitHubRestThresholdDecisionSource(token="synthetic-token", client=client)
        with pytest.raises(ValueError, match="authenticated API host"):
            await source.comments(557)
