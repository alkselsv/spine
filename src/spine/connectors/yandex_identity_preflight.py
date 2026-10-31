"""Concrete Yandex ID adapter for the qualification identity-probe port."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from spine.application.yandex_qualification_credentials import (
    YandexQualificationCredentialBinding,
)
from spine.application.yandex_qualification_identity import (
    YandexIdentityObservation,
    YandexIdentityProbeError,
)
from spine.connectors.yandex_disk import YandexDiskCredentialResolver
from spine.connectors.yandex_disk_api import (
    YandexDiskApiRequest,
    YandexDiskApiTransport,
)


_IDENTITY_URL = "https://login.yandex.ru/info"
_MAXIMUM_IDENTITY_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class YandexIdentityProbeAdapter:
    credential_resolver: YandexDiskCredentialResolver
    transport: YandexDiskApiTransport

    async def probe(
        self, binding: YandexQualificationCredentialBinding
    ) -> YandexIdentityObservation:
        try:
            secret = await self.credential_resolver.resolve(
                binding.reference,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
            )
            token = secret.get_secret_value()
            if not token:
                raise YandexIdentityProbeError(
                    "credential_denied", identity_requests=0
                )
        except asyncio.CancelledError:
            raise
        except YandexIdentityProbeError:
            raise
        except Exception:
            raise YandexIdentityProbeError(
                "credential_denied", identity_requests=0
            ) from None

        try:
            response = await self.transport.request(
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
            raise YandexIdentityProbeError(
                "provider_unavailable", identity_requests=1
            ) from None

        if response.status_code in (401, 403):
            raise YandexIdentityProbeError(
                "authorization_denied", identity_requests=1
            )
        if response.status_code >= 500:
            raise YandexIdentityProbeError(
                "provider_unavailable", identity_requests=1
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise YandexIdentityProbeError(
                "provider_contract_rejected", identity_requests=1
            )
        payload = response.json_body
        if payload is None:
            raise YandexIdentityProbeError(
                "provider_contract_rejected", identity_requests=1
            )
        user_id = payload.get("id")
        client_id = payload.get("client_id")
        if (
            not isinstance(user_id, str)
            or not user_id.isascii()
            or not user_id.isdigit()
            or len(user_id) > 255
            or not isinstance(client_id, str)
            or not client_id
        ):
            raise YandexIdentityProbeError(
                "provider_contract_rejected", identity_requests=1
            )
        if client_id != binding.client_id:
            raise YandexIdentityProbeError("client_mismatch", identity_requests=1)
        return YandexIdentityObservation(user_id=user_id, client_id=client_id)


__all__ = ["YandexIdentityProbeAdapter"]
