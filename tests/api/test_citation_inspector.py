from uuid import UUID

import httpx
import pytest

from spine.api.citation_inspector import install_citation_inspector_routes
from spine.domain.disclosure import (
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
)
from tests.api.test_authorization import ENVIRONMENT_ID, WORKSPACE_ID, authorized_app


class UnavailableCitationInspector:
    async def inspect(self, **kwargs):
        del kwargs
        return CitationInspectionResult(
            status=CitationInspectionStatus.UNAVAILABLE,
            reason=CitationInspectionReason.CITATION_UNAVAILABLE,
        )


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }


@pytest.mark.asyncio
async def test_citation_inspector_uses_safe_error_for_unavailable_content() -> None:
    application = authorized_app([])
    install_citation_inspector_routes(
        application,
        service=UnavailableCitationInspector(),  # type: ignore[arg-type]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/qa/citations/{UUID(int=999)}", headers=headers()
        )

    assert response.status_code == 404
    assert response.json()["code"] == "spine.qa.citations.unavailable"
    assert "999" not in response.text
    assert "excerpt" not in response.text
