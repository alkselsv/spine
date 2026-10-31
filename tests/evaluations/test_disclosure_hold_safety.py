import pytest

from spine.evaluations.disclosure_hold_safety import (
    DisclosureHoldSafetyGate,
    DisclosureHoldSafetyObservation,
    DisclosureHoldSafetyViolation,
)


@pytest.mark.parametrize(
    ("lifecycle_eligible", "active_hold", "disclosed", "expected"),
    (
        (True, False, True, ()),
        (
            False,
            False,
            True,
            (DisclosureHoldSafetyViolation.REVIEW_REQUIRED_CONTENT_DISCLOSED,),
        ),
        (
            True,
            True,
            True,
            (DisclosureHoldSafetyViolation.HELD_CONTENT_DISCLOSED,),
        ),
        (True, False, False, ()),
        (False, True, False, ()),
    ),
)
def test_disclosure_hold_release_gate_preserves_denials_and_unrestricted_behavior(
    lifecycle_eligible: bool,
    active_hold: bool,
    disclosed: bool,
    expected: tuple[DisclosureHoldSafetyViolation, ...],
) -> None:
    result = DisclosureHoldSafetyGate().evaluate(
        DisclosureHoldSafetyObservation(
            lifecycle_disclosure_eligible=lifecycle_eligible,
            active_disclosure_hold=active_hold,
            content_disclosed=disclosed,
        )
    )

    assert result.violation_codes == expected
    assert result.status == ("failed" if expected else "passed")
