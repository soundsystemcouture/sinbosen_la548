"""Live confirmation of set_eq_band's AllPass behavior - the feature this
whole write path exists for (subwoofer time/phase alignment).

The EQ `type` values follow the manufacturer's app's type list `EQ,
LowShelf, HighShelf, AllPass1, AllPass2, Notch`, so AllPass is `type=3`
(first order) and `type=4` (second order).

**Measurement method.** A pure tone can't measure an allpass: a phase shift
on a tone is indistinguishable from a time shift, so re-aligning each capture
with an argmax-|correlation| lag search absorbs whatever phase the filter
added and the readout can only land on ~0 or ~180 deg - enough for the sign
flip at the centre of a 2nd-order allpass, but not the -90 deg of a
1st-order one or any off-centre phase.

Instead, identical white noise goes to both inputs; output 1 carries the
allpass and output 2 is an untouched reference path in the *same* capture,
so start jitter and path latency cancel without any lag search
(`analysis.relative_response`). A baseline capture with the band disabled
removes the static difference between the two paths. The phase change at
centre/2, centre and 2x centre is compared with the RBJ model in
`sinbosen_la548.response.eq_response`.

Passes on hardware: every reading at 200, 1000 and 1500 Hz, both types, was
within 0.4 deg of the model and 0.06 dB of flat.
"""

from __future__ import annotations

import time

import numpy as np

from sinbosen_la548.response import eq_response
from sinbosen_la548.settings import DA0408X, EQBandSettings

from . import analysis, signal_gen
from .rig import Rig

NOISE_DURATION_S = 4.0
NOISE_AMPLITUDE = 0.2
MAG_TOLERANCE_DB = 1.0  # an allpass must not change level
PHASE_TOLERANCE_DEG = 5.0  # worst case on hardware was 0.4 deg
ENGAGED_MIN_DEG = 45.0  # below this at the centre, the enable write did not take
MAX_ENABLE_ATTEMPTS = 3

OUTPUT_CHANNEL = 0  # carries the allpass
REFERENCE_CHANNEL = 1  # untouched path in the same capture
BAND_INDEX = 0
Q = 1.0


def _model_phase_deg(allpass_type: int, center_hz: float, at_hz: tuple[float, ...]) -> list[float]:
    band = EQBandSettings(int(center_hz), 0.0, Q, 0, allpass_type, True, raw=b"")
    response = eq_response(band, np.asarray(at_hz, dtype=np.float64), sample_rate_hz=DA0408X.sample_rate_hz)
    return [float(v) for v in np.degrees(np.angle(response.transfer))]


def _wrapped_diff_deg(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _capture(rig: Rig, enabled: bool, allpass_type: int, center_hz: float) -> np.ndarray:
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=int(center_hz), gain_db=0.0, q=Q,
        type=allpass_type if enabled else 0, slope=0, enabled=enabled, output=True,
    )
    time.sleep(rig.settle_s)
    noise = signal_gen.white_noise(NOISE_DURATION_S, amplitude=NOISE_AMPLITUDE, sample_rate=rig.sample_rate, seed=0)
    return rig.measure(noise, post_roll_s=0.3)


def _allpass_check(rig: Rig, allpass_type: int, center_hz: float) -> None:
    at_hz = (center_hz / 2, center_hz, center_hz * 2)
    baseline = _capture(rig, False, allpass_type, center_hz)
    # A normal ACK has occasionally left the band visibly disengaged
    # (~1 in 5-10 enables, both readings clean), so retry the enable write
    # itself when the centre shows no phase change.
    for attempt in range(MAX_ENABLE_ATTEMPTS):
        measured = analysis.relative_response(baseline, _capture(rig, True, allpass_type, center_hz),
                                              rig.sample_rate, at_hz, test_column=OUTPUT_CHANNEL,
                                              reference_column=REFERENCE_CHANNEL)
        if abs(measured[1][0]) > ENGAGED_MIN_DEG:
            break
        print(f"  (attempt {attempt + 1}/{MAX_ENABLE_ATTEMPTS}: allpass did not engage, retrying the enable)")
    else:
        raise AssertionError(f"AllPass type {allpass_type} never engaged after {MAX_ENABLE_ATTEMPTS} enables")

    expected = _model_phase_deg(allpass_type, center_hz, at_hz)
    for freq, (phase, level), want in zip(at_hz, measured, expected, strict=True):
        print(f"type {allpass_type} @ {center_hz:.0f} Hz: {freq:.0f} Hz -> {phase:+.1f} deg "
              f"(model {want:+.1f}), level {level:+.2f} dB")
        assert abs(level) < MAG_TOLERANCE_DB, f"allpass changed level at {freq:.0f} Hz by {level:+.2f} dB"
        assert abs(_wrapped_diff_deg(phase, want)) < PHASE_TOLERANCE_DEG, (
            f"type {allpass_type} at {freq:.0f} Hz: measured {phase:+.1f} deg, model {want:+.1f} deg"
        )


def test_60_output_second_order_allpass_matches_model_at_1000hz(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _allpass_check(rig, 4, 1000.0)


def test_61_output_second_order_allpass_tracks_configured_center_freq(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _allpass_check(rig, 4, 1500.0)


def test_62_output_first_order_allpass_matches_model_at_1000hz(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _allpass_check(rig, 3, 1000.0)


def test_63_output_second_order_allpass_at_subwoofer_crossover_200hz(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _allpass_check(rig, 4, 200.0)
