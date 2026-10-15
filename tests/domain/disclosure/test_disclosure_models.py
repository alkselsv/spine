from uuid import UUID

from spine.domain.disclosure import DisclosureDependencySet

from tests.domain.context_bundle.test_context_bundle_models import context_bundle


def test_dependency_set_contains_every_source_reaching_generation() -> None:
    bundle = context_bundle(
        evidence_references=(
            *context_bundle().evidence_references,
            context_bundle().evidence_references[0].model_copy(
                update={
                    "evidence_id": "evidence-uncited",
                    "source_object_id": UUID(int=25),
                    "source_revision_id": UUID(int=26),
                    "revision_digest": "8" * 64,
                }
            ),
        ),
        policy_decision_generations=(
            *context_bundle().policy_decision_generations,
            context_bundle().policy_decision_generations[0].model_copy(
                update={
                    "source_object_id": UUID(int=25),
                    "policy_version_id": UUID(int=27),
                    "decision_reference": UUID(int=28),
                }
            ),
        ),
    )

    dependencies = DisclosureDependencySet.from_context_bundle(bundle)

    assert {item.source_object_id for item in dependencies.dependencies} == {
        UUID(int=20),
        UUID(int=25),
    }


def test_dependency_set_merge_deduplicates_sources_from_intermediate_attempts() -> None:
    dependency = DisclosureDependencySet.from_context_bundle(context_bundle())

    merged = dependency.merge(dependency)

    assert merged == dependency
