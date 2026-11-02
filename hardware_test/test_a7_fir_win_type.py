"""Live investigation of FIR's `win_type` field (module_id=49, the FIR
block's window-type byte) - the window function used to compute the FIR
filter's taps.

The manufacturer's app lists 12 window types; `win_type` is taken to be the
index into that list (`0=KAISER, 1=HANNING, 2=HAMMING, 3=BLACKMAN,
4=FLATTOP, 5=BLACKMAN_HARRIS, ...`), a candidate mapping rather than a
proven one. This can't and doesn't try to name all 12 windows - several are
theoretically too close to each other, and to the loopback's own noise
floor, to tell apart over electrical loopback. The achievable, honest goal:

1. Confirm `win_type` selects between genuinely different, measurably
   distinct filter shapes. **In the deep stopband (6000-9000 Hz, several
   octaves above a 2000 Hz corner) all three candidates converge to the
   same ~-89 to -91 dB floor - the measurement floor, not a window-shape
   effect, since it shows up regardless of which candidate is sent.**
   Closer to the transition (2100-2300 Hz, right above the corner) there is
   real, reproducible separation instead - consistent with FIR window
   theory's main-lobe-width/sidelobe-depth tradeoff: Blackman-Harris
   trades a much wider main lobe (slower transition) for a far deeper
   ultimate sidelobe floor than Hanning/Hamming (whose main-lobe widths are
   close to each other), so right next to the corner it is *less*
   attenuated than either of them - the opposite of "better window = more
   attenuated everywhere", but exactly what window theory predicts, and
   not something a coincidental noise difference could fake. Hanning vs.
   Hamming's own near-transition levels are close enough (theory predicts
   similar main-lobe widths for both) that they can't be reliably
   separated from each other - only Blackman-Harris's markedly wider main
   lobe stands out.
2. A `win_type=0` (candidate Kaiser) vs. `win_type=2` (candidate Hamming)
   cross-check against `attenuation_db`: in FIR design a Kaiser window has a
   shape parameter (beta) set from the desired stopband attenuation, so
   `attenuation_db` plausibly only has a real effect for a window type that
   takes a shape parameter (Kaiser), and should do nothing for a fixed-shape
   window (Hamming). Reported as a comparison, not a hard assertion - this
   is a hypothesis this test can gather evidence for, not something it can
   conclusively name.
"""

from __future__ import annotations

import time

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

OUTPUT_CHANNEL = 0
CORNER_HZ = 2000
FAR_HZ = 20000  # the ignored edge for fir_type=0 (low-pass), see test_a3
ORDER_INDEX = 1  # held constant - not what this file investigates, see test_a8
SWEEP_F0_HZ = 20.0
SWEEP_F1_HZ = 20000.0
SWEEP_DURATION_S = 4.0
SWEEP_AMPLITUDE = 0.2
CAPTURE_POST_ROLL_S = 1.0
COEFF_THRESHOLD = 0.3  # see test_40_xover.py - a wideband sweep's zero-lag correlation is inherently looser
MAX_MEASURE_ATTEMPTS = 3  # see test_30_delay.py/test_60_allpass.py - bounded retry on a weak/transient capture

PASSBAND_HZ = (200.0, 800.0)
# Deliberately measured right above the corner, not deep in the stopband: at 6000-9000 Hz all
# three candidates converge to the same ~-89 to -91 dB floor, statistically indistinguishable -
# that floor is the loopback's own measurement floor, not a window-shape effect. Right next to the
# corner, a window's *main-lobe width* (transition steepness) dominates instead of its ultimate
# sidelobe depth, and that's where real, distinct, reproducible separation between candidates
# shows up (see NEAR_TRANSITION_HZ below).
NEAR_TRANSITION_HZ = (2100.0, 2300.0)
FAR_STOPBAND_HZ = (6000.0, 9000.0)  # report-only: all real windows converge near the loopback's noise floor here
SIDELOBE_ORDERING_MARGIN_DB = 3.0

