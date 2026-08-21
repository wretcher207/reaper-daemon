"""Ground-truth fixtures for the musical automation rollout."""

import json
from pathlib import Path


FIXTURES = Path(__file__).with_name("fixtures")


def test_three_section_fixture_has_sixteen_nested_windows():
    fixture = json.loads(
        (FIXTURES / "musical_automation_three_sections_16_windows.json").read_text(
            encoding="utf-8"
        )
    )
    regions = {region["id"]: region for region in fixture["regions"]}
    windows = {window["id"]: window for window in fixture["activation_windows"]}
    expected = fixture["expected"]

    assert expected["macro_section_count"] == len(regions) == 3
    assert expected["activation_window_count"] == len(windows) == 16

    assigned = []
    for section in expected["macro_sections"]:
        region = regions[section["id"]]
        for window_id in section["window_ids"]:
            window = windows[window_id]
            assert region["start_bar"] <= window["start_bar"]
            assert window["end_bar"] <= region["end_bar"]
            assigned.append(window_id)

    assert sorted(assigned) == sorted(windows)
    assert [windows[wid]["number"] for wid in expected["macro_sections"][0]["window_ids"]] == list(range(1, 7))
    assert [windows[wid]["number"] for wid in expected["macro_sections"][1]["window_ids"]] == list(range(1, 6))
    assert [windows[wid]["number"] for wid in expected["macro_sections"][2]["window_ids"]] == list(range(1, 6))
