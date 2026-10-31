from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.auth import ExternalSecretReference
from spine.connectors.yandex_disk import YandexDiskResource, YandexDiskResourcePage
from spine.connectors.yandex_disk_api import (
    YandexDiskApiResponse,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.application.yandex_disk_qualification import (
    YandexQualificationInventoryConfig,
    YandexQualificationInventoryRunner,
)
from spine.application.yandex_qualification_inventory import (
    YandexQualificationInventoryError,
)
from spine.connectors.yandex_disk_inventory import YandexQualificationInventoryReader
from spine.connectors.yandex_disk_qualification import (
    YandexDiskQualificationInventoryProbe,
)
from spine.domain.connections.provider import ProviderConnectionStatus
from spine.infrastructure.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_OAUTH_SCOPE,
    YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    YANDEX_QUALIFICATION_ROOT,
    YANDEX_QUALIFICATION_SECRET_REFERENCE,
    YandexQualificationCredentialBinding,
)
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)
from tests.connectors.test_yandex_disk_api import SCOPE, _connection


def _file(path: str, *, size: int = 4) -> YandexDiskResource:
    return YandexDiskResource(
        name=path.rsplit("/", 1)[-1],
        path=path,
        type="file",
        mime_type="text/plain",
        size=size,
        md5="8d777f385d3dfec8815d20f7496026dc",
        revision="1",
        modified=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )


def _directory(path: str) -> YandexDiskResource:
    return YandexDiskResource(name=path.rsplit("/", 1)[-1], path=path, type="dir")


class Service:
    def __init__(self, inventories: list[dict[str, tuple[YandexDiskResource, ...]]]):
        self.inventories = inventories
        self.calls: list[str] = []

    async def list_resources(self, connection, scope, *, path=None, offset, limit):
        del connection, scope
        assert path is not None
        self.calls.append(path)
        pass_index = 0 if len(self.calls) <= len(self.inventories[0]) else 1
        items = self.inventories[pass_index][path]
        return YandexDiskResourcePage(
            _embedded={
                "path": path,
                "items": items[offset : offset + limit],
                "limit": limit,
                "offset": offset,
                "total": len(items),
            }
        )


class PagingService:
    def __init__(self, items: tuple[YandexDiskResource, ...]):
        self.items = items
        self.offsets: list[int] = []

    async def list_resources(self, connection, scope, *, path=None, offset, limit):
        del connection, scope
        assert path is not None
        self.offsets.append(offset)
        return YandexDiskResourcePage(
            _embedded={
                "path": path,
                "items": self.items[offset : offset + limit],
                "limit": limit,
                "offset": offset,
                "total": len(self.items),
            }
        )


@pytest.mark.asyncio
async def test_recursive_inventory_confirms_nested_complete_snapshot() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    nested = f"{root}/nested"
    inventory = {
        root: (_directory(nested), _file(f"{root}/root.txt", size=4)),
        nested: (_file(f"{nested}/child.txt", size=7),),
    }
    service = Service([inventory, inventory])

    result = await YandexQualificationInventoryReader(
        service=service, approved_root="/Spine-Qualification-678-v1"
    ).read(_connection(), SCOPE)

    assert result.file_count == 2
    assert result.directory_count == 1
    assert result.file_bytes == 11
    assert result.provider_pages == 2
    assert service.calls == [root, nested, root, nested]


@pytest.mark.asyncio
async def test_recursive_inventory_exhausts_multi_page_offsets_in_both_passes() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    service = PagingService(
        (_file(f"{root}/one.txt"), _file(f"{root}/two.txt"))
    )

    result = await YandexQualificationInventoryReader(
        service=service,
        approved_root="/Spine-Qualification-678-v1",
        page_size=1,
    ).read(_connection(), SCOPE)

    assert result.file_count == 2
    assert result.provider_pages == 2
    assert service.offsets == [0, 1, 0, 1]


