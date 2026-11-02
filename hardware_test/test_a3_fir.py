"""Live confirmation of set_fir (module_id=49, real on both input and output
channels - see `codecs.py`'s `_encode_fir` comment).

`fir_type` 0 is low-pass and 1 is high-pass. Both frequency fields
(`freq_low_hz`/`freq_high_hz`) are raw Hz, and which one acts as the corner
for each type could only be settled on hardware. The result is clean, if
slightly counter to the field names:

- **Low-pass (`fir_type=0`)**: `freq_low_hz` is the actual corner -
  `freq_high_hz` has no measurable effect on it at all.
- **High-pass (`fir_type=1`)**: `freq_high_hz` is the actual corner -
  `freq_low_hz` has no measurable effect on it at all.

(Plausibly this FIR block is really one generic band-pass/band-stop
primitive under the hood, and LP/HP are just the two special cases where
one edge is pushed out past the band of interest - consistent with
`freq_low_hz`/`freq_high_hz` sounding like a passband's two edges rather
than "the LP frequency"/"the HP frequency" - but that's inference, not
confirmed; only the two cases below are actually measured.)

`win_type`/`order_index`/`attenuation_db` aren't exercised here (see
test_a7_fir_win_type.py and test_a8_fir_order.py) - this file confirms that
`fir_type` selects a real, distinct shape and that `enabled` toggles it.
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

OUTPUT_CHANNEL = 0
LOW_FREQ_HZ = 200
HIGH_FREQ_HZ = 5000
CORNER_HZ = 1000
FAR_HZ = 20000  # the "ignored" edge, pushed well past anything measured
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2
MAX_PASSBAND_DROP_DB = 5.0
MIN_STOPBAND_DROP_DB = 20.0


def _dbfs(rig: Rig, freq_hz: float) -> float:
    tone = signal_gen.sine(freq_hz, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, OUTPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def _flat(rig: Rig) -> None:
    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=False, fir_type=0, win_type=0,
        freq_low_hz=CORNER_HZ, freq_high_hz=CORNER_HZ, attenuation_db=21.0, order_index=1, output=True,
    )


def test_a3_fir_enabled_toggles_filtering(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _flat(rig)
    time.sleep(rig.settle_s)
    baseline_high = _dbfs(rig, HIGH_FREQ_HZ)

    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=True, fir_type=0, win_type=0,
        freq_low_hz=CORNER_HZ, freq_high_hz=FAR_HZ, attenuation_db=21.0, order_index=1, output=True,
    )
    time.sleep(rig.settle_s)
    filtered_high = _dbfs(rig, HIGH_FREQ_HZ)

    _flat(rig)
    drop = baseline_high - filtered_high
    print(f"enabled=False: {baseline_high:.2f} dBFS @ {HIGH_FREQ_HZ}Hz; enabled=True (LP): {filtered_high:.2f} dBFS")
    assert drop > MIN_STOPBAND_DROP_DB, f"expected LP to attenuate {HIGH_FREQ_HZ}Hz, got {drop:.2f} dB"
    print("CONFIRMED: enabled toggles real filtering, not inverted")


def test_a3_low_pass_corner_is_freq_low_hz_not_freq_high_hz(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _flat(rig)
    time.sleep(rig.settle_s)
    baseline_low = _dbfs(rig, LOW_FREQ_HZ)
    baseline_high = _dbfs(rig, HIGH_FREQ_HZ)

    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=True, fir_type=0, win_type=0,
        freq_low_hz=CORNER_HZ, freq_high_hz=FAR_HZ, attenuation_db=21.0, order_index=1, output=True,
    )
    time.sleep(rig.settle_s)
    passed_low = _dbfs(rig, LOW_FREQ_HZ)
    cut_high = _dbfs(rig, HIGH_FREQ_HZ)

    _flat(rig)
    pass_drop = baseline_low - passed_low
    cut_drop = baseline_high - cut_high
    print(
        f"LP freq_low_hz={CORNER_HZ}, freq_high_hz={FAR_HZ}: "
        f"{LOW_FREQ_HZ}Hz drop={pass_drop:.2f} dB, {HIGH_FREQ_HZ}Hz drop={cut_drop:.2f} dB"
    )
    assert pass_drop < MAX_PASSBAND_DROP_DB, f"expected {LOW_FREQ_HZ}Hz below corner to pass, dropped {pass_drop:.2f}"
    assert cut_drop > MIN_STOPBAND_DROP_DB, f"expected {HIGH_FREQ_HZ}Hz above corner to be cut, dropped {cut_drop:.2f}"
    print("CONFIRMED: for fir_type=0 (low-pass), freq_low_hz is the real corner")


def test_a3_high_pass_corner_is_freq_high_hz_not_freq_low_hz(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _flat(rig)
    time.sleep(rig.settle_s)
    baseline_low = _dbfs(rig, LOW_FREQ_HZ)
    baseline_high = _dbfs(rig, HIGH_FREQ_HZ)

    rig.client.set_fir(
        OUTPUT_CHANNEL, enabled=True, fir_type=1, win_type=0,
        freq_low_hz=FAR_HZ, freq_high_hz=CORNER_HZ, attenuation_db=21.0, order_index=1, output=True,
    )
    time.sleep(rig.settle_s)
    cut_low = _dbfs(rig, LOW_FREQ_HZ)
    passed_high = _dbfs(rig, HIGH_FREQ_HZ)

    _flat(rig)
    cut_drop = baseline_low - cut_low
    pass_drop = baseline_high - passed_high
    print(
        f"HP freq_low_hz={FAR_HZ}, freq_high_hz={CORNER_HZ}: "
        f"{LOW_FREQ_HZ}Hz drop={cut_drop:.2f} dB, {HIGH_FREQ_HZ}Hz drop={pass_drop:.2f} dB"
    )
    assert cut_drop > MIN_STOPBAND_DROP_DB, f"expected {LOW_FREQ_HZ}Hz below corner to be cut, dropped {cut_drop:.2f}"
    assert pass_drop < MAX_PASSBAND_DROP_DB, f"expected {HIGH_FREQ_HZ}Hz above corner to pass, dropped {pass_drop:.2f}"
    print("CONFIRMED: for fir_type=1 (high-pass), freq_high_hz is the real corner")
