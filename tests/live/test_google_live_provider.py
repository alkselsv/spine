"""Manual live-provider entry point; never part of ordinary CI."""

from __future__ import annotations

import os

import pytest


@pytest.mark.google_live
@pytest.mark.skipif(
    os.environ.get("SPINE_GOOGLE_LIVE_OPT_IN") != "1",
    reason="Google live qualification requires explicit SPINE_GOOGLE_LIVE_OPT_IN=1",
)
def test_google_live_qualification_is_run_through_sanitized_cli_harness() -> None:
    """The operator runs spine-google-live-qualification with approved inputs."""

    config_path = os.environ.get("SPINE_GOOGLE_LIVE_CONFIG")
    report_path = os.environ.get("SPINE_GOOGLE_LIVE_REPORT")
    if not config_path or not report_path:
        pytest.skip("approved live config and report paths are required")
    from spine.scripts.google_live_qualification import main

    assert main(["--config", config_path, "--report", report_path]) == 0
