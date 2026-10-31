"""Application use case for the isolated Yandex identity qualification.

This module cannot issue Disk API requests.  It verifies the OAuth token against
Yandex ID, records only sanitized evidence, and writes the discovered user ID to
an operator-local candidate file for a separate human approval step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, model_validator

from spine.application.private_qualification_artifacts import (
    PrivateQualificationArtifactWriter,
)
from spine.connectors.yandex_disk import YandexDiskCredentialResolver
from spine.connectors.yandex_disk_api import (
    YandexDiskApiRequest,
    YandexDiskApiTransport,
)
from spine.domain.common import DefinitionModel
from spine.application.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YandexQualificationCredentialBinding,
)


_IDENTITY_URL = "https://login.yandex.ru/info"
_MAXIMUM_IDENTITY_RESPONSE_BYTES = 64 * 1024
_EXPECTED_USER_ID_KEY = "SPINE_YANDEX_QUALIFICATION_EXPECTED_USER_ID"

IdentityFailureCode = Literal[
    "authorization_denied",
    "client_mismatch",
    "credential_denied",
    "provider_contract_rejected",
    "provider_unavailable",
    "candidate_write_denied",
]


class YandexIdentityPreflightRefused(RuntimeError):
    """The identity probe was not explicitly enabled at both gates."""


class YandexIdentityPreflightConfig(DefinitionModel):
    """Non-secret authority required to perform the identity-only probe."""

    schema_version: Literal["yandex-identity-preflight:v1"] = (
        "yandex-identity-preflight:v1"
    )
    opt_in: StrictBool
    credential_binding: YandexQualificationCredentialBinding


class YandexIdentityPreflightReport(DefinitionModel):
    """Sanitized evidence safe to attach to qualification records."""

    schema_version: Literal["yandex-identity-preflight-report:v1"] = (
        "yandex-identity-preflight-report:v1"
    )
    status: Literal["ready_for_human_approval", "not_verified"]
    provider: Literal["yandex"] = "yandex"
    workspace_id: UUID
    environment_id: UUID
    client_id: Literal[YANDEX_QUALIFICATION_CLIENT_ID]
    identity_fingerprint: StrictStr | None = Field(
        default=None, min_length=64, max_length=64
    )
    identity_requests: Literal[0, 1]
    disk_requests: Literal[0] = 0
    failure_code: IdentityFailureCode | None = None
    credentials_recorded: Literal[False] = False
    provider_payloads_recorded: Literal[False] = False
    identity_candidate_written: StrictBool
    raw_identity_in_report: Literal[False] = False
    raw_errors_recorded: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome(self) -> "YandexIdentityPreflightReport":
        passed = self.status == "ready_for_human_approval"
        if passed != (self.identity_fingerprint is not None):
            raise ValueError(
                "identity fingerprint must exist only for a successful probe"
            )
        if passed != (self.failure_code is None):
            raise ValueError("failure code must exist only for a failed probe")
        if passed != self.identity_candidate_written:
            raise ValueError("candidate status must match the preflight outcome")
        return self


class YandexIdentityPreflightRunner:
    """Run exactly one identity request and no Disk requests."""

    def __init__(
        self,
        *,
        config: YandexIdentityPreflightConfig,
        credential_resolver: YandexDiskCredentialResolver,
        transport: YandexDiskApiTransport,
        artifact_writer: PrivateQualificationArtifactWriter,
    ) -> None:
        self.config = config
        self._resolver = credential_resolver
        self._transport = transport
        self._artifact_writer = artifact_writer

    async def run(
        self,
        *,
        environment_opt_in: bool,
        candidate_path: Path,
    ) -> YandexIdentityPreflightReport:
        if not self.config.opt_in or environment_opt_in is not True:
            raise YandexIdentityPreflightRefused(
                "Yandex identity preflight requires both explicit opt-ins"
            )

        binding = self.config.credential_binding
        try:
            secret = await self._resolver.resolve(
                binding.reference,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
            )
            token = secret.get_secret_value()
            if not token:
                return self._failed("credential_denied", identity_requests=0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._failed("credential_denied", identity_requests=0)

        try:
            response = await self._transport.request(
                YandexDiskApiRequest(
                    method="GET",
                    url=_IDENTITY_URL,
                    headers={"Authorization": f"OAuth {token}"},
                    query={"format": "json"},
                    timeout_seconds=10.0,
                    max_response_bytes=_MAXIMUM_IDENTITY_RESPONSE_BYTES,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._failed("provider_unavailable", identity_requests=1)

        if response.status_code in (401, 403):
            return self._failed("authorization_denied", identity_requests=1)
        if response.status_code >= 500:
            return self._failed("provider_unavailable", identity_requests=1)
        if response.status_code < 200 or response.status_code >= 300:
            return self._failed("provider_contract_rejected", identity_requests=1)

        payload = response.json_body
        if payload is None:
            return self._failed("provider_contract_rejected", identity_requests=1)
        user_id = payload.get("id")
        client_id = payload.get("client_id")
        if (
            not isinstance(user_id, str)
            or not user_id.isascii()
            or not user_id.isdigit()
            or len(user_id) > 255
        ):
            return self._failed("provider_contract_rejected", identity_requests=1)
        if not isinstance(client_id, str) or not client_id:
            return self._failed("provider_contract_rejected", identity_requests=1)
        if client_id != binding.client_id:
            return self._failed("client_mismatch", identity_requests=1)

        try:
            _write_identity_candidate(
                candidate_path,
                user_id,
                artifact_writer=self._artifact_writer,
            )
        except OSError:
            return self._failed("candidate_write_denied", identity_requests=1)

        return YandexIdentityPreflightReport(
            status="ready_for_human_approval",
            workspace_id=binding.workspace_id,
            environment_id=binding.environment_id,
            client_id=binding.client_id,
            identity_fingerprint=hashlib.sha256(user_id.encode("utf-8")).hexdigest(),
            identity_requests=1,
            identity_candidate_written=True,
        )

    def _failed(
        self, failure_code: IdentityFailureCode, *, identity_requests: Literal[0, 1]
    ) -> YandexIdentityPreflightReport:
        return YandexIdentityPreflightReport.model_validate(
            {
                "status": "not_verified",
                "workspace_id": self.config.credential_binding.workspace_id,
                "environment_id": self.config.credential_binding.environment_id,
                "client_id": self.config.credential_binding.client_id,
                "failure_code": failure_code,
                "identity_requests": identity_requests,
                "identity_candidate_written": False,
            }
        )


def _write_identity_candidate(
    path: Path,
    user_id: str,
    *,
    artifact_writer: PrivateQualificationArtifactWriter,
) -> None:
    artifact_writer.create(
        path,
        f"{_EXPECTED_USER_ID_KEY}={user_id}\n".encode("utf-8"),
    )


def write_yandex_identity_preflight_report(
    path: Path,
    report: YandexIdentityPreflightReport,
    *,
    artifact_writer: PrivateQualificationArtifactWriter,
) -> None:
    """Create a private metadata-only report without replacing operator files."""

    content = (
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    artifact_writer.create(path, content)


__all__ = [
    "YandexIdentityPreflightConfig",
    "YandexIdentityPreflightRefused",
    "YandexIdentityPreflightReport",
    "YandexIdentityPreflightRunner",
    "write_yandex_identity_preflight_report",
]
