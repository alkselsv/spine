from __future__ import annotations

import os
import subprocess
import sys


def test_importing_configuration_and_persistence_creates_no_engine_or_socket() -> None:
    program = """
import socket
import sqlalchemy.ext.asyncio

def reject(*args, **kwargs):
    raise AssertionError("import-time side effect")

socket.socket.connect = reject
sqlalchemy.ext.asyncio.create_async_engine = reject

import spine.infrastructure.db.settings
import spine.infrastructure.db.engine
import spine.infrastructure.db.test_harness
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=".",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
