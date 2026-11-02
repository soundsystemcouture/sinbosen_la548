"""Live confirmation of set_phase's polarity via cross-correlation sign, for
both channels on both directions. Also cross-validates the channel_id <->
capture-column mapping via a third, independent technique (sign flip instead
of mute/frequency or level tracking).

Uses white noise rather than a pure tone deliberately: a pure sine's
autocorrelation is itself periodic, so cross_correlate_delay's argmax-of-|corr|
search can lock onto the wrong lag (any half-period offset looks like a peak of
the same magnitude, alternating sign). White noise has one clean, unambiguous
correlation peak at the true path delay.

The baseline ("normal") state is *not* assumed to correlate positively. On
hardware, with input and output phase both at their default `False`, the
loopback correlates *negatively*, and toggling either field alone flips it
positive (two independent inversions on top of a baseline already inverted
once); the loopback interface may add an inversion of its own too. What's
actually being confirmed here is the *relative* flip - toggling one field
inverts the sign of exactly one capture column, whatever that column's
starting sign was - not an absolute "normal must be positive" assumption.
"""

from __future__ import annotations

import time

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

SIGNAL_DURATION_S = 2.0
COEFF_THRESHOLD = 0.7
MAX_MEASURE_ATTEMPTS = 3


def _measure_coeffs(rig: Rig, noise: np.ndarray, reference: np.ndarray) -> list[float]:
    """A single measurement occasionally comes back with a weaker-than-usual
    magnitude (never a sign flip - confirmed by repeated measurement on
    hardware) due to a write/capture-timing transient. Retry a few times rather than fail on a known noise artifact -
    a genuine encoding problem would show up as a wrong *sign*, which this
    retry does nothing to hide."""
    coeffs: list[float] = []
    for attempt in range(MAX_MEASURE_ATTEMPTS):
        captured = rig.measure(noise)
        coeffs = [analysis.cross_correlate_delay(reference, captured[:, i], rig.sample_rate)[1] for i in range(2)]
        if max(abs(c) for c in coeffs) > COEFF_THRESHOLD:
            return coeffs
        print(f"  (attempt {attempt + 1}/{MAX_MEASURE_ATTEMPTS}: weak magnitude {coeffs}, retrying)")
    return coeffs


def _phase_flip_check(rig: Rig, channel_id: int, *, output: bool) -> None:
    label = f"{'output' if output else 'input'} {channel_id}"
    noise = signal_gen.white_noise(SIGNAL_DURATION_S, sample_rate=rig.sample_rate, seed=0)
    reference = noise[:, 0]

    normal_coeffs = _measure_coeffs(rig, noise, reference)
    print(f"{label} phase=normal: coeffs={[f'{c:.2f}' for c in normal_coeffs]}")
    assert max(abs(c) for c in normal_coeffs) > COEFF_THRESHOLD, (
        f"no capture channel correlated strongly (either sign) at baseline: {normal_coeffs}"
    )

    try:
        rig.client.set_phase(channel_id, True, output=output)
        time.sleep(rig.settle_s)
        inverted_coeffs = _measure_coeffs(rig, noise, reference)
        print(f"{label} phase=inverted: coeffs={[f'{c:.2f}' for c in inverted_coeffs]}")

        flipped = [
            i
            for i in range(2)
            if abs(normal_coeffs[i]) > COEFF_THRESHOLD
            and abs(inverted_coeffs[i]) > COEFF_THRESHOLD
            and (normal_coeffs[i] > 0) != (inverted_coeffs[i] > 0)
        ]
        assert len(flipped) == 1, (
            f"expected exactly one capture channel to flip sign when inverting {label}, "
            f"normal={normal_coeffs} inverted={inverted_coeffs}"
        )
        flipped_channel = flipped[0]
        steady_channel = 1 - flipped_channel
        steady_ok = abs(inverted_coeffs[steady_channel]) > COEFF_THRESHOLD and (
            inverted_coeffs[steady_channel] > 0
        ) == (normal_coeffs[steady_channel] > 0)
        assert steady_ok, (
            f"the untouched capture channel's correlation sign should stay the same, "
            f"normal={normal_coeffs} inverted={inverted_coeffs}"
        )
        print(f"CONFIRMED: {label} -> capture column {flipped_channel} (phase=True inverts polarity)")
    finally:
        rig.client.set_phase(channel_id, False, output=output)


def test_20_input_phase_channel_0(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _phase_flip_check(rig, 0, output=False)


def test_21_input_phase_channel_1(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _phase_flip_check(rig, 1, output=False)


def test_22_output_phase_channel_0(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _phase_flip_check(rig, 0, output=True)


def test_23_output_phase_channel_1(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _phase_flip_check(rig, 1, output=True)
