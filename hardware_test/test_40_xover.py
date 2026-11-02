"""Live confirmation of set_xover's HP/LP corner frequency and slope, using
the whole-struct wire format (see `codecs.xover_struct` and docs/PROTOCOL.md).

Crossover only takes effect when the whole crossover struct is written as one
0xA4 frame with `data_id=0`. Writing one field per frame is ACK'd by the
device but has zero audible effect at any setting - even an unmistakable
1000 Hz/24 dB-per-oct HP corner left a 50 Hz tone untouched - whereas the
whole-struct write of that same HP setting crushed the tone by ~56 dB.
Crossover is also **output-only** on this hardware: an input write of the same
shape produced no stored-state change and no audio effect, so only output
channels are exercised here.

Uses a log-swept sine as reference and measures magnitude response via
FFT-ratio (analysis.frequency_response) after aligning capture-to-reference
lag via cross-correlation.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

SWEEP_F0_HZ = 20.0
SWEEP_F1_HZ = 20000.0
SWEEP_DURATION_S = 4.0
SWEEP_AMPLITUDE = 0.2
CAPTURE_POST_ROLL_S = 1.0
COEFF_THRESHOLD = 0.3  # a wideband sweep's zero-lag correlation is inherently looser than a tone/noise burst's
CORNER_TOLERANCE_OCTAVES = 1.0  # generous first-pass tolerance for the simple-ratio technique
SLOPE_GAP_DB = 3.0  # minimum extra attenuation the steeper slope must show to count as "steeper"

OUTPUT_CHANNEL = 0
HP_TEST_FREQS_HZ = (200, 800, 3000)
LP_TEST_FREQS_HZ = (300, 1500, 6000)


def _reference_and_sweep(sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    sweep = signal_gen.log_sweep(
        SWEEP_F0_HZ, SWEEP_F1_HZ, SWEEP_DURATION_S, amplitude=SWEEP_AMPLITUDE, sample_rate=sample_rate
    )
    return sweep, sweep[:, 0]


def _measure_response(rig: Rig, write_fn: Callable[[], Any], sweep: np.ndarray, reference: np.ndarray) -> np.ndarray:
    captured = rig.write_then_measure(write_fn, sweep, post_roll_s=CAPTURE_POST_ROLL_S)
    lag_s, coeff = analysis.cross_correlate_delay(reference, captured[:, OUTPUT_CHANNEL], rig.sample_rate)
    assert abs(coeff) > COEFF_THRESHOLD, f"correlation too weak to trust lag: {coeff:.2f}"
    lag_samples = int(round(lag_s * rig.sample_rate))
    aligned = captured[lag_samples : lag_samples + len(reference), OUTPUT_CHANNEL]
    assert len(aligned) == len(reference), (
        f"capture too short after alignment: got {len(aligned)}, need {len(reference)} (lag={lag_s * 1000:.1f} ms)"
    )
    _freqs, mag_db, _phase = analysis.frequency_response(reference, aligned, rig.sample_rate)
    return mag_db


def _park_other_band(rig: Rig) -> None:
    """Push the LP corner to the top of its range so it can't interfere with
    an HP measurement (or vice versa isn't needed - LP tests only need HP
    parked low, done inline per test)."""
    rig.client.set_xover(OUTPUT_CHANNEL, "lp", freq_hz=20000, filter_type=0, slope_db_per_oct=6, enabled=True)


def _corner_and_slope_check(rig: Rig, band: str, sweep: np.ndarray, reference: np.ndarray) -> None:
    highpass = band == "hp"
    test_freqs = HP_TEST_FREQS_HZ if highpass else LP_TEST_FREQS_HZ
    freqs = np.fft.rfftfreq(len(reference), d=1.0 / rig.sample_rate)

    for test_freq in test_freqs:
        write_fn = functools.partial(
            rig.client.set_xover,
            OUTPUT_CHANNEL,
            band,  # type: ignore[arg-type]
            enabled=True,
            freq_hz=test_freq,
            filter_type=0,
            slope_db_per_oct=12,
        )
        mag_db = _measure_response(rig, write_fn, sweep, reference)
        # Clamped so it can't collapse (unclamped, HP 3000 Hz would ask for
        # 12000-9000 Hz, an empty band whose NaN average makes the corner
        # unfindable), and the crossing search only walks from the passband into
        # the stopband, so the LP test can't latch onto the parked 10 Hz HP's
        # roll-off below 20 Hz.
        passband = analysis.passband_beyond_corner(test_freq, highpass=highpass)
        measured_corner = analysis.corner_frequency(
            freqs, mag_db, highpass=highpass, passband=passband, search_hz=(SWEEP_F0_HZ, SWEEP_F1_HZ)
        )
        octaves_off = abs(np.log2(measured_corner / test_freq))
        print(f"{band} freq={test_freq} Hz -> measured corner={measured_corner:.0f} Hz ({octaves_off:.2f} octaves off)")
        assert octaves_off < CORNER_TOLERANCE_OCTAVES, (
            f"{band} freq={test_freq} Hz: measured corner {measured_corner:.0f} Hz is "
            f"{octaves_off:.2f} octaves off (tolerance {CORNER_TOLERANCE_OCTAVES})"
        )

    mid_freq = test_freqs[1]
    probe_freq = mid_freq / 2.0 if highpass else mid_freq * 2.0
    slope_levels: dict[int, float] = {}
    for slope in (6, 24):
        write_fn = functools.partial(
            rig.client.set_xover,
            OUTPUT_CHANNEL,
            band,  # type: ignore[arg-type]
            enabled=True,
            freq_hz=mid_freq,
            filter_type=0,
            slope_db_per_oct=slope,
        )
        mag_db = _measure_response(rig, write_fn, sweep, reference)
        idx = int(np.argmin(np.abs(freqs - probe_freq)))
        slope_levels[slope] = float(mag_db[idx])
        print(f"{band} slope={slope} dB/oct -> level at {probe_freq:.0f} Hz = {slope_levels[slope]:.1f} dB")

    assert slope_levels[6] - slope_levels[24] > SLOPE_GAP_DB, (
        f"{band}: slope=24 dB/oct should attenuate {probe_freq:.0f} Hz noticeably more than slope=6, "
        f"got {slope_levels}"
    )
    print(f"CONFIRMED: {band} corner frequency tracks configured Hz and steeper slope attenuates more")


def test_40_output_hp_xover(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _park_other_band(rig)
    sweep, reference = _reference_and_sweep(rig.sample_rate)
    _corner_and_slope_check(rig, "hp", sweep, reference)
    rig.client.set_xover(OUTPUT_CHANNEL, "hp", freq_hz=10, filter_type=0, slope_db_per_oct=6, enabled=False)


def test_41_output_lp_xover(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    rig.client.set_xover(OUTPUT_CHANNEL, "hp", freq_hz=10, filter_type=0, slope_db_per_oct=6, enabled=True)
    sweep, reference = _reference_and_sweep(rig.sample_rate)
    _corner_and_slope_check(rig, "lp", sweep, reference)
    rig.client.set_xover(OUTPUT_CHANNEL, "lp", freq_hz=20000, filter_type=0, slope_db_per_oct=6, enabled=False)
    rig.client.set_xover(OUTPUT_CHANNEL, "hp", freq_hz=10, filter_type=0, slope_db_per_oct=6, enabled=False)
