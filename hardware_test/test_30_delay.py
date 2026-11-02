"""Live confirmation that set_delay's configured ms matches the measured
time-of-flight delta via cross-correlation lag, for both an input and an
output channel, straddling the delay codec's 49-50 ms piecewise-formula
boundary. Also cross-validates the channel_id <-> capture-column mapping via
a fourth, independent technique (lag tracking instead of mute/frequency,
level, or polarity).

Uses white noise for the same reason test_20_phase.py does: a pure tone's
periodic autocorrelation can make cross_correlate_delay lock onto the wrong
half-period lag, whereas white noise has one unambiguous peak at the true
path delay.

Raw measured lag includes a large, constant offset that has nothing to do
with the LA548 - audio_io.py's startup_settle_s + pre_roll_s (~0.8s) of
silence recorded before playback even starts, baked into the front of every
capture. That's fine since it's identical on every step and cancels out of
any delta.

Observed on the device: the *other*, untouched channel's absolute lag is not
perfectly fixed either - it shifts by a small, roughly constant amount the
moment any non-zero delay is introduced anywhere in the box (consistent with
a shared delay-line buffer whose overall size/latency depends on the current
max delay across channels, not just the swept one). That rules out an
absolute "the untouched channel's lag must stay put" isolation check. Instead this
compares the *differential* (swept channel's lag minus the untouched
channel's lag) against configured ms - any shared drift cancels out of the
subtraction, while a genuine isolation bug (both channels moving by the same
amount as configured ms) would still show up as a flat, non-tracking
differential.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

SIGNAL_DURATION_S = 1.0
COEFF_THRESHOLD = 0.6
MAX_MEASURE_ATTEMPTS = 3
DELAY_STEPS_MS = (0.0, 25.0, 49.5, 50.0, 75.0, 300.0, 1000.0)
VARYING_MIN_RANGE_MS = 20.0
# The delay codec steps 0.01 ms below 50 ms and 1 ms above, and the lag
# resolution is one sample (0.02 ms), so a 5 ms tolerance could pass a wrong
# formula branch. On hardware the worst error was 0.15 ms, at 1000 ms, growing
# with delay (~150 ppm, consistent with the clock offset between the LA548 and
# the loopback interface).
TRACKING_TOLERANCE_MS = 0.5


def _post_roll_s(ms: float) -> float:
    """Long enough that the delayed noise burst always lands fully inside the
    captured window, even at the top of the 0-1200 ms range (see audio_io.py's
    pre_roll_s/post_roll_s docstring)."""
    return max(1.0, ms / 1000.0 + 0.5)


def _measure_lags(
    rig: Rig, write_fn: Callable[[], Any], post_roll_s: float, noise: np.ndarray, reference: np.ndarray
) -> tuple[list[float], list[float]]:
    """A single measurement occasionally comes back with a weak (or even
    sign-flipped) correlation on one column due to a write/capture-timing
    transient - the same class of noise test_20_phase.py's _measure_coeffs
    already characterizes, just showing up here as a lag-detection failure
    instead of a polarity failure. Retry a few times rather than fail on a
    known artifact."""
    lags = [0.0, 0.0]
    coeffs = [0.0, 0.0]
    for attempt in range(MAX_MEASURE_ATTEMPTS):
        captured = rig.write_then_measure(write_fn, noise, post_roll_s=post_roll_s)
        for i in range(2):
            lag_s, coeff = analysis.cross_correlate_delay(reference, captured[:, i], rig.sample_rate)
            lags[i] = lag_s * 1000.0
            coeffs[i] = coeff
        if min(abs(c) for c in coeffs) > COEFF_THRESHOLD:
            return lags, coeffs
        formatted = [f"{c:.2f}" for c in coeffs]
        print(f"  (attempt {attempt + 1}/{MAX_MEASURE_ATTEMPTS}: weak correlation {formatted}, retrying)")
    return lags, coeffs


def _sweep_and_identify(rig: Rig, channel_id: int, *, output: bool) -> None:
    label = f"{'output' if output else 'input'} {channel_id}"
    noise = signal_gen.white_noise(SIGNAL_DURATION_S, sample_rate=rig.sample_rate, seed=0)
    reference = noise[:, 0]

    lags_ms: list[list[float]] = [[], []]
    for ms in DELAY_STEPS_MS:
        write_fn = functools.partial(rig.client.set_delay, channel_id, ms, output=output)
        lags, coeffs = _measure_lags(rig, write_fn, _post_roll_s(ms), noise, reference)
        assert min(abs(c) for c in coeffs) > COEFF_THRESHOLD, (
            f"{label} delay={ms:g} ms: correlation too weak on one or both capture channels: {coeffs}"
        )
        lags_ms[0].append(lags[0])
        lags_ms[1].append(lags[1])
        print(f"{label} delay={ms:g} ms -> ch0 lag={lags[0]:.2f} ms, ch1 lag={lags[1]:.2f} ms")

    ranges = [max(col) - min(col) for col in lags_ms]
    varying = 0 if ranges[0] > ranges[1] else 1
    steady = 1 - varying

    assert ranges[varying] > VARYING_MIN_RANGE_MS, (
        f"no capture channel showed meaningful lag variation: ranges={ranges}"
    )

    zero_index = DELAY_STEPS_MS.index(0.0)
    diffs_ms = [lags_ms[varying][j] - lags_ms[steady][j] for j in range(len(DELAY_STEPS_MS))]
    base_diff = diffs_ms[zero_index]
    for ms, diff in zip(DELAY_STEPS_MS, diffs_ms, strict=True):
        expected = base_diff + ms
        assert abs(diff - expected) < TRACKING_TOLERANCE_MS, (
            f"{label} delay={ms:g} ms: expected differential ~{expected:.2f} ms (base={base_diff:.2f} ms), "
            f"measured {diff:.2f} ms"
        )

    print(f"CONFIRMED: {label} -> capture column {varying}, base differential={base_diff:.2f} ms")


def test_30_input_delay_tracks_configured_ms(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _sweep_and_identify(rig, 0, output=False)


def test_31_output_delay_tracks_configured_ms(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _sweep_and_identify(rig, 0, output=True)
