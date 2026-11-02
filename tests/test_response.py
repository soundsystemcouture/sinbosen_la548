from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from sinbosen_la548.response import (  # noqa: E402
    channel_response,
    compose_responses,
    crossover_response,
    eq_response,
    frequency_grid,
    normalize_eq,
    normalize_xover,
    path_response,
    sum_responses,
)
from sinbosen_la548.settings import CrossoverSettings, EQBandSettings, decode_settings  # noqa: E402


@pytest.fixture
def settings():
    return decode_settings((Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes())


def band(type=4, gain=0, q=1, freq=1000, slope=0):
    return EQBandSettings(freq_hz=freq, gain_db=gain, q=q, slope=slope, type=type, enabled=True, raw=b"")


def test_allpass_unity_magnitude_and_center_phase():
    frequencies = np.array([100.0, 1000.0, 10000.0])
    response = eq_response(band(), frequencies)
    assert np.max(np.abs(response.magnitude_db)) < 1e-9
    assert abs(response.phase_deg[1]) == pytest.approx(180, abs=1e-9)
    assert response.phase_unwrapped_deg[2] < response.phase_unwrapped_deg[0]


def test_allpass_q_changes_phase_transition():
    frequencies = np.array([800.0, 1000.0, 1250.0])
    low = eq_response(band(q=0.5), frequencies)
    high = eq_response(band(q=2), frequencies)
    assert (high.phase_unwrapped_deg[0] - high.phase_unwrapped_deg[2]
            > low.phase_unwrapped_deg[0] - low.phase_unwrapped_deg[2])


@pytest.mark.parametrize("gain", [-12, 0, 12])
def test_peaking_center_gain(gain):
    response = eq_response(band(type=0, gain=gain), np.array([100.0, 1000.0, 10000.0]))
    assert response.magnitude_db[1] == pytest.approx(gain, abs=1e-9)


@pytest.mark.parametrize("type,low,high", [(1, 12, 0), (2, 0, 12)])
def test_shelf_limits(type, low, high):
    response = eq_response(band(type=type, gain=12), np.array([1.0, 1000.0, 47999.0]))
    assert response.magnitude_db[0] == pytest.approx(low, abs=0.001)
    assert response.magnitude_db[-1] == pytest.approx(high, abs=0.001)


@pytest.mark.parametrize("family,corner_db", [(0, -3.0102999566), (1, -6.0205999132)])
@pytest.mark.parametrize("name", ["hp", "lp"])
def test_crossover_corner(family, corner_db, name):
    block = CrossoverSettings(1000, family, 24, True, b"")
    response = crossover_response(block, name, np.array([100.0, 1000.0, 10000.0]))
    assert response.magnitude_db[1] == pytest.approx(corner_db, abs=1e-7)
    assert response.magnitude_db[0 if name == "hp" else 2] < -60


@pytest.mark.parametrize("family", [0, 1, 2])
def test_steeper_crossover_attenuates_more(family):
    frequencies = np.array([100.0, 1000.0, 10000.0])
    low = crossover_response(CrossoverSettings(1000, family, 12, True, b""), "lp", frequencies)
    high = crossover_response(CrossoverSettings(1000, family, 48, True, b""), "lp", frequencies)
    assert high.magnitude_db[-1] < low.magnitude_db[-1] - 30


@pytest.mark.parametrize("delay", [0.01, 49.99, 50, 1200])
def test_delay_phase_and_group_delay_are_analytic(settings, delay):
    channel = settings.outputs[2]
    channel = replace(channel, base=replace(channel.base, delay_ms=delay))
    response = channel_response(channel).total
    assert np.allclose(response.group_delay_ms, delay, atol=1e-10)
    assert np.allclose(response.phase_unwrapped_deg, -360 * response.frequencies * delay / 1000)


def test_cascade_preserves_long_delay_and_allpass(settings):
    channel = settings.outputs[2]
    channel = replace(channel, base=replace(channel.base, delay_ms=1200), eq_bands=(band(),) + channel.eq_bands[1:])
    frequencies = frequency_grid()
    response = channel_response(channel, frequencies).total
    ap = eq_response(band(), frequencies)
    assert np.allclose(response.group_delay_ms, ap.group_delay_ms + 1200)
    assert np.allclose(response.phase_unwrapped_deg, ap.phase_unwrapped_deg - 360 * frequencies * 1.2)


def test_polarity_cancels_and_null_phase_is_masked(settings):
    channel = settings.outputs[2]
    a = channel_response(channel).total
    b = channel_response(replace(channel, base=replace(channel.base, inverted=True))).total
    summed = sum_responses(a, b)
    assert np.max(np.abs(summed.transfer)) < 1e-12
    assert np.all(np.isnan(summed.phase_deg))
    assert np.all(np.isnan(summed.group_delay_ms))


def test_identical_responses_sum_to_six_db(settings):
    response = channel_response(settings.outputs[2]).total
    summed = sum_responses(response, response)
    assert np.allclose(summed.magnitude_db, 6.020599913)


def test_sum_preserves_shared_long_delay_phase(settings):
    channel = settings.outputs[2]
    channel = replace(channel, base=replace(channel.base, delay_ms=1200))
    response = channel_response(channel).total
    summed = sum_responses(response, response)
    assert np.allclose(summed.phase_unwrapped_deg, response.phase_unwrapped_deg)
    assert np.allclose(summed.group_delay_ms, 1200)


def test_lr24_complementary_sum_is_flat():
    frequencies = frequency_grid()
    block = CrossoverSettings(1000, 1, 24, True, b"")
    summed = sum_responses(crossover_response(block, "hp", frequencies), crossover_response(block, "lp", frequencies))
    assert np.max(np.abs(summed.magnitude_db)) < 1e-9


def test_mute_masks_phase(settings):
    channel = settings.outputs[2]
    response = channel_response(replace(channel, base=replace(channel.base, muted=True))).total
    assert np.all(response.transfer == 0)
    assert np.all(np.isnan(response.phase_deg))
    assert np.all(np.isnan(response.group_delay_ms))


def test_enabled_unknown_filters_and_fir_are_incomplete(settings):
    channel = settings.outputs[2]
    channel = replace(channel, eq_bands=(band(type=6),) + channel.eq_bands[1:], fir=replace(channel.fir, enabled=True))
    response = channel_response(channel).total
    assert not response.complete
    assert any("type 6" in note for note in response.notes)
    assert any("FIR" in note for note in response.notes)


@pytest.mark.parametrize("slope", [6, 18, 30, 42])
@pytest.mark.parametrize("name", ["hp", "lp"])
def test_odd_linkwitz_riley_modes_are_modeled(slope, name):
    response = crossover_response(
        CrossoverSettings(1000, 1, slope, True, b""), name, np.array([100.0, 1000.0, 10000.0])
    )
    assert response.complete
    assert response.magnitude_db[1] == pytest.approx(-6, abs=0.8)
    assert any("LA548-specific" in note for note in response.notes)


def test_type3_first_order_allpass_matches_hardware_phase():
    frequencies = np.array([500.0, 1000.0, 2000.0])
    expected_deg = [-53.13, -90.0, -126.93]
    response = eq_response(band(type=3, freq=1000), frequencies)
    assert np.max(np.abs(response.magnitude_db)) < 1e-6
    assert response.phase_deg == pytest.approx(expected_deg, abs=0.05)


def test_type3_allpass_ignores_q_and_gain():
    frequencies = np.array([500.0, 1000.0, 2000.0])
    baseline = eq_response(band(type=3, q=1, gain=0, freq=1000), frequencies)
    for q, gain in [(0.5, 0), (2, 0), (1, 6), (1, 12)]:
        other = eq_response(band(type=3, q=q, gain=gain, freq=1000), frequencies)
        assert np.allclose(other.transfer, baseline.transfer)


def test_type5_notch_matches_hardware():
    # fc=1kHz, gain=-20dB, Q~1: hardware measured -20.009 dB at center, -1.608 dB at 500 Hz.
    response = eq_response(band(type=5, freq=1000, gain=-20, q=1), np.array([500.0, 1000.0, 2000.0]))
    assert response.complete
    assert response.magnitude_db[0] == pytest.approx(-1.608, abs=0.05)
    assert response.magnitude_db[1] == pytest.approx(-20.009, abs=0.05)
    assert not response.notes

    # fc=2kHz, gain=-12dB, Q~2: hardware measured -11.996 dB at center, -0.476 dB at 1 kHz.
    response = eq_response(band(type=5, freq=2000, gain=-12, q=2), np.array([500.0, 1000.0, 4000.0]))
    assert response.magnitude_db[1] == pytest.approx(-0.476, abs=0.03)


# Extended hardware sweep:
# 10 additional hardware configurations independently varying gain, Q, and center
# frequency; full captured responses are checked in test_filter_measurements.py.
# (gain_db, q, fc, off_center_freq, off_center_db, center_db)
_TYPE5_EXTENDED_CASES = [
    (-6, 1, 1000, 500, -1.893, -5.996),
    (-18, 1, 1000, 500, -1.609, -18.002),
    (-12, 0.5, 1000, 500, -4.285, -11.994),
    (-12, 4, 1000, 500, -0.124, -11.997),
    (-12, 1, 500, 250, -1.653, -11.996),
    (-12, 1, 4000, 2000, -1.633, -12.001),
]


@pytest.mark.parametrize("gain,q,fc,off_freq,off_db,center_db", _TYPE5_EXTENDED_CASES)
def test_type5_notch_matches_extended_hardware_sweep(gain, q, fc, off_freq, off_db, center_db):
    frequencies = np.array([float(off_freq), float(fc), float(fc) * 10])
    response = eq_response(band(type=5, freq=fc, gain=gain, q=q), frequencies)
    assert response.magnitude_db[0] == pytest.approx(off_db, abs=0.1)
    assert response.magnitude_db[1] == pytest.approx(center_db, abs=0.1)


@pytest.mark.parametrize("gain", [0, 6, 12])
def test_type5_notch_is_flat_for_zero_and_positive_gain(gain):
    response = eq_response(band(type=5, freq=1000, gain=gain), np.array([500.0, 1000.0, 2000.0]))
    assert np.max(np.abs(response.magnitude_db)) < 1e-6


def test_type5_slope_0_and_1_match_but_others_stay_unresolved():
    frequencies = np.array([500.0, 1000.0, 2000.0])
    slope0 = eq_response(band(type=5, freq=1000, gain=-20, slope=0), frequencies)
    slope1 = eq_response(band(type=5, freq=1000, gain=-20, slope=1), frequencies)
    assert np.allclose(slope0.transfer, slope1.transfer)
    slope2 = eq_response(band(type=5, freq=1000, gain=-20, slope=2), frequencies)
    assert not slope2.complete


@pytest.mark.parametrize("gain,expected_db", [(6, 2.828), (12, 7.464)])
def test_peaking_q_effective_correction_matches_hardware(gain, expected_db):
    response = eq_response(band(type=0, freq=1000, gain=gain, q=1), np.array([500.0, 1000.0, 2000.0]))
    assert response.magnitude_db[0] == pytest.approx(expected_db, abs=0.05)


@pytest.mark.parametrize("slope", [12, 24])
@pytest.mark.parametrize("name", ["hp", "lp"])
def test_bessel_crossover_matches_hardware_cutoff(name, slope):
    block = CrossoverSettings(1000, 2, slope, True, b"")
    response = crossover_response(block, name, np.array([500.0, 1000.0, 2000.0]))
    assert response.magnitude_db[1] == pytest.approx(-3.0103, abs=0.1)


# Extended hardware sweep:
# HP Bessel re-tested at 500/1000/2000 Hz cutoffs (frequencies that land exactly on
# the measurement grid) confirms the -3 dB-at-cutoff match holds across a cutoff
# range, not just the single 1 kHz case - the reason a "shift the assumed cutoff"
# correction was rejected in response.py despite reducing off-cutoff error elsewhere.
@pytest.mark.parametrize("fc,measured_db", [(500, -2.9946), (1000, -2.9927), (2000, -2.9946)])
def test_bessel_hp_cutoff_holds_across_configured_frequencies(fc, measured_db):
    block = CrossoverSettings(fc, 2, 12, True, b"")
    frequencies = np.array([fc / 2, float(fc), fc * 2.0])
    response = crossover_response(block, "hp", frequencies)
    assert response.magnitude_db[1] == pytest.approx(measured_db, abs=0.02)


def test_edits_normalize_to_codec_quantization():
    normalized = normalize_eq(band(type=0, gain=1.19, q=1.2345))
    assert normalized.gain_db == pytest.approx(1.1)
    assert normalized.q != 1.2345
    block = normalize_xover(CrossoverSettings(1000, 1, 24, True, b""))
    assert block.raw == b"\xe8\x03\x01\x03\x01"


def test_path_requires_routing_and_includes_input_delay(settings):
    settings = replace(settings, inputs=(replace(settings.inputs[0], base=replace(settings.inputs[0].base,
                                                                               delay_ms=2)),) + settings.inputs[1:])
    response = path_response(settings, 0, 0)
    assert np.allclose(response.group_delay_ms, 2)
    disconnected = path_response(settings, 0, 7)
    assert np.all(disconnected.transfer == 0)
    with pytest.raises(ValueError):
        path_response(settings, 4, 0)


def test_grid_validation_and_matching(settings):
    with pytest.raises(ValueError):
        frequency_grid(high=48000)
    with pytest.raises(ValueError):
        eq_response(band(), np.array([1.0, 2.0, 2.0]))
    a = channel_response(settings.outputs[2]).total
    b = channel_response(settings.outputs[2], frequency_grid(points=100)).total
    with pytest.raises(ValueError):
        compose_responses(a, b)
    with pytest.raises(ValueError):
        sum_responses(a, b)


def test_response_arrays_are_immutable(settings):
    response = channel_response(settings.outputs[2]).total
    with pytest.raises(ValueError):
        response.transfer[0] = 42


def test_maximum_delay_cancellation_masks_roundoff_phase(settings):
    ch = settings.outputs[2]
    a = channel_response(replace(ch, base=replace(ch.base, delay_ms=1200, inverted=False))).total
    b = channel_response(replace(ch, base=replace(ch.base, delay_ms=1200, inverted=True))).total
    combined = sum_responses(a, b)
    assert np.isnan(combined.phase_deg).all()
    assert np.isnan(combined.group_delay_ms).all()
