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
from spine.application.yandex_qualification_identity import (
    IdentityFailureCode,
    YandexIdentityProbe,
    YandexIdentityProbeError,
)
from spine.domain.common import DefinitionModel
from spine.application.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YandexQualificationCredentialBinding,
)


_EXPECTED_USER_ID_KEY = "SPINE_YANDEX_QUALIFICATION_EXPECTED_USER_ID"


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
        identity_probe: YandexIdentityProbe,
        artifact_writer: PrivateQualificationArtifactWriter,
    ) -> None:
        self.config = config
        self._identity_probe = identity_probe
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
            observation = await self._identity_probe.probe(binding)
        except asyncio.CancelledError:
            raise
        except YandexIdentityProbeError as exc:
            return self._failed(exc.code, identity_requests=exc.identity_requests)
        user_id = observation.user_id

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