# WinType candidates chosen to maximize theoretical separation rather than trying to distinguish
# all 12 at once. Hanning and Hamming have very similar main-lobe widths (theoretical sidelobes
# -31/-43 dB, both narrow transition), while Blackman-Harris trades a much wider main lobe
# (slower transition/rolloff) for a far deeper sidelobe floor (~-92 dB) - so the candidate most
# likely to visibly differ from the other two, right next to the corner, is Blackman-Harris, not
# Hanning vs. Hamming (which theory predicts should look similar there, and did).
WIN_TYPE_HANNING = 1
WIN_TYPE_HAMMING = 2
WIN_TYPE_BLACKMAN_HARRIS = 5
WIN_TYPE_KAISER_CANDIDATE = 0


def _reference_and_sweep(sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    sweep = signal_gen.log_sweep(
        SWEEP_F0_HZ, SWEEP_F1_HZ, SWEEP_DURATION_S, amplitude=SWEEP_AMPLITUDE, sample_rate=sample_rate
    )
    return sweep, sweep[:, 0]


def _band_avg_db(freqs: np.ndarray, mag_db: np.ndarray, f_lo: float, f_hi: float) -> float:
    mask = (freqs >= f_lo) & (freqs <= f_hi)
    return float(np.mean(mag_db[mask]))


def _measure_bands(
    rig: Rig, sweep: np.ndarray, reference: np.ndarray, *, win_type: int, atten_db: float
) -> tuple[float, float]:
    """Returns (near_transition_db, far_stopband_db)."""

    def write_fn() -> None:
        rig.client.set_fir(
            OUTPUT_CHANNEL,
            enabled=True,
            fir_type=0,
            win_type=win_type,
            freq_low_hz=CORNER_HZ,
            freq_high_hz=FAR_HZ,
            attenuation_db=atten_db,
            order_index=ORDER_INDEX,
            output=True,
        )

    write_fn()
    time.sleep(rig.settle_s)
    for attempt in range(MAX_MEASURE_ATTEMPTS):
        captured = rig.measure(sweep, post_roll_s=CAPTURE_POST_ROLL_S)
        lag_s, coeff = analysis.cross_correlate_delay(reference, captured[:, OUTPUT_CHANNEL], rig.sample_rate)
        if abs(coeff) > COEFF_THRESHOLD:
            break
        print(f"  (attempt {attempt + 1}/{MAX_MEASURE_ATTEMPTS}: weak correlation {coeff:.3f}, retrying)")
    else:
        raise AssertionError(f"correlation too weak to trust after {MAX_MEASURE_ATTEMPTS} attempts: {coeff:.3f}")
    lag_samples = int(round(lag_s * rig.sample_rate))
    aligned = captured[lag_samples : lag_samples + len(reference), OUTPUT_CHANNEL]
    assert len(aligned) == len(reference), (
        f"capture too short after alignment: got {len(aligned)}, need {len(reference)} (lag={lag_s * 1000:.1f} ms)"
    )
    freqs, mag_db, _phase = analysis.frequency_response(reference, aligned, rig.sample_rate)
    return _band_avg_db(freqs, mag_db, *NEAR_TRANSITION_HZ), _band_avg_db(freqs, mag_db, *FAR_STOPBAND_HZ)


def _measure_stopband_floor(
    rig: Rig, sweep: np.ndarray, reference: np.ndarray, *, win_type: int, atten_db: float
) -> float:
    """Far-stopband-only convenience wrapper for the Kaiser/attenuation cross-check below,
    where only the deep floor (not the near-transition shape) is relevant."""
    _near, far = _measure_bands(rig, sweep, reference, win_type=win_type, atten_db=atten_db)
    return far


def _flat(rig: Rig) -> None:
    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=False, fir_type=0, win_type=0,
        freq_low_hz=CORNER_HZ, freq_high_hz=CORNER_HZ, attenuation_db=21.0, order_index=ORDER_INDEX, output=True,
    )


