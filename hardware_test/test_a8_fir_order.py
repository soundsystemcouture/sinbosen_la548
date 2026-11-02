"""Live investigation of FIR's `order_index` field (module_id=49, the FIR
block's tap-count selector).

On this model the manufacturer's app offers two tap counts, 256 and 512
(`order_index` 0 and 1), but the device can't report which tap count a raw
value actually selects. So this test doesn't grade the result against an
expected number of taps - the achievable, honest goal is: does
`order_index` measurably affect the filter's steepness at all, and does
every value still produce a working low-pass? Either steepness result is a
real, reportable finding; this file characterizes it.

On hardware `order_index=0` measured a real, reproducible difference from 1
(roughly double the transition width, ~6 dB less deep stopband), consistent
with a smaller tap count. Indices 2 and 3 measured identical to 1;
`Client.set_fir` rejects them locally, so only 0 and 1 are exercised.
"""

from __future__ import annotations

import time

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

OUTPUT_CHANNEL = 0
CORNER_HZ = 2000
FAR_HZ = 20000  # the ignored edge for fir_type=0 (low-pass), see test_a3
WIN_TYPE = 0  # held constant - not what this file investigates, see test_a7
ATTENUATION_DB = 21.0
SWEEP_F0_HZ = 20.0
SWEEP_F1_HZ = 20000.0
SWEEP_DURATION_S = 4.0
SWEEP_AMPLITUDE = 0.2
CAPTURE_POST_ROLL_S = 1.0
COEFF_THRESHOLD = 0.3  # see test_40_xover.py
MAX_MEASURE_ATTEMPTS = 3  # see test_30_delay.py/test_60_allpass.py - bounded retry on a weak/transient capture

PASSBAND_HZ = (200.0, 800.0)
DEEP_STOPBAND_HZ = (8000.0, 9000.0)
MIN_STOPBAND_DROP_DB = 20.0
CORNER_TOLERANCE_OCTAVES = 1.5  # generous - this is a sanity check, not a precision measurement

ORDER_INDICES = (0, 1)  # Client.set_fir rejects 2 and 3


def _reference_and_sweep(sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    sweep = signal_gen.log_sweep(
        SWEEP_F0_HZ, SWEEP_F1_HZ, SWEEP_DURATION_S, amplitude=SWEEP_AMPLITUDE, sample_rate=sample_rate
    )
    return sweep, sweep[:, 0]


def _band_avg_db(freqs: np.ndarray, mag_db: np.ndarray, f_lo: float, f_hi: float) -> float:
    mask = (freqs >= f_lo) & (freqs <= f_hi)
    return float(np.mean(mag_db[mask]))


def _find_threshold_crossing(
    freqs: np.ndarray, mag_db: np.ndarray, ref_level: float, drop_db: float, *, min_freq_hz: float = SWEEP_F0_HZ
) -> float:
    """First ascending-frequency point (at or above min_freq_hz) where mag_db falls to
    ref_level - drop_db. Takes an arbitrary drop, so it can locate both a -3 dB corner and a
    -30 dB deep-stopband point from the same sweep. min_freq_hz defaults to the sweep's own
    low edge: the FFT-ratio's near-DC bins are noisy (little real signal energy that low in a
    log sweep starting at SWEEP_F0_HZ) and can spuriously satisfy a "dropped below threshold"
    check at ~0 Hz, which a plain `freqs > 0` guard does not exclude."""
    threshold = ref_level - drop_db
    above_floor = freqs >= min_freq_hz
    f = freqs[above_floor]
    m = mag_db[above_floor]
    condition = m <= threshold
    if not np.any(condition):
        raise ValueError(f"never dropped {drop_db:.0f} dB below {ref_level:.1f} dB reference in sweep range")
    idx = int(np.argmax(condition))
    return float(f[idx])


def _measure_response(
    rig: Rig, order_index: int, sweep: np.ndarray, reference: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    def write_fn() -> None:
        rig.client.set_fir(
            OUTPUT_CHANNEL,
            enabled=True,
            fir_type=0,
            win_type=WIN_TYPE,
            freq_low_hz=CORNER_HZ,
            freq_high_hz=FAR_HZ,
            attenuation_db=ATTENUATION_DB,
            order_index=order_index,
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
    return freqs, mag_db


def _flat(rig: Rig) -> None:
    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=False, fir_type=0, win_type=WIN_TYPE,
        freq_low_hz=CORNER_HZ, freq_high_hz=CORNER_HZ, attenuation_db=ATTENUATION_DB, order_index=1, output=True,
    )


def test_a8_order_index_sanity_and_steepness_characterization(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    sweep, reference = _reference_and_sweep(rig.sample_rate)

    steepness_octaves: dict[int, float] = {}
    for order_index in ORDER_INDICES:
        freqs, mag_db = _measure_response(rig, order_index, sweep, reference)
        ref_level = _band_avg_db(freqs, mag_db, *PASSBAND_HZ)
        deep_stopband_level = _band_avg_db(freqs, mag_db, *DEEP_STOPBAND_HZ)

        stopband_drop = ref_level - deep_stopband_level
        print(f"order_index={order_index}: stopband_drop={stopband_drop:.2f} dB")
        assert stopband_drop > MIN_STOPBAND_DROP_DB, (
            f"order_index={order_index}: expected a real working low-pass, "
            f"stopband only dropped {stopband_drop:.2f} dB"
        )

        corner_freq = _find_threshold_crossing(freqs, mag_db, ref_level, 3.0)
        octaves_off = abs(np.log2(corner_freq / CORNER_HZ))
        assert octaves_off < CORNER_TOLERANCE_OCTAVES, (
            f"order_index={order_index}: measured corner {corner_freq:.0f} Hz is "
            f"{octaves_off:.2f} octaves from configured {CORNER_HZ} Hz"
        )

        deep_freq = _find_threshold_crossing(freqs, mag_db, ref_level, 30.0)
        steepness_octaves[order_index] = abs(np.log2(deep_freq / corner_freq))
        print(
            f"order_index={order_index}: -3dB at {corner_freq:.0f} Hz, -30dB at {deep_freq:.0f} Hz, "
            f"steepness={steepness_octaves[order_index]:.2f} octaves"
        )

    _flat(rig)
    print("CHARACTERIZATION (not graded against an expected tap count):")
    print(f"steepness by order_index = {steepness_octaves}")
    distinct_values = {round(v, 2) for v in steepness_octaves.values()}
    if len(distinct_values) == 1:
        print(
            f"REPORT: steepness is identical across every order_index tried {ORDER_INDICES} - consistent with "
            "this unit having only one real tap-count option, or every value tried clamping to it; "
            "this is an honest finding, not a test failure"
        )
    else:
        # The result on hardware (see the module docstring): order_index=0 is measurably less
        # steep than 1, consistent with a smaller tap count - not proof of an exact tap count.
        print(f"REPORT: steepness is NOT identical across every order_index tried - distinct values: {distinct_values}")
    print("CONFIRMED: every order_index value tried still produces a real, working low-pass on this unit")
