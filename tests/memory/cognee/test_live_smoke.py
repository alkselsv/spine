from spine.memory.cognee.live_smoke import payload_contains_text


def test_payload_text_probe_handles_nested_public_sdk_shapes() -> None:
    payload = [{"search_result": {"chunks": [{"text": "marker after update"}]}}]

    assert payload_contains_text(payload, "marker after update") is True
    assert payload_contains_text(payload, "marker before update") is False


def test_payload_text_probe_does_not_match_mapping_keys() -> None:
    payload = {"protected customer text": {"value": "safe synthetic value"}}

    assert payload_contains_text(payload, "protected customer text") is False