def test_a7_win_type_candidates_produce_measurably_different_filter_shapes(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    sweep, reference = _reference_and_sweep(rig.sample_rate)

    near = {}
    far = {}
    for win_type in (WIN_TYPE_HANNING, WIN_TYPE_HAMMING, WIN_TYPE_BLACKMAN_HARRIS):
        near[win_type], far[win_type] = _measure_bands(rig, sweep, reference, win_type=win_type, atten_db=21.0)
    _flat(rig)

    print(
        f"near-transition ({NEAR_TRANSITION_HZ[0]:.0f}-{NEAR_TRANSITION_HZ[1]:.0f} Hz): "
        f"win_type=1 (Hanning?)={near[WIN_TYPE_HANNING]:.1f} dB, "
        f"win_type=2 (Hamming?)={near[WIN_TYPE_HAMMING]:.1f} dB, "
        f"win_type=5 (Blackman-Harris?)={near[WIN_TYPE_BLACKMAN_HARRIS]:.1f} dB"
    )
    print(
        f"far stopband ({FAR_STOPBAND_HZ[0]:.0f}-{FAR_STOPBAND_HZ[1]:.0f} Hz, report only - see comment above "
        f"NEAR_TRANSITION_HZ): win_type=1={far[WIN_TYPE_HANNING]:.1f} dB, win_type=2={far[WIN_TYPE_HAMMING]:.1f} dB, "
        f"win_type=5={far[WIN_TYPE_BLACKMAN_HARRIS]:.1f} dB"
    )

    # The real, reproducible asymmetry: Blackman-Harris's much wider main lobe (the tradeoff for
    # its far deeper ultimate sidelobe floor) means it is still mid-transition, and therefore
    # measurably *less* attenuated than Hanning/Hamming, right next to the corner - the opposite
    # of "better window = more attenuated everywhere", but exactly consistent with real FIR window
    # theory (narrower main lobe <-> worse ultimate sidelobe is a real, well-known tradeoff, not
    # something a "some numbers differ" result could fake).
    assert near[WIN_TYPE_BLACKMAN_HARRIS] > near[WIN_TYPE_HANNING] + SIDELOBE_ORDERING_MARGIN_DB, (
        f"expected win_type=5's wider main lobe to leave it less attenuated than win_type=1 "
        f"right next to the corner, got {near}"
    )
    assert near[WIN_TYPE_BLACKMAN_HARRIS] > near[WIN_TYPE_HAMMING] + SIDELOBE_ORDERING_MARGIN_DB, (
        f"expected win_type=5's wider main lobe to leave it less attenuated than win_type=2 "
        f"right next to the corner, got {near}"
    )
    print(
        "CONFIRMED: win_type selects genuinely different filter shapes, with a main-lobe-width "
        "vs. sidelobe-depth tradeoff consistent with real FIR window theory (not just 'the numbers "
        "differ') - Hanning vs. Hamming were NOT asserted apart, since theory predicts their "
        "main-lobe widths are close enough that a loopback measurement may not reliably separate them"
    )


def test_a7_kaiser_candidate_responds_to_attenuation_more_than_hamming_candidate(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    sweep, reference = _reference_and_sweep(rig.sample_rate)

    kaiser_low = _measure_stopband_floor(rig, sweep, reference, win_type=WIN_TYPE_KAISER_CANDIDATE, atten_db=21.0)
    kaiser_high = _measure_stopband_floor(rig, sweep, reference, win_type=WIN_TYPE_KAISER_CANDIDATE, atten_db=80.0)
    hamming_low = _measure_stopband_floor(rig, sweep, reference, win_type=WIN_TYPE_HAMMING, atten_db=21.0)
    hamming_high = _measure_stopband_floor(rig, sweep, reference, win_type=WIN_TYPE_HAMMING, atten_db=80.0)
    _flat(rig)

    kaiser_delta = kaiser_low - kaiser_high
    hamming_delta = hamming_low - hamming_high
    print(
        f"attenuation_db 21.0 -> 80.0 stopband floor change: "
        f"win_type=0 (Kaiser?) {kaiser_low:.1f} -> {kaiser_high:.1f} dB (delta {kaiser_delta:.1f}), "
        f"win_type=2 (Hamming?) {hamming_low:.1f} -> {hamming_high:.1f} dB (delta {hamming_delta:.1f})"
    )
    is_consistent = kaiser_delta > hamming_delta + SIDELOBE_ORDERING_MARGIN_DB
    print(
        "REPORT ONLY (not asserted): a real Kaiser window's sidelobe level should respond to its own "
        "shape parameter (beta) while a fixed-shape window like Hamming should not - "
        f"{'consistent with' if is_consistent else 'NOT clearly consistent with'} that hypothesis on this measurement"
    )