@pytest.mark.asyncio
async def test_recursive_inventory_retries_transport_with_full_jitter_ceilings() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    service = PagingService((_file(f"{root}/one.txt"),))
    original = service.list_resources
    attempts = 0

    async def transient_then_page(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            from spine.application.yandex_disk_scan import YandexDiskTransientError

            raise YandexDiskTransientError()
        return await original(*args, **kwargs)

    service.list_resources = transient_then_page  # type: ignore[method-assign]
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    await YandexQualificationInventoryReader(
        service=service,
        approved_root="/Spine-Qualification-678-v1",
        sleep=record_delay,
        jitter=lambda ceiling: ceiling / 2,
    ).read(_connection(), SCOPE)

    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_recursive_inventory_stops_before_retry_budget_is_exceeded() -> None:
    class AlwaysTransient:
        async def list_resources(self, *args, **kwargs):
            del args, kwargs
            from spine.application.yandex_disk_scan import YandexDiskTransientError

            raise YandexDiskTransientError()

    reader = YandexQualificationInventoryReader(
        service=AlwaysTransient(),
        approved_root="/Spine-Qualification-678-v1",
        max_retry_requests=1,
        sleep=lambda _: _no_sleep(),
        jitter=lambda _: 0.0,
    )

    with pytest.raises(YandexQualificationInventoryError) as raised:
        await reader.read(_connection(), SCOPE)

    assert raised.value.code == "retry_budget_exhausted"


@pytest.mark.asyncio
async def test_recursive_inventory_honors_bounded_provider_retry_after() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    service = PagingService((_file(f"{root}/one.txt"),))
    original = service.list_resources
    attempts = 0

    async def throttled_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            from spine.application.yandex_disk_scan import YandexDiskThrottleError

            raise YandexDiskThrottleError(retry_after_seconds=0.75)
        return await original(*args, **kwargs)

    service.list_resources = throttled_once  # type: ignore[method-assign]
    delays: list[float] = []
    retry_events: list[str] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)
        retry_events.append("sleep")

    async def revalidate() -> None:
        retry_events.append("revalidate")

    await YandexQualificationInventoryReader(
        service=service,
        approved_root="/Spine-Qualification-678-v1",
        sleep=record_delay,
        jitter=lambda ceiling: ceiling / 2,
        revalidate=revalidate,
    ).read(_connection(), SCOPE)

    assert delays == [0.75]
    assert retry_events == ["sleep", "revalidate"]


@pytest.mark.asyncio
async def test_recursive_inventory_rejects_retry_after_beyond_attempt_deadline() -> None:
    class Throttled:
        async def list_resources(self, *args, **kwargs):
            del args, kwargs
            from spine.application.yandex_disk_scan import YandexDiskThrottleError

            raise YandexDiskThrottleError(retry_after_seconds=0.75)

    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    reader = YandexQualificationInventoryReader(
        service=Throttled(),
        approved_root="/Spine-Qualification-678-v1",
        attempt_timeout_seconds=0.5,
        logical_timeout_seconds=1.0,
        monotonic=lambda: 0.0,
        sleep=record_delay,
        jitter=lambda _: 0.0,
    )

    with pytest.raises(YandexQualificationInventoryError) as raised:
        await reader.read(_connection(), SCOPE)

    assert raised.value.code == "deadline_exceeded"
    assert delays == []


@pytest.mark.asyncio
async def test_recursive_inventory_shares_attempt_deadline_between_confirmation_passes() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    now = 0.0

    class SlowPass:
        async def list_resources(self, connection, scope, *, path=None, offset, limit):
            nonlocal now
            del connection, scope
            assert path == root
            now += 0.6
            return YandexDiskResourcePage(
                _embedded={
                    "path": root,
                    "items": (_file(f"{root}/one.txt"),),
                    "limit": limit,
                    "offset": offset,
                    "total": 1,
                }
            )

    reader = YandexQualificationInventoryReader(
        service=SlowPass(),
        approved_root="/Spine-Qualification-678-v1",
        attempt_timeout_seconds=1.0,
        logical_timeout_seconds=2.0,
        monotonic=lambda: now,
    )

    with pytest.raises(YandexQualificationInventoryError) as raised:
        await reader.read(_connection(), SCOPE)

    assert raised.value.code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_recursive_inventory_rejects_change_between_confirmation_passes() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    first = {root: (_file(f"{root}/one.txt"),)}
    second = {root: (_file(f"{root}/two.txt"),)}

    with pytest.raises(YandexQualificationInventoryError):
        await YandexQualificationInventoryReader(
            service=Service([first, second]),
            approved_root="/Spine-Qualification-678-v1",
        ).read(_connection(), SCOPE)


