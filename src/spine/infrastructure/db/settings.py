"""Typed database configuration without import-time side effects."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

SUPPORTED_POSTGRESQL_MAJOR = 17
TESTCONTAINERS_POSTGRES_IMAGE = "postgres:17.6-bookworm"
POSTGRESQL_ASYNC_SCHEME = "postgresql+psycopg"
POSTGRESQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
POSTGRESQL_SEARCH_PATH_OPTIONS = "-csearch_path=pg_catalog,spine"


def _validate_postgresql_url(url: SecretStr) -> SecretStr:
    """Validate only the supported async driver while retaining secret wrapping."""

    parsed = urlsplit(url.get_secret_value())
    if parsed.scheme != POSTGRESQL_ASYNC_SCHEME or not parsed.hostname:
        raise ValueError(
            "database URL must use postgresql+psycopg and include a host; "
            "received <redacted-database-url>"
        )
    if not parsed.path.removeprefix("/"):
        raise ValueError(
            "database URL must name a database; received <redacted-database-url>"
        )
    return url


class _SecretWrappingSource(PydanticBaseSettingsSource):
    """Convert secret inputs before Pydantic can retain them in error details."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._source = source

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[object, str, bool]:
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, object]:
        values = self._source()
        for field_name, value in values.items():
            normalized_name = field_name.lower()
            is_sensitive_field = any(
                token in normalized_name
                for token in ("url", "password", "credential", "marker")
            )
            if isinstance(value, str) and is_sensitive_field:
                values[field_name] = SecretStr(value)
        return values


class _DatabaseSettings(BaseSettings):
    """Common safety policy for independently loaded database settings."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_file=None,
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return tuple(
            _SecretWrappingSource(settings_cls, source)
            for source in (
                init_settings,
                env_settings,
                dotenv_settings,
                file_secret_settings,
            )
        )


class RuntimeDatabaseSettings(_DatabaseSettings):
    """Runtime credentials and bounded process-pool configuration."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_file=None,
        env_prefix="SPINE_DATABASE_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    url: SecretStr = Field(repr=False)
    runtime_role: str = "spine_runtime"
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    connect_timeout_seconds: int = Field(default=10, gt=0, le=300)
    pool_pre_ping: bool = True
    transaction_retry_limit: int = Field(default=3, ge=1, le=10)

    _supported_url = field_validator("url")(_validate_postgresql_url)

    @field_validator("runtime_role")
    @classmethod
    def _safe_runtime_role(cls, value: str) -> str:
        if not POSTGRESQL_IDENTIFIER.fullmatch(value):
            raise ValueError("runtime role must be a safe lowercase identifier")
        return value


class MigrationDatabaseSettings(_DatabaseSettings):
    """Credentials exposed only to explicit migration composition roots."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_file=None,
        env_prefix="SPINE_MIGRATION_DATABASE_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    url: SecretStr = Field(repr=False)
    migration_role: str = "spine_migration"
    runtime_role: str = "spine_runtime"

    _supported_url = field_validator("url")(_validate_postgresql_url)

    @field_validator("migration_role", "runtime_role")
    @classmethod
    def _safe_role_identifier(cls, value: str) -> str:
        if not POSTGRESQL_IDENTIFIER.fullmatch(value):
            raise ValueError("role names must be safe lowercase PostgreSQL identifiers")
        return value

    @model_validator(mode="after")
    def _migration_roles_are_distinct(self) -> MigrationDatabaseSettings:
        if self.migration_role == self.runtime_role:
            raise ValueError("migration and runtime roles must be distinct")
        return self


class OperatorDatabaseSettings(_DatabaseSettings):
    """Cluster-level inputs used only by the explicit operator bootstrap."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_file=None,
        env_prefix="SPINE_OPERATOR_DATABASE_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    url: SecretStr = Field(repr=False)
    database_name: str
    migration_role: str
    migration_password: SecretStr = Field(repr=False)
    runtime_role: str
    runtime_password: SecretStr = Field(repr=False)

    _supported_url = field_validator("url")(_validate_postgresql_url)

    @field_validator("database_name", "migration_role", "runtime_role")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if not POSTGRESQL_IDENTIFIER.fullmatch(value):
            raise ValueError(
                "database and role names must be safe lowercase PostgreSQL identifiers"
            )
        return value

    @model_validator(mode="after")
    def _roles_are_distinct(self) -> OperatorDatabaseSettings:
        if self.migration_role == self.runtime_role:
            raise ValueError("migration and runtime roles must be distinct")
        return self


class TestDatabaseSettings(_DatabaseSettings):
    """Dedicated PostgreSQL test target selection; never reads ordinary URLs."""

    __test__ = False

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_file=None,
        env_prefix="SPINE_TEST_DATABASE_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    url: SecretStr | None = Field(default=None, repr=False)
    expected_name: str | None = None
    disposable_marker: SecretStr | None = Field(default=None, repr=False)
    use_testcontainers: bool = True
    image: str = TESTCONTAINERS_POSTGRES_IMAGE

    @field_validator("url")
    @classmethod
    def _supported_test_url(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else _validate_postgresql_url(value)

    @field_validator("image")
    @classmethod
    def _pinned_image(cls, value: str) -> str:
        if value != TESTCONTAINERS_POSTGRES_IMAGE:
            raise ValueError(
                f"test image must be pinned to {TESTCONTAINERS_POSTGRES_IMAGE}"
            )
        return value

    @model_validator(mode="after")
    def _explicit_target_is_fully_identified(self) -> TestDatabaseSettings:
        if self.url is not None and (
            self.expected_name is None or self.disposable_marker is None
        ):
            raise ValueError(
                "explicit test URL requires expected_name and disposable_marker"
            )
        if self.url is None and (
            self.expected_name is not None or self.disposable_marker is not None
        ):
            raise ValueError(
                "expected_name and disposable_marker require an explicit test URL"
            )
        return self


REDACTED_DATABASE_URL = "<redacted-database-url>"


def redact_database_url(_: SecretStr | str) -> str:
    """Return a stable replacement suitable for errors and diagnostics."""

    return REDACTED_DATABASE_URL
