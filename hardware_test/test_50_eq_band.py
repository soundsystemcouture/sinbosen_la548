"""Live confirmation of set_eq_band's boost/cut gain and Q, using the
whole-struct wire format (see `codecs.eq_struct` and docs/PROTOCOL.md).

Like crossover (test_40_xover.py), an EQ band only takes effect when the
whole band struct is written as one 0xA4 frame with `data_id=0`; per-field
writes are ACK'd but inaudible. Unlike crossover, EQ works on **both**
directions.

Uses a plain tone at the EQ band's own center frequency (the same
onset-aligned dBFS technique as test_10_volume.py) for the gain check.

The Q check fits the measured peak shape to the RBJ model in
`sinbosen_la548.response.eq_response`. Reading the half-gain bandwidth off a
raw sweep FFT ratio is biased for this filter: on a perfect device it returns
Q=1.13 for a stored Q=2.01, because the bell is not flat over its "passband"
reference and Q is not defined by the half-gain points. Identical noise feeds
both outputs and output 2 is the untouched reference in the same capture
(`analysis.relative_response`, as test_60). Passes on hardware: fitted Q 2.01
vs stored 2.012, and the measured curve matched the model to 0.00 dB rms.
"""

from __future__ import annotations

import time

import numpy as np

from sinbosen_la548.response import eq_response
from sinbosen_la548.settings import DA0408X, EQBandSettings

from . import analysis, signal_gen
from .rig import Rig

TONE_DURATION_S = 2.0
TONE_AMPLITUDE = 0.2
GAIN_TOLERANCE_DB = 1.0

NOISE_DURATION_S = 4.0
NOISE_AMPLITUDE = 0.2
REFERENCE_CHANNEL = 1  # untouched output in the same capture
Q_GRID_HZ = tuple(float(f) for f in np.geomspace(200.0, 5000.0, 41))
Q_CANDIDATES = np.linspace(0.5, 8.0, 751)
Q_RELATIVE_TOLERANCE = 0.1  # the fit on hardware landed on the stored Q exactly
SHAPE_RMS_TOLERANCE_DB = 0.5  # rms vs the model on hardware was 0.00 dB

OUTPUT_CHANNEL = 0
BAND_INDEX = 0
TEST_FREQ_HZ = 1000


def _dbfs_at(rig: Rig, freq_hz: float, capture_column: int) -> float:
    tone = signal_gen.sine(freq_hz, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, capture_column], rig.sample_rate, TONE_DURATION_S)


def _gain_check(rig: Rig, *, output: bool) -> None:
    channel = OUTPUT_CHANNEL

    rig.client.set_eq_band(
        channel, BAND_INDEX, freq_hz=TEST_FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=output
    )
    time.sleep(rig.settle_s)
    baseline_db = _dbfs_at(rig, TEST_FREQ_HZ, channel)

    rig.client.set_eq_band(
        channel, BAND_INDEX, freq_hz=TEST_FREQ_HZ, gain_db=12.0, q=1.0, type=0, slope=0, enabled=True, output=output
    )
    time.sleep(rig.settle_s)
    boosted_db = _dbfs_at(rig, TEST_FREQ_HZ, channel)

    rig.client.set_eq_band(
        channel, BAND_INDEX, gain_db=-12.0, freq_hz=TEST_FREQ_HZ, q=1.0, type=0, slope=0, enabled=True, output=output
    )
    time.sleep(rig.settle_s)
    cut_db = _dbfs_at(rig, TEST_FREQ_HZ, channel)

    rig.client.set_eq_band(
        channel, BAND_INDEX, freq_hz=TEST_FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=output
    )

    boost_delta = boosted_db - baseline_db
    cut_delta = cut_db - baseline_db
    label = "output" if output else "input"
    print(f"{label} EQ band0@{TEST_FREQ_HZ}Hz -> baseline={baseline_db:.2f} dBFS, boost delta={boost_delta:.2f} dB, "
          f"cut delta={cut_delta:.2f} dB")
    assert abs(boost_delta - 12.0) < GAIN_TOLERANCE_DB, f"{label} +12dB boost measured {boost_delta:.2f} dB"
    assert abs(cut_delta - (-12.0)) < GAIN_TOLERANCE_DB, f"{label} -12dB cut measured {cut_delta:.2f} dB"
    print(f"CONFIRMED: {label} EQ boost/cut tracks configured dB")


def _model_db(q: float) -> np.ndarray:
    band = EQBandSettings(TEST_FREQ_HZ, 12.0, q, 0, 0, True, raw=b"")
    response = eq_response(band, np.asarray(Q_GRID_HZ), sample_rate_hz=DA0408X.sample_rate_hz)
    return np.asarray(20 * np.log10(np.abs(response.transfer)))


def _noise_capture(rig: Rig, *, enabled: bool, q: float) -> np.ndarray:
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=TEST_FREQ_HZ, gain_db=12.0 if enabled else 0.0, q=q, type=0, slope=0,
        enabled=enabled, output=True,
    )
    time.sleep(rig.settle_s)
    noise = signal_gen.white_noise(NOISE_DURATION_S, amplitude=NOISE_AMPLITUDE, sample_rate=rig.sample_rate, seed=0)
    return rig.measure(noise, post_roll_s=0.3)


def _q_check(rig: Rig) -> None:
    configured_q = 2.0
    baseline = _noise_capture(rig, enabled=False, q=configured_q)
    boosted = _noise_capture(rig, enabled=True, q=configured_q)
    stored_q = rig.client.read_settings().outputs[OUTPUT_CHANNEL].eq_bands[BAND_INDEX].q
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=TEST_FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0,
        enabled=False, output=True,
    )
    response = analysis.relative_response(baseline, boosted, rig.sample_rate, Q_GRID_HZ,
                                          test_column=OUTPUT_CHANNEL, reference_column=REFERENCE_CHANNEL)
    measured = np.array([level for _phase, level in response])

    errors = [float(np.sqrt(np.mean((_model_db(q) - measured) ** 2))) for q in Q_CANDIDATES]
    fitted_q = float(Q_CANDIDATES[int(np.argmin(errors))])
    shape_rms = float(np.sqrt(np.mean((_model_db(stored_q) - measured) ** 2)))
    print(f"EQ Q fit: configured Q={configured_q}, stored Q={stored_q:.3f}, fitted Q={fitted_q:.2f}, "
          f"rms vs stored-Q model={shape_rms:.2f} dB")
    relative_error = abs(fitted_q - stored_q) / stored_q
    assert relative_error < Q_RELATIVE_TOLERANCE, (
        f"fitted Q={fitted_q:.2f} vs stored {stored_q:.3f}: relative error {relative_error:.2f} "
        f"exceeds {Q_RELATIVE_TOLERANCE}"
    )
    assert shape_rms < SHAPE_RMS_TOLERANCE_DB, f"peak shape is {shape_rms:.2f} dB rms from the RBJ model"
    print("CONFIRMED: measured peak shape matches the RBJ model at the stored Q")


def test_50_output_eq_boost_cut_tracks_configured_gain(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _gain_check(rig, output=True)


def test_51_input_eq_boost_cut_tracks_configured_gain(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _gain_check(rig, output=False)


def test_52_output_eq_q_matches_the_rbj_model(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _q_check(rig)