@pytest.mark.asyncio
async def test_recursive_inventory_rejects_provider_item_not_directly_in_page_directory() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    invalid = {root: (_file(f"{root}/nested/child.txt"),)}

    with pytest.raises(YandexQualificationInventoryError):
        await YandexQualificationInventoryReader(
            service=Service([invalid, invalid]),
            approved_root="/Spine-Qualification-678-v1",
        ).read(_connection(), SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader_overrides", "expected_code"),
    (
        ({"max_pages": 0}, "page_limit_reached"),
        ({"max_resources": 0}, "resource_limit_reached"),
    ),
)
async def test_recursive_inventory_reports_the_exact_exhausted_bound(
    reader_overrides: dict[str, int], expected_code: str
) -> None:
    root = "disk:/Spine-Qualification-678-v1"
    inventory = {root: (_file(f"{root}/one.txt"),)}
    reader = YandexQualificationInventoryReader(
        service=Service([inventory, inventory]),
        approved_root="/Spine-Qualification-678-v1",
        **reader_overrides,
    )

    with pytest.raises(YandexQualificationInventoryError) as raised:
        await reader.read(_connection(), SCOPE)

    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_recursive_inventory_applies_page_limit_across_confirmation_passes() -> None:
    root = "disk:/Spine-Qualification-678-v1"
    inventory = {root: (_file(f"{root}/one.txt"),)}

    with pytest.raises(YandexQualificationInventoryError) as raised:
        await YandexQualificationInventoryReader(
            service=Service([inventory, inventory]),
            approved_root="/Spine-Qualification-678-v1",
            max_pages=1,
        ).read(_connection(), SCOPE)

    assert raised.value.code == "page_limit_reached"


class Resolver:
    async def resolve(self, reference, *, workspace_id, environment_id):
        del reference, workspace_id, environment_id
        return SecretStr("synthetic-token")


class Transport:
    def __init__(self, responses: list[YandexDiskApiResponse]):
        self.responses = list(responses)
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class CancelledTransport:
    async def request(self, request):
        del request
        raise asyncio.CancelledError()


def _request_governor() -> YandexQualificationRequestGovernor:
    policy = YandexQualificationRequestPolicy()
    now = 0.0

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    return YandexQualificationRequestGovernor(
        policy=policy,
        monotonic=lambda: now,
        sleep=advance,
    )


async def _ignore_delay(_: float) -> None:
    return None


def _inventory_probe(
    transport,
    *,
    sleep=_ignore_delay,
    jitter=lambda _: 0.0,
) -> YandexDiskQualificationInventoryProbe:
    return YandexDiskQualificationInventoryProbe(
        credential_resolver=Resolver(),
        transport=transport,
        request_governor=_request_governor(),
        sleep=sleep,
        jitter=jitter,
    )


