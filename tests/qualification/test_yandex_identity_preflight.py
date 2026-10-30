from __future__ import annotations

import hashlib
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.auth import ExternalSecretReference
from spine.connectors.yandex_disk_api import (
    YandexDiskApiRequest,
    YandexDiskApiResponse,
)
from spine.connectors.yandex_identity_preflight import (
    YandexIdentityPreflightConfig,
    YandexIdentityPreflightRefused,
    YandexIdentityPreflightRunner,
    write_yandex_identity_preflight_report,
)
from spine.infrastructure.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_OAUTH_SCOPE,
    YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    YANDEX_QUALIFICATION_ROOT,
    YANDEX_QUALIFICATION_SECRET_REFERENCE,
    YandexQualificationCredentialBinding,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


REFERENCE = ExternalSecretReference(
    provider_reference=YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    secret_reference=YANDEX_QUALIFICATION_SECRET_REFERENCE,
)


def _binding() -> YandexQualificationCredentialBinding:
    return YandexQualificationCredentialBinding(
        schema_version="yandex-qualification-credential-binding:v1",
        deployment_mode="qualification",
        workspace_id=_id(1),
        environment_id=_id(2),
        reference=REFERENCE,
        client_id=YANDEX_QUALIFICATION_CLIENT_ID,
        oauth_scope=YANDEX_QUALIFICATION_OAUTH_SCOPE,
        approved_root=YANDEX_QUALIFICATION_ROOT,
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )


class RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def resolve(self, reference, *, workspace_id, environment_id):
        self.calls.append((reference, workspace_id, environment_id))
        return SecretStr("private-oauth-token")


class RecordingTransport:
    def __init__(self, response: YandexDiskApiResponse) -> None:
        self.response = response
        self.requests: list[YandexDiskApiRequest] = []

    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        self.requests.append(request)
        return self.response


def _runner(*, opt_in: bool, response: YandexDiskApiResponse):
    resolver = RecordingResolver()
    transport = RecordingTransport(response)
    runner = YandexIdentityPreflightRunner(
        config=YandexIdentityPreflightConfig(
            opt_in=opt_in,
            credential_binding=_binding(),
        ),
        credential_resolver=resolver,
        transport=transport,
    )
    return runner, resolver, transport


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_opt_in", "environment_opt_in"),
    ((False, False), (True, False), (False, True)),
)
async def test_preflight_refuses_without_double_opt_in_before_secret_or_network(
    tmp_path: Path, config_opt_in: bool, environment_opt_in: bool
) -> None:
    runner, resolver, transport = _runner(
        opt_in=config_opt_in,
        response=YandexDiskApiResponse(status_code=200),
    )

    with pytest.raises(YandexIdentityPreflightRefused):
        await runner.run(
            environment_opt_in=environment_opt_in,
            candidate_path=tmp_path / "identity-candidate",
        )

    assert resolver.calls == []
    assert transport.requests == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_preflight_calls_only_fixed_identity_endpoint_and_writes_private_candidate(
    tmp_path: Path,
) -> None:
    user_id = "123456789"
    runner, resolver, transport = _runner(
        opt_in=True,
        response=YandexDiskApiResponse(
            status_code=200,
            json_body={
                "id": user_id,
                "client_id": YANDEX_QUALIFICATION_CLIENT_ID,
                "login": "must-not-be-recorded",
            },
        ),
    )
    candidate = (tmp_path / "identity-candidate").resolve()

    report = await runner.run(
        environment_opt_in=True,
        candidate_path=candidate,
    )

    assert len(resolver.calls) == 1
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url == "https://login.yandex.ru/info"
    assert request.query == {"format": "json"}
    assert request.headers == {"Authorization": "OAuth private-oauth-token"}
    assert report.status == "ready_for_human_approval"
    assert report.client_id == YANDEX_QUALIFICATION_CLIENT_ID
    assert report.identity_fingerprint == hashlib.sha256(user_id.encode()).hexdigest()
    assert report.identity_requests == 1
    assert report.disk_requests == 0
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    assert candidate.read_text(encoding="utf-8") == (
        f"SPINE_YANDEX_QUALIFICATION_EXPECTED_USER_ID={user_id}\n"
    )
    serialized = report.model_dump_json()
    for forbidden in (user_id, "private-oauth-token", "must-not-be-recorded"):
        assert forbidden not in serialized
        assert forbidden not in repr(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "failure_code"),
    (
        (
            YandexDiskApiResponse(
                status_code=200,
                json_body={"id": "123456789", "client_id": "wrong-client"},
            ),
            "client_mismatch",
        ),
        (YandexDiskApiResponse(status_code=401), "authorization_denied"),
        (YandexDiskApiResponse(status_code=500), "provider_unavailable"),
        (
            YandexDiskApiResponse(status_code=200, json_body={"id": "123456789"}),
            "provider_contract_rejected",
        ),
        (
            YandexDiskApiResponse(
                status_code=200,
                json_body={
                    "id": "123\nINJECTED=value",
                    "client_id": YANDEX_QUALIFICATION_CLIENT_ID,
                },
            ),
            "provider_contract_rejected",
        ),
    ),
)
async def test_preflight_failure_is_sanitized_and_does_not_write_candidate(
    tmp_path: Path,
    response: YandexDiskApiResponse,
    failure_code: str,
) -> None:
    runner, _, transport = _runner(opt_in=True, response=response)
    candidate = (tmp_path / "identity-candidate").resolve()

    report = await runner.run(
        environment_opt_in=True,
        candidate_path=candidate,
    )

    assert report.status == "not_verified"
    assert report.failure_code == failure_code
    assert report.identity_fingerprint is None
    assert report.disk_requests == 0
    assert len(transport.requests) == 1
    assert not candidate.exists()


@pytest.mark.asyncio
async def test_preflight_refuses_to_replace_existing_candidate(tmp_path: Path) -> None:
    runner, _, _ = _runner(
        opt_in=True,
        response=YandexDiskApiResponse(
            status_code=200,
            json_body={
                "id": "123456789",
                "client_id": YANDEX_QUALIFICATION_CLIENT_ID,
            },
        ),
    )
    candidate = (tmp_path / "identity-candidate").resolve()
    candidate.write_text("operator-owned\n", encoding="utf-8")

    report = await runner.run(
        environment_opt_in=True,
        candidate_path=candidate,
    )

    assert report.status == "not_verified"
    assert report.failure_code == "candidate_write_denied"
    assert candidate.read_text(encoding="utf-8") == "operator-owned\n"


@pytest.mark.asyncio
async def test_preflight_report_is_private_and_contains_no_raw_identity(
    tmp_path: Path,
) -> None:
    user_id = "123456789"
    runner, _, _ = _runner(
        opt_in=True,
        response=YandexDiskApiResponse(
            status_code=200,
            json_body={"id": user_id, "client_id": YANDEX_QUALIFICATION_CLIENT_ID},
        ),
    )
    report = await runner.run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate").resolve(),
    )
    report_path = (tmp_path / "report.json").resolve()

    write_yandex_identity_preflight_report(report_path, report)

    serialized = report_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert user_id not in serialized
    assert '"disk_requests": 0' in serialized
