from __future__ import annotations

import pytest

from spine.evals import answer_quality_benchmark_cli


def test_answer_quality_benchmark_cli_requires_all_private_inputs() -> None:
    with pytest.raises(SystemExit):
        answer_quality_benchmark_cli.main([])
