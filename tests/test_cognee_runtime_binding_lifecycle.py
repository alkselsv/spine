from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_public_sdk_reloads_cached_database_binding_before_use(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        from types import SimpleNamespace

        import spine.memory.cognee_memory as cognee_memory
        from spine.memory.cognee_memory import CogneeEnvironmentBindingError
        from spine.memory.cognee.indexing import CogneePublicSdk

        cognee_memory.settings = SimpleNamespace(
            project_root=Path.cwd(),
            system_root_directory="system",
            data_root_directory="data",
            cache_root_directory="cache",
            llm_api_key="",
        )
        os.environ.update(
            DB_PROVIDER="postgres",
            DB_HOST="stale-target",
            DB_NAME="stale-database",
            VECTOR_DB_PROVIDER="pgvector",
            VECTOR_DB_HOST="stale-target",
            VECTOR_DB_NAME="stale-database",
            GRAPH_DATABASE_PROVIDER="neo4j",
            GRAPH_DATABASE_HOST="stale-graph",
        )

        from cognee.infrastructure.databases.graph.config import get_graph_config
        from cognee.infrastructure.databases.relational.config import get_relational_config
        from cognee.infrastructure.databases.vector.config import get_vectordb_config

        assert get_relational_config().db_name == "stale-database"
        assert get_vectordb_config().vector_db_name == "stale-database"
        assert get_graph_config().graph_database_host == "stale-graph"

        os.environ.update(
            DB_HOST="fresh-target",
            DB_NAME="fresh-database",
            VECTOR_DB_HOST="fresh-target",
            VECTOR_DB_NAME="fresh-database",
            GRAPH_DATABASE_HOST="fresh-graph",
        )

        CogneePublicSdk()

        assert get_relational_config().db_host == "fresh-target"
        assert get_relational_config().db_name == "fresh-database"
        assert get_vectordb_config().vector_db_host == "fresh-target"
        assert get_vectordb_config().vector_db_name == "fresh-database"
        assert get_graph_config().graph_database_host == "fresh-graph"

        selected_relational_config = get_relational_config()
        CogneePublicSdk()
        assert get_relational_config() is selected_relational_config

        os.environ["DB_NAME"] = "unexpected-retarget"
        try:
            CogneePublicSdk()
        except CogneeEnvironmentBindingError as error:
            assert error.reason_code == "cognee_runtime_binding_mismatch"
            assert "unexpected-retarget" not in str(error)
        else:
            raise AssertionError("initialized Cognee process accepted a new target")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