def _config() -> YandexQualificationInventoryConfig:
    connection = _live_connection()
    return YandexQualificationInventoryConfig(
        opt_in=True,
        credential_binding=YandexQualificationCredentialBinding(
            schema_version="yandex-qualification-credential-binding:v1",
            deployment_mode="qualification",
            workspace_id=UUID("7ecf2a14-fa55-5025-a2f9-afbfd94c4d4d"),
            environment_id=UUID("5139ba65-49e5-51b7-a167-ff3e3a6a08bd"),
            reference=ExternalSecretReference(
                provider_reference=YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
                secret_reference=YANDEX_QUALIFICATION_SECRET_REFERENCE,
            ),
            client_id=YANDEX_QUALIFICATION_CLIENT_ID,
            oauth_scope=YANDEX_QUALIFICATION_OAUTH_SCOPE,
            approved_root=YANDEX_QUALIFICATION_ROOT,
            expires_at=datetime(2026, 11, 7, 21, tzinfo=timezone.utc),
        ),
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        service_principal_id=connection.service_principal_id,
        resource_scope_reference=SCOPE,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        security_approval_reference="issue-701-comment-6539550358",
        data_approval_reference="issue-702-comment-6539550684",
        fixture_manifest_version="spine-yandex-qualification-fixtures-v1",
        scope_version="spine-yandex-qualification-scope:v1",
        approved_manifest_digest=(
            "a3a1d452f069302a59ecc6f1e440b4080222a188b309995eb586ac185f63a11a"
        ),
    )


def _live_connection():
    return _connection(
        workspace_id=UUID("7ecf2a14-fa55-5025-a2f9-afbfd94c4d4d"),
        environment_id=UUID("5139ba65-49e5-51b7-a167-ff3e3a6a08bd"),
    )


class Repository:
    def __init__(self, connection=None):
        self.connection = connection or _live_connection()
        self.commands = []

    @asynccontextmanager
    async def admit_read(self, command, context):
        del context
        self.commands.append(command)
        yield self.connection


class RevokedOnRevalidationRepository(Repository):
    @asynccontextmanager
    async def admit_read(self, command, context):
        from spine.application.connections.provider import ProviderReadDeniedError

        del context
        self.commands.append(command)
        if len(self.commands) > 1:
            raise ProviderReadDeniedError()
        yield self.connection


class DeniedExecutionLease:
    @asynccontextmanager
    async def hold(self, scope):
        del scope
        from spine.application.yandex_qualification_execution import (
            YandexQualificationExecutionDeniedError,
        )

        raise YandexQualificationExecutionDeniedError()
        yield  # pragma: no cover


class AllowedExecutionLease:
    @asynccontextmanager
    async def hold(self, scope):
        del scope
        yield


def _page(path: str, items: tuple[YandexDiskResource, ...]) -> YandexDiskApiResponse:
    return YandexDiskApiResponse(
        status_code=200,
        wire_bytes=128,
        json_body={
            "_embedded": {
                "path": path,
                "items": [item.model_dump(mode="json") for item in items],
                "limit": 32,
                "offset": 0,
                "total": len(items),
            }
        },
    )


def _identity_response() -> YandexDiskApiResponse:
    return YandexDiskApiResponse(
        status_code=200,
        wire_bytes=64,
        json_body={
            "id": "123456789",
            "client_id": YANDEX_QUALIFICATION_CLIENT_ID,
        },
    )


def _baseline_responses() -> list[YandexDiskApiResponse]:
    root = "disk:/Spine-Qualification-678-v1"
    directories = tuple(_directory(f"{root}/dir-{index}") for index in range(4))
    pages = {root: directories + (_file(f"{root}/root.txt", size=16),)}
    for index, directory in enumerate(directories):
        pages[directory.path] = (
            _file(f"{directory.path}/file-{index}.txt", size=350),
        )
    responses: list[YandexDiskApiResponse] = []
    for _ in range(2):
        for path, items in pages.items():
            responses.extend((_identity_response(), _page(path, items)))
    return responses


def _same_aggregate_different_manifest_responses() -> list[YandexDiskApiResponse]:
    root = "disk:/Spine-Qualification-678-v1"
    directories = tuple(_directory(f"{root}/dir-{index}") for index in range(4))
    pages = {root: directories + (_file(f"{root}/renamed.txt", size=16),)}
    for index, directory in enumerate(directories):
        pages[directory.path] = (
            _file(f"{directory.path}/file-{index}.txt", size=350),
        )
    responses: list[YandexDiskApiResponse] = []
    for _ in range(2):
        for path, items in pages.items():
            responses.extend((_identity_response(), _page(path, items)))
    return responses


