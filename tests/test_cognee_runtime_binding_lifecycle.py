from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

import spine.memory.cognee_memory as cognee_memory


ConfigGetter = Callable[[], SimpleNamespace]


@pytest.fixture
def cached_cognee_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[dict[str, ConfigGetter]]:
    monkeypatch.setattr(cognee_memory, "_cognee_database_binding_digest", None)
    monkeypatch.setattr(cognee_memory, "_cognee_database_binding_frozen", False)
    monkeypatch.setattr(
        cognee_memory,
        "settings",
        SimpleNamespace(
            project_root=tmp_path,
            system_root_directory="system",
            data_root_directory="data",
            cache_root_directory="cache",
            llm_api_key="",
            spine_dataset="spine",
        ),
    )

    @lru_cache(maxsize=1)
    def get_base_config() -> SimpleNamespace:
        return SimpleNamespace(data_root=os.environ["DATA_ROOT_DIRECTORY"])

    @lru_cache(maxsize=1)
    def get_relational_config() -> SimpleNamespace:
        return SimpleNamespace(db_name=os.environ["DB_NAME"])

    @lru_cache(maxsize=1)
    def get_migration_config() -> SimpleNamespace:
        return SimpleNamespace(migration_db_name=os.environ["MIGRATION_DB_NAME"])

    @lru_cache(maxsize=1)
    def get_vectordb_config() -> SimpleNamespace:
        return SimpleNamespace(vector_db_name=os.environ["VECTOR_DB_NAME"])

    @lru_cache(maxsize=1)
    def get_graph_config() -> SimpleNamespace:
        return SimpleNamespace(graph_database_name=os.environ["GRAPH_DATABASE_NAME"])

    getters = {
        "base": get_base_config,
        "relational": get_relational_config,
        "migration": get_migration_config,
        "vector": get_vectordb_config,
        "graph": get_graph_config,
    }
    modules = {
        "cognee.base_config": {"get_base_config": get_base_config},
        "cognee.infrastructure.databases.relational.config": {
            "get_relational_config": get_relational_config,
            "get_migration_config": get_migration_config,
        },
        "cognee.infrastructure.databases.vector.config": {
            "get_vectordb_config": get_vectordb_config,
        },
        "cognee.infrastructure.databases.graph.config": {
            "get_graph_config": get_graph_config,
        },
    }
    package_names = (
        "cognee",
        "cognee.infrastructure",
        "cognee.infrastructure.databases",
        "cognee.infrastructure.databases.relational",
        "cognee.infrastructure.databases.vector",
        "cognee.infrastructure.databases.graph",
    )
    for name in package_names:
        package = ModuleType(name)
        package.__path__ = []  # type: ignore[attr-defined]
        if name == "cognee":
            package.__version__ = "1.5.4"
        monkeypatch.setitem(sys.modules, name, package)
    for name, attributes in modules.items():
        module = ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("DB_NAME", "stale-relational")
    monkeypatch.setenv("MIGRATION_DB_NAME", "stale-migration")
    monkeypatch.setenv("VECTOR_DB_NAME", "stale-vector")
    monkeypatch.setenv("GRAPH_DATABASE_NAME", "stale-graph")
    monkeypatch.setenv("DATA_ROOT_DIRECTORY", "stale-data-root")
    for getter in getters.values():
        getter()

    yield getters


@pytest.mark.parametrize(
    ("getter_name", "environment_key", "attribute", "fresh_value"),
    (
        ("relational", "DB_NAME", "db_name", "fresh-relational"),
        ("migration", "MIGRATION_DB_NAME", "migration_db_name", "fresh-migration"),
        ("vector", "VECTOR_DB_NAME", "vector_db_name", "fresh-vector"),
        ("graph", "GRAPH_DATABASE_NAME", "graph_database_name", "fresh-graph"),
    ),
)
def test_configure_environment_refreshes_each_cached_database_binding(
    cached_cognee_configuration: dict[str, ConfigGetter],
    monkeypatch: pytest.MonkeyPatch,
    getter_name: str,
    environment_key: str,
    attribute: str,
    fresh_value: str,
) -> None:
    monkeypatch.setenv(environment_key, fresh_value)

    cognee_memory.configure_environment()

    configured = cached_cognee_configuration[getter_name]()
    assert getattr(configured, attribute) == fresh_value


def test_configure_environment_refreshes_cached_base_configuration(
    cached_cognee_configuration: dict[str, ConfigGetter],
    tmp_path: Path,
) -> None:
    cognee_memory.configure_environment()

    configured = cached_cognee_configuration["base"]()
    assert configured.data_root == str((tmp_path / "data").resolve())


def test_configure_environment_selects_shared_pgvector_without_createdb(
    cached_cognee_configuration: dict[str, ConfigGetter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.delenv("VECTOR_DATASET_DATABASE_HANDLER", raising=False)

    cognee_memory.configure_environment()

    assert os.environ["VECTOR_DATASET_DATABASE_HANDLER"] == "pgvector_shared"


def test_configure_environment_preserves_explicit_vector_dataset_handler(
    cached_cognee_configuration: dict[str, ConfigGetter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "operator_selected")

    cognee_memory.configure_environment()

    assert os.environ["VECTOR_DATASET_DATABASE_HANDLER"] == "operator_selected"


def test_load_configured_cognee_accepts_the_frozen_binding_on_repeat(
    cached_cognee_configuration: dict[str, ConfigGetter],
) -> None:
    first_sdk = cognee_memory.load_configured_cognee()

    repeated_sdk = cognee_memory.load_configured_cognee()

    assert repeated_sdk is first_sdk


def test_frozen_cognee_binding_rejects_database_retarget_without_disclosure(
    cached_cognee_configuration: dict[str, ConfigGetter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cognee_memory.load_configured_cognee()
    unexpected_target = "unexpected-retarget"
    monkeypatch.setenv("MIGRATION_DB_NAME", unexpected_target)

    with pytest.raises(cognee_memory.CogneeEnvironmentBindingError) as error:
        cognee_memory.load_configured_cognee()

    assert error.value.reason_code == "cognee_runtime_binding_mismatch"
    assert unexpected_target not in str(error.value)
