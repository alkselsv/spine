import ast
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import (
    Bitrix24ConfirmedUninstall,
    Bitrix24OAuthSafeCode,
    CallbackUnsupportedDropped,
    GrantUncertain,
    RefreshedTokenPair,
    SecretApplicationCredential,
    SecretTokenPair,
    safe_oauth_outcome_code,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def test_secret_material_never_appears_in_repr_or_safe_outcomes() -> None:
    pair = SecretTokenPair("access-secret", "refresh-secret")
    credential = SecretApplicationCredential("client-id", "client-secret")
    refreshed = RefreshedTokenPair(tokens=pair)

    rendered = " ".join((repr(pair), repr(credential), repr(refreshed)))

    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "client-secret" not in rendered
    assert (
        safe_oauth_outcome_code(GrantUncertain(binding_id=_id(1), attempt_id=_id(2)))
        is Bitrix24OAuthSafeCode.GRANT_UNCERTAIN
    )
    assert (
        safe_oauth_outcome_code(CallbackUnsupportedDropped(event_kind="ONAPPUSERREADY"))
        is Bitrix24OAuthSafeCode.CALLBACK_UNSUPPORTED_DROPPED
    )


def test_default_bitrix24_oauth_modules_have_no_network_client() -> None:
    root = Path(__file__).parents[2] / "src" / "spine"
    modules = (
        root / "application" / "connections" / "bitrix24_oauth.py",
        root / "infrastructure" / "connections" / "bitrix24_oauth.py",
        root / "infrastructure" / "connections" / "postgresql_bitrix24_oauth.py",
    )
    forbidden = {"httpx", "requests", "socket", "urllib", "aiohttp"}

    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert imports.isdisjoint(forbidden)


def test_offline_oauth_modules_do_not_register_event_bind_subscriptions() -> None:
    root = Path(__file__).parents[2] / "src" / "spine"
    modules = (
        root / "application" / "connections" / "bitrix24_oauth.py",
        root / "infrastructure" / "connections" / "bitrix24_oauth.py",
        root / "infrastructure" / "connections" / "postgresql_bitrix24_oauth.py",
    )

    assert all("event.bind" not in module.read_text(encoding="utf-8") for module in modules)


def test_unknown_outcome_cannot_become_unbounded_diagnostic_text() -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        safe_oauth_outcome_code(RuntimeError("provider secret text"))


def test_uninstall_diagnostics_use_only_closed_safe_codes() -> None:
    assert safe_oauth_outcome_code(Bitrix24ConfirmedUninstall.DENIED) is (
        Bitrix24OAuthSafeCode.UNINSTALL_DENIED
    )
    assert safe_oauth_outcome_code(Bitrix24ConfirmedUninstall.IDENTITY_MISMATCH) is (
        Bitrix24OAuthSafeCode.IDENTITY_MISMATCH
    )