@pytest.mark.asyncio
async def test_inventory_runner_writes_private_candidate_and_safe_aggregate_report(
    tmp_path: Path,
) -> None:
    transport = Transport(_baseline_responses())
    candidate = (tmp_path / "manifest-candidate.json").resolve()

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(environment_opt_in=True, candidate_path=candidate)

    assert report.status == "ready_for_manifest_approval"
    assert report.workspace_id == _config().credential_binding.workspace_id
    assert report.environment_id == _config().credential_binding.environment_id
    assert report.service_principal_id == _config().service_principal_id
    assert (report.file_count, report.directory_count, report.file_bytes) == (5, 4, 1416)
    assert report.identity_requests == report.disk_requests == 10
    assert candidate.stat().st_mode & 0o777 == 0o600
    candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
    assert candidate_payload["workspace_id"] == str(report.workspace_id)
    assert candidate_payload["environment_id"] == str(report.environment_id)
    assert candidate_payload["service_principal_id"] == str(
        report.service_principal_id
    )
    serialized_report = report.model_dump_json()
    assert "root.txt" not in serialized_report
    assert "/Spine-Qualification" not in serialized_report
    assert "synthetic-token" not in serialized_report


@pytest.mark.asyncio
async def test_inventory_runner_refuses_without_double_opt_in_before_admission(
    tmp_path: Path,
) -> None:
    repository = Repository()
    transport = Transport([])

    report = await YandexQualificationInventoryRunner(
        config=_config().model_copy(update={"opt_in": False}),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=repository,
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.status == "not_verified"
    assert report.failure_code == "prerequisite_denied"
    assert repository.commands == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_inventory_runner_denies_missing_execution_lease_before_network(
    tmp_path: Path,
) -> None:
    transport = Transport([])

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=Repository(),
        execution_lease=DeniedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.status == "not_verified"
    assert report.failure_code == "prerequisite_denied"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign_connection",
    (
        _live_connection().model_copy(
            update={"workspace_id": UUID("00000000-0000-4000-8000-000000000999")}
        ),
        _live_connection().model_copy(
            update={"status": ProviderConnectionStatus.REVOKED}
        ),
        _live_connection().model_copy(
            update={
                "service_principal_id": UUID(
                    "00000000-0000-4000-8000-000000000998"
                )
            }
        ),
    ),
)
async def test_inventory_runner_denies_foreign_or_inactive_admission_before_network(
    tmp_path: Path, foreign_connection
) -> None:
    transport = Transport([])

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=Repository(foreign_connection),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.failure_code == "prerequisite_denied"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "failure_code", "identity_requests", "disk_requests"),
    (
        ([YandexDiskApiResponse(status_code=401, wire_bytes=0)], "authorization_revoked", 1, 0),
        (
            [YandexDiskApiResponse(status_code=429, wire_bytes=0)] * 3,
            "throttled",
            3,
            0,
        ),
        ([YandexDiskApiResponse(status_code=503, wire_bytes=0)], "provider_unavailable", 1, 0),
        (
            [_identity_response(), YandexDiskApiResponse(status_code=403, wire_bytes=0)],
            "permission_lost",
            1,
            1,
        ),
        (
            [_identity_response(), YandexDiskApiResponse(status_code=404, wire_bytes=0)],
            "resource_not_found",
            1,
            1,
        ),
        (
            [
                _identity_response(),
                YandexDiskApiResponse(
                    status_code=200,
                    wire_bytes=128,
                    json_body={
                        "_embedded": {
                            "path": "disk:/Spine-Qualification-678-v1",
                            "items": [
                                _file("disk:/outside/one.txt").model_dump(mode="json")
                            ],
                            "limit": 32,
                            "offset": 0,
                            "total": 1,
                        }
                    },
                ),
            ],
            "scope_denied",
            1,
            1,
        ),
        (
            [
                _identity_response(),
                YandexDiskApiResponse(status_code=200, wire_bytes=2, json_body={}),
            ],
            "contract_rejected",
            1,
            1,
        ),
    ),
)
async def test_inventory_runner_records_provider_failures_as_distinct_states(
    tmp_path: Path,
    responses: list[YandexDiskApiResponse],
    failure_code: str,
    identity_requests: int,
    disk_requests: int,
) -> None:
    transport = Transport(responses)

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.status == "not_verified"
    assert report.failure_code == failure_code
    assert report.identity_requests == identity_requests
    assert report.disk_requests == disk_requests


