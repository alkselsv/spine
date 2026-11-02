"""Opt-in pytest plugin that emits one safe R1 migration target receipt."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from spine.domain.evaluations.r1_repository_qualification import (
    MigrationTargetReceipt,
)
from spine.infrastructure.db.readiness import load_migration_inventory
from spine.infrastructure.db.test_harness import TestDatabaseProvision

_target_provision: TestDatabaseProvision | None = None


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object],
    request: pytest.FixtureRequest,
) -> object:
    del request
    result = yield
    global _target_provision
    if fixturedef.argname == "postgresql_provision":
        if not isinstance(result, TestDatabaseProvision):
            raise TypeError("postgresql_provision returned an invalid target")
        _target_provision = result
    return result


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _target_provision is None:
        raise RuntimeError("R1 target receipt requires postgresql_provision")
    _write_target_receipt(session, _target_provision)


def _write_target_receipt(
    session: pytest.Session,
    provision: TestDatabaseProvision,
) -> None:
    role = os.environ["SPINE_R1_TARGET_ROLE"]
    receipt_path = Path(os.environ["SPINE_R1_TARGET_RECEIPT"])
    expected_nodeid = {
        "clean": (
            "tests/integration/postgresql/test_r1_migration_targets.py::"
            "test_current_graph_applies_to_clean_target"
        ),
        "upgrade": (
            "tests/integration/postgresql/test_initial_tenancy_migration.py::"
            "test_every_retained_revision_upgrades_to_one_final_head"
        ),
    }.get(role)
    selected_nodeids = {item.nodeid for item in session.items}
    inventory = load_migration_inventory()
    selected_expected_test = expected_nodeid is not None and selected_nodeids == {
        expected_nodeid
    }
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
        status=(
            "passed"
            if session.testsfailed == 0 and selected_expected_test
            else "failed"
        ),
    )
    receipt_path.write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
