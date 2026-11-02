from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import pytest
import pytest_asyncio

from spine.domain.evaluations.r1_repository_qualification import (
    MigrationTargetReceipt,
)
from spine.infrastructure.db.readiness import load_migration_inventory
from spine.infrastructure.db.settings import TestDatabaseSettings
from spine.infrastructure.db.test_harness import (
    PostgreSQLGateError,
    TestDatabaseProvision,
    UnsafeTestTargetError,
    install_container_marker,
    provision_test_database,
)

_r1_target_provision: TestDatabaseProvision | None = None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgresql_provision() -> TestDatabaseProvision:
    global _r1_target_provision
    settings = TestDatabaseSettings()
    run_id = f"{os.getenv('PYTEST_XDIST_WORKER', 'local')}-{uuid.uuid4().hex[:12]}"
    failure: str | None = None
    try:
        provision = provision_test_database(settings, run_id=run_id)
        if not provision.is_explicit:
            await install_container_marker(provision)
    except (PostgreSQLGateError, UnsafeTestTargetError) as error:
        failure = str(error)
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    if os.getenv("SPINE_R1_TARGET_ROLE") is not None:
        _r1_target_provision = provision

    try:
        yield provision
    finally:
        if provision.container is not None:
            provision.container.stop()


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _r1_target_provision is not None:
        _write_r1_target_receipt(session, _r1_target_provision)


def _write_r1_target_receipt(
    session: pytest.Session,
    provision: TestDatabaseProvision,
) -> None:
    role = os.getenv("SPINE_R1_TARGET_ROLE")
    receipt_value = os.getenv("SPINE_R1_TARGET_RECEIPT")
    if role is None and receipt_value is None:
        return
    if role not in {"clean", "upgrade"} or receipt_value is None:
        raise RuntimeError("R1 target role and receipt path must be configured together")

    expected_nodeid = {
        "clean": (
            "tests/integration/postgresql/test_r1_migration_targets.py::"
            "test_current_graph_applies_to_clean_target"
        ),
        "upgrade": (
            "tests/integration/postgresql/test_initial_tenancy_migration.py::"
            "test_every_retained_revision_upgrades_to_one_final_head"
        ),
    }[role]
    selected_nodeids = {item.nodeid for item in session.items}
    inventory = load_migration_inventory()
    selected_expected_test = selected_nodeids == {expected_nodeid}
    status = (
        "passed"
        if session.testsfailed == 0 and selected_expected_test
        else "failed"
    )
    identity = (
        f"{provision.target.database_name}:"
        f"{provision.target.disposable_marker.get_secret_value()}"
    )
    receipt = MigrationTargetReceipt(
        role=role,
        target_fingerprint=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        postgresql_major=provision.target.postgresql_major,
        head_revision=inventory.supported_head,
        retained_revision_count=len(inventory.revisions),
        tested_revision_count=(
            len(inventory.revisions) if role == "upgrade" else 1
        )
        if selected_expected_test
        else 0,
        status=status,
    )
    Path(receipt_value).write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
