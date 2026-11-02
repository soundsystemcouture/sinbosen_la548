"""Regression against recorded nonstandard odd-order Linkwitz-Riley modes."""

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from sinbosen_la548.response import crossover_response  # noqa: E402
from sinbosen_la548.settings import CrossoverSettings  # noqa: E402

MEASUREMENTS = json.loads(
    (Path(__file__).parent / "fixtures" / "odd_lr_measurements.json").read_text()
)


@pytest.mark.parametrize("case", MEASUREMENTS["cases"], ids=lambda case: case["id"])
def test_odd_lr_response_matches_recorded_hardware(case):
    points = np.asarray(case["points"], dtype=float)
    block = CrossoverSettings(case["fc"], 1, case["slope"], True, b"")
    response = crossover_response(block, case["band"], points[:, 0])
    assert response.complete
    assert response.magnitude_db == pytest.approx(points[:, 1], abs=case["db_tolerance"])
    phase_error = (response.phase_deg - points[:, 2] + 180) % 360 - 180
    assert np.max(np.abs(phase_error)) < case["phase_tolerance"]


def test_fixture_covers_every_odd_lr_slope_in_both_directions():
    assert {(case["band"], case["slope"]) for case in MEASUREMENTS["cases"]} == {
        (band, slope) for band in ("hp", "lp") for slope in (6, 18, 30, 42)
    }