@pytest.mark.asyncio
async def test_inventory_runner_revalidates_authority_before_retry(
    tmp_path: Path,
) -> None:
    repository = RevokedOnRevalidationRepository()
    transport = Transport([YandexDiskApiResponse(status_code=429, wire_bytes=0)])

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(transport),
        connection_repository=repository,
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.status == "not_verified"
    assert report.failure_code == "prerequisite_denied"
    assert len(repository.commands) == 2
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_inventory_runner_records_cancellation_as_terminal_evidence(
    tmp_path: Path,
) -> None:
    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(CancelledTransport()),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(
        environment_opt_in=True,
        candidate_path=(tmp_path / "candidate.json").resolve(),
    )

    assert report.status == "not_verified"
    assert report.failure_code == "cancelled"
    assert report.manifest_candidate_written is False


@pytest.mark.asyncio
async def test_inventory_runner_rejects_unaccounted_or_excess_wire_bytes(
    tmp_path: Path,
) -> None:
    for response, failure_code in (
        (YandexDiskApiResponse(status_code=200, json_body={}), "contract_rejected"),
        (
            YandexDiskApiResponse(
                status_code=200,
                json_body={},
                wire_bytes=2 * 1024 * 1024 + 1,
            ),
            "response_byte_limit_reached",
        ),
    ):
        report = await YandexQualificationInventoryRunner(
            config=_config(),
            expected_user_id=SecretStr("123456789"),
            inventory_probe=_inventory_probe(Transport([response])),
            connection_repository=Repository(),
            execution_lease=AllowedExecutionLease(),
            artifact_writer=FilePrivateQualificationArtifactWriter(),
            context=object(),  # type: ignore[arg-type]
        ).run(
            environment_opt_in=True,
            candidate_path=(tmp_path / f"{failure_code}.json").resolve(),
        )

        assert report.failure_code == failure_code


@pytest.mark.asyncio
async def test_inventory_runner_reports_manifest_mismatch_without_candidate(
    tmp_path: Path,
) -> None:
    root = "disk:/Spine-Qualification-678-v1"
    responses = []
    for _ in range(2):
        responses.extend((_identity_response(), _page(root, (_file(f"{root}/one.txt"),))))
    candidate = (tmp_path / "candidate.json").resolve()

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(Transport(responses)),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(environment_opt_in=True, candidate_path=candidate)

    assert report.failure_code == "manifest_mismatch"
    assert not candidate.exists()


@pytest.mark.asyncio
async def test_inventory_runner_rejects_different_manifest_with_same_aggregates(
    tmp_path: Path,
) -> None:
    candidate = (tmp_path / "candidate.json").resolve()

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(
            Transport(_same_aggregate_different_manifest_responses())
        ),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(environment_opt_in=True, candidate_path=candidate)

    assert report.status == "not_verified"
    assert report.failure_code == "manifest_mismatch"
    assert not candidate.exists()


@pytest.mark.asyncio
async def test_inventory_runner_does_not_replace_existing_manifest_candidate(
    tmp_path: Path,
) -> None:
    candidate = (tmp_path / "candidate.json").resolve()
    candidate.write_text("operator-owned\n", encoding="utf-8")

    report = await YandexQualificationInventoryRunner(
        config=_config(),
        expected_user_id=SecretStr("123456789"),
        inventory_probe=_inventory_probe(Transport(_baseline_responses())),
        connection_repository=Repository(),
        execution_lease=AllowedExecutionLease(),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=object(),  # type: ignore[arg-type]
    ).run(environment_opt_in=True, candidate_path=candidate)

    assert report.failure_code == "candidate_write_denied"
    assert candidate.read_text(encoding="utf-8") == "operator-owned\n"


async def _no_sleep() -> None:
    return None
