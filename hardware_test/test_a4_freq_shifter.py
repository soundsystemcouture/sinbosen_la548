"""Live confirmation of set_freq_shifter (module_id=48, input-only).

`freq_level` and `direction` are raw values whose meaning was settled on
hardware by playing a steady 1 kHz tone and finding the captured FFT peak via
`analysis.peak_frequency`. The frequency-shift effect is clean and
unambiguous:

- `direction` selects the shift's sign: `0` shifts the tone *down*, `1`
  shifts it *up*.
- `freq_level` (0-5, matching the manufacturer's app's 6 preset buttons
  'lev1'..'lev6') scales the shift's magnitude - `0` gives the smallest
  shift, `5` the largest measured here (~9 Hz on a 1 kHz tone at 0.25 Hz FFT
  resolution).

The manufacturer's app never exposes `direction=1`, but the device responds
correctly to it with an upward shift.

Measurement caveat: `analysis.peak_frequency` is a plain FFT-bin argmax, so
its resolution is `1/duration_s` Hz - a 4 s tone gives 0.25 Hz bins, enough
to clearly resolve the ~2-9 Hz shifts measured here, but not precise enough
to pin down an exact Hz-per-level formula (not attempted - this test only
confirms direction/ordering, not a specific numeric mapping).
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

CHANNEL = 0
TONE_FREQ_HZ = 1000
TONE_DURATION_S = 4.0
TONE_AMPLITUDE = 0.2
MIN_SHIFT_HZ = 1.0  # generous vs. the smallest measured shift (~1-2 Hz at level 0)


def _peak_hz(rig: Rig) -> float:
    tone = signal_gen.sine(TONE_FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    n = int(rig.sample_rate * TONE_DURATION_S)
    mid_start = (len(captured) - n) // 2
    return analysis.peak_frequency(captured[mid_start : mid_start + n, CHANNEL], rig.sample_rate)


def _off(rig: Rig) -> None:
    rig.client.set_freq_shifter(CHANNEL, freq_level=0, direction=0, enabled=False)


def test_a4_enabled_toggles_a_measurable_frequency_shift(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _off(rig)
    time.sleep(rig.settle_s)
    baseline_hz = _peak_hz(rig)

    rig.client.set_freq_shifter(CHANNEL, freq_level=5, direction=1, enabled=True)
    time.sleep(rig.settle_s)
    shifted_hz = _peak_hz(rig)

    _off(rig)
    delta = shifted_hz - baseline_hz
    print(f"disabled: {baseline_hz:.2f} Hz, enabled (level=5, dir=1): {shifted_hz:.2f} Hz, delta={delta:+.2f} Hz")
    assert delta > MIN_SHIFT_HZ, f"expected a clear upward frequency shift, measured only {delta:+.2f} Hz"
    print("CONFIRMED: enabling the frequency shifter measurably shifts the tone")


def test_a4_direction_flips_the_shift_sign(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _off(rig)
    time.sleep(rig.settle_s)
    baseline_hz = _peak_hz(rig)

    rig.client.set_freq_shifter(CHANNEL, freq_level=5, direction=1, enabled=True)
    time.sleep(rig.settle_s)
    up_hz = _peak_hz(rig)

    rig.client.set_freq_shifter(CHANNEL, freq_level=5, direction=0, enabled=True)
    time.sleep(rig.settle_s)
    down_hz = _peak_hz(rig)

    _off(rig)
    up_delta = up_hz - baseline_hz
    down_delta = down_hz - baseline_hz
    print(f"baseline={baseline_hz:.2f} Hz, direction=1: {up_hz:.2f} Hz ({up_delta:+.2f}), "
          f"direction=0: {down_hz:.2f} Hz ({down_delta:+.2f})")
    assert up_delta > MIN_SHIFT_HZ, f"expected direction=1 to shift up, measured {up_delta:+.2f} Hz"
    assert down_delta < -MIN_SHIFT_HZ, f"expected direction=0 to shift down, measured {down_delta:+.2f} Hz"
    print("CONFIRMED: direction selects the shift's sign (0=down, 1=up)")


def test_a4_freq_level_scales_the_shift_magnitude(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _off(rig)
    time.sleep(rig.settle_s)
    baseline_hz = _peak_hz(rig)

    rig.client.set_freq_shifter(CHANNEL, freq_level=0, direction=1, enabled=True)
    time.sleep(rig.settle_s)
    small_hz = _peak_hz(rig)

    rig.client.set_freq_shifter(CHANNEL, freq_level=5, direction=1, enabled=True)
    time.sleep(rig.settle_s)
    large_hz = _peak_hz(rig)

    _off(rig)
    small_shift = small_hz - baseline_hz
    large_shift = large_hz - baseline_hz
    print(f"baseline={baseline_hz:.2f} Hz, level=0: {small_hz:.2f} Hz ({small_shift:+.2f}), "
          f"level=5: {large_hz:.2f} Hz ({large_shift:+.2f})")
    assert large_shift > small_shift, (
        f"expected freq_level=5 to shift more than freq_level=0, got {large_shift:+.2f} vs {small_shift:+.2f} Hz"
    )
    print("CONFIRMED: freq_level scales the shift magnitude")
