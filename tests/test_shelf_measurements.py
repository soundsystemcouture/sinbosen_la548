"""Shelf behavior and input EQ parity measured on the LA548."""

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from sinbosen_la548.response import eq_response  # noqa: E402
from sinbosen_la548.settings import EQBandSettings  # noqa: E402

CASES = json.loads((Path(__file__).parent / "fixtures/shelf_measurements.json").read_text())["cases"]
CASES += json.loads((Path(__file__).parent / "fixtures/input_eq_measurements.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_eq_matches_measured_shelf_and_input_behavior(case):
    points = np.asarray(case["points"], dtype=float)
    band = EQBandSettings(case["fc"], case["gain"], case["q"], case["slope"], case["type"], True, b"")
    response = eq_response(band, points[:, 0])
    assert response.complete
    assert response.magnitude_db == pytest.approx(points[:, 1], abs=0.03)
    error = (response.phase_deg - points[:, 2] + 180) % 360 - 180
    assert np.max(np.abs(error)) < 0.2


@pytest.mark.parametrize("gain", [-0.1, 0.1])
def test_first_order_shelf_has_measured_phase_jump_from_zero_gain(gain):
    frequencies = np.array([500.0, 1000.0, 2000.0])
    bypass = eq_response(EQBandSettings(1000, 0, 1, 0, 1, True, b""), frequencies)
    active = eq_response(EQBandSettings(1000, gain, 1, 0, 1, True, b""), frequencies)
    assert np.allclose(bypass.transfer, 1)
    assert active.phase_deg[1] == pytest.approx(-89.68, abs=0.05)
