from __future__ import annotations

import ast
from pathlib import Path
import unittest
from uuid import uuid4

from pydantic import ValidationError

from spine.domain.common import ActorKind, ActorRef, SchemaRef
from spine.domain.workflows import StepDefinition, StepKind, WorkflowVersion


class ArchitectureScaffoldTests(unittest.TestCase):
    def assert_no_framework_imports(
        self,
        root: Path,
        *,
        additional_forbidden: set[str] | None = None,
    ) -> None:
        forbidden = {
            "alembic",
            "cognee",
            "fastapi",
            "psycopg",
            "sqlalchemy",
            "temporalio",
        }
        if additional_forbidden is not None:
            forbidden.update(additional_forbidden)

        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.split(".", 1)[0])

            self.assertFalse(imported_roots & forbidden, f"framework import in {path}")

    def test_agent_step_requires_capability(self) -> None:
        with self.assertRaisesRegex(ValidationError, "require a capability"):
            StepDefinition(key="extract", title="Extract", kind=StepKind.AGENT)

    def test_workflow_rejects_unknown_dependency(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown dependencies"):
            WorkflowVersion(
                workflow_id=uuid4(),
                version="1",
                steps=(
                    StepDefinition(
                        key="review",
                        title="Review",
                        kind=StepKind.HUMAN,
                        depends_on=("missing",),
                        assignee=ActorRef(kind=ActorKind.TEAM, id=uuid4()),
                        input_schema=SchemaRef(name="document", version="1"),
                    ),
                ),
            )

    def test_workflow_rejects_cycles(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must be acyclic"):
            WorkflowVersion(
                workflow_id=uuid4(),
                version="1",
                steps=(
                    StepDefinition(key="a", title="A", kind=StepKind.CODE, depends_on=("b",)),
                    StepDefinition(key="b", title="B", kind=StepKind.CODE, depends_on=("a",)),
                ),
            )

    def test_domain_layer_has_no_framework_imports(self) -> None:
        domain_root = Path(__file__).parents[1] / "src" / "spine" / "domain"
        self.assert_no_framework_imports(domain_root)

    def test_domain_layer_has_no_application_or_infrastructure_imports(self) -> None:
        domain_root = Path(__file__).parents[1] / "src" / "spine" / "domain"
        for path in domain_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(
                any(name.startswith("spine.application") or name.startswith("spine.infrastructure") for name in imports),
                f"outer-layer import in {path}",
            )

    def test_application_persistence_contracts_have_no_framework_imports(self) -> None:
        contracts_root = (
            Path(__file__).parents[1] / "src" / "spine" / "application" / "persistence"
        )
        self.assert_no_framework_imports(contracts_root)

    def test_application_diagnostics_contracts_have_no_framework_imports(self) -> None:
        diagnostics_root = (
            Path(__file__).parents[1] / "src" / "spine" / "application" / "diagnostics"
        )
        self.assert_no_framework_imports(
            diagnostics_root,
            additional_forbidden={"langfuse", "logging", "opentelemetry"},
        )

    def test_infrastructure_diagnostics_adapters_have_no_framework_imports(self) -> None:
        adapters_root = (
            Path(__file__).parents[1] / "src" / "spine" / "infrastructure" / "diagnostics"
        )
        self.assert_no_framework_imports(adapters_root)


if __name__ == "__main__":
    unittest.main()
