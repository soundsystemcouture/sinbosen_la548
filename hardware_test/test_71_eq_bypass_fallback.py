"""The working way to turn off EQ on a channel, since `eq_all_bypass`
(opcode 0xAF, test_70_eq_bypass_polarity.py) and `eq_all_reset` (opcode
0xB0, test_72_eq_all_reset.py) have no effect on this firmware.

No other bypass-shaped control is known, and 0xAF stays dead after a power
cycle, so it is a firmware gap rather than a wedged block.

Setting every band's own `enabled=False` via `set_eq_band` reliably
flattens a channel's entire EQ response, even with multiple bands
overlapping and boosted simultaneously. It's not atomic (one write per band,
no single frame does it) and it's band-count-dependent; this test touches
bands 0-4.
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

OUTPUT_CHANNEL = 0
BANDS = (0, 1, 2, 3, 4)
FREQS = (200, 500, 1000, 2000, 4000)
GAIN_DB = 12.0
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2
MAX_RESIDUAL_DB = 1.0  # generous vs. the ~0.04 dB measured - well clear of a real boost (~15-21 dB)


def _dbfs_at(rig: Rig, freq_hz: float) -> float:
    tone = signal_gen.sine(freq_hz, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, OUTPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def _flat(rig: Rig, band: int) -> None:
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, band, freq_hz=1000, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=True
    )


def test_71_disabling_every_band_flattens_the_channel(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    for band in BANDS:
        _flat(rig, band)

    baseline_db = {freq: _dbfs_at(rig, freq) for freq in FREQS}

    for band, freq in zip(BANDS, FREQS, strict=True):
        rig.client.set_eq_band(
            OUTPUT_CHANNEL, band, freq_hz=freq, gain_db=GAIN_DB, q=2.0, type=0, slope=0, enabled=True, output=True
        )
    time.sleep(rig.settle_s)
    boosted_db = {freq: _dbfs_at(rig, freq) for freq in FREQS}
    for freq in FREQS:
        delta = boosted_db[freq] - baseline_db[freq]
        print(f"{freq} Hz boosted: {boosted_db[freq]:.2f} dBFS (delta {delta:+.2f})")
        assert delta > 5.0, f"expected a large boost at {freq} Hz before testing the fallback bypass, got {delta:+.2f}"

    for band, freq in zip(BANDS, FREQS, strict=True):
        rig.client.set_eq_band(
            OUTPUT_CHANNEL, band, freq_hz=freq, gain_db=GAIN_DB, q=2.0, type=0, slope=0, enabled=False, output=True
        )
    time.sleep(rig.settle_s)
    after_db = {freq: _dbfs_at(rig, freq) for freq in FREQS}
    for freq in FREQS:
        residual = after_db[freq] - baseline_db[freq]
        print(f"{freq} Hz after disabling all bands: {after_db[freq]:.2f} dBFS (residual vs baseline {residual:+.2f})")
        assert abs(residual) < MAX_RESIDUAL_DB, (
            f"{freq} Hz: expected the channel to return to baseline after disabling every band, "
            f"residual was {residual:+.2f} dB"
        )

    for band in BANDS:
        _flat(rig, band)
    print("CONFIRMED: setting every band's enabled=False is a working fallback EQ bypass")
