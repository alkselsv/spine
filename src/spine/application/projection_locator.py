"""Canonical digest profile for provider-neutral projection locators."""

from __future__ import annotations

from hashlib import sha256
import json

from spine.memory.contracts import StableLocator


def canonical_projection_locator_digest(
    *, locator_contract_version: str, locator: StableLocator
) -> str:
    """Hash one stable locator with the projection persistence profile."""

    payload = {
        "schema_version": "canonical-projection-locator:v1",
        "locator_schema_version": locator_contract_version,
        "locator": locator.model_dump(mode="json"),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["canonical_projection_locator_digest"]
