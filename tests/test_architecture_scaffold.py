from __future__ import annotations

import ast
from pathlib import Path
import unittest
from uuid import uuid4

from pydantic import ValidationError

from spine.domain.common import ActorKind, ActorRef, SchemaRef
from spine.domain.workflows import StepDefinition, StepKind, WorkflowVersion


class ArchitectureScaffoldTests(unittest.TestCase):
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
        forbidden = {"cognee", "fastapi", "sqlalchemy", "temporalio"}

        for path in domain_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.split(".", 1)[0])

            self.assertFalse(imported_roots & forbidden, f"framework import in {path}")


if __name__ == "__main__":
    unittest.main()
