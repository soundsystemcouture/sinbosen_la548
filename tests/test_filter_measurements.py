"""Regression against recorded LA548 audio, independent of model coefficients."""

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from sinbosen_la548.response import crossover_response, eq_response  # noqa: E402
from sinbosen_la548.settings import CrossoverSettings, EQBandSettings  # noqa: E402

MEASUREMENTS = json.loads((Path(__file__).parent / "fixtures" / "filter_measurements.json").read_text())


@pytest.mark.parametrize("case", MEASUREMENTS["cases"], ids=lambda case: case["id"])
def test_response_matches_recorded_hardware(case):
    points = np.asarray(case["points"], dtype=float)
    if case["kind"] == "notch":
        # q is the decoded wire value from the capture settings, not rounded UI Q.
        band = EQBandSettings(case["fc"], case["gain"], case["q"], 0, 5, True, b"")
        response = eq_response(band, points[:, 0])
    else:
        block = CrossoverSettings(case["fc"], 2, case["slope"], True, b"")
        response = crossover_response(block, "hp", points[:, 0])
    assert response.complete
    assert response.magnitude_db == pytest.approx(points[:, 1], abs=case["db_tolerance"])
    phase_error = (response.phase_deg - points[:, 2] + 180) % 360 - 180
    assert np.max(np.abs(phase_error)) < case["phase_tolerance"]


def test_measurements_include_independent_cutoffs_for_every_bessel_slope():
    # Prevent accidentally replacing the validation set with the 1 kHz fit data.
    for slope in range(12, 49, 6):
        assert any(c["kind"] == "bessel_hp" and c["slope"] == slope and c["fc"] != 1000
                   for c in MEASUREMENTS["cases"])


def test_measurements_cover_notch_branch_boundary():
    gains = {c["gain"] for c in MEASUREMENTS["cases"] if c["kind"] == "notch"}
    assert {-5.9, -6.0, -6.1}.issubset(gains)
