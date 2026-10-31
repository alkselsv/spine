"""Application port for the isolated Yandex acting-identity probe."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from spine.application.yandex_qualification_credentials import (
    YandexQualificationCredentialBinding,
)
from spine.domain.common import DefinitionModel


IdentityFailureCode = Literal[
    "authorization_denied",
    "client_mismatch",
    "credential_denied",
    "provider_contract_rejected",
    "provider_unavailable",
    "candidate_write_denied",
]


class YandexIdentityProbeError(RuntimeError):
    def __init__(
        self, code: IdentityFailureCode, *, identity_requests: Literal[0, 1]
    ) -> None:
        self.code = code
        self.identity_requests = identity_requests
        super().__init__("Yandex identity could not be verified")


class YandexIdentityObservation(DefinitionModel):
    user_id: str = Field(min_length=1, max_length=255, pattern=r"^[0-9]+$")
    client_id: str = Field(min_length=1, max_length=255)


class YandexIdentityProbe(Protocol):
    async def probe(
        self, binding: YandexQualificationCredentialBinding
    ) -> YandexIdentityObservation: ...


__all__ = [
    "IdentityFailureCode",
    "YandexIdentityObservation",
    "YandexIdentityProbe",
    "YandexIdentityProbeError",
]
