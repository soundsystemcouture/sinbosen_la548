"""Live confirmation that Dynamic EQ actually processes audio - the audible
half of what `test_90_dyneq_wire_format.py` deliberately leaves unconfirmed.

On hardware, the whole-struct `set_dynamic_eq_band` write once reached
device storage (confirmed via `read_group()`, see `test_90`) but produced
zero measured dB change across an extensive parameter sweep (type/threshold/
ratio/startup_mode, both slope values, all 3 bands, 5 center frequencies,
both gain directions). The front panel's own Dynamic EQ on/off switch writes
the same `read_group()` byte this client controls as `enabled`, so there is
no separate hidden gate.

The cause was a wedged DSP processing block, not a protocol bug: the device
is sensitive to malformed/off-protocol packets, and a bad frame can silently
stop one processing block's audio while its parameter storage and ACKs keep
working normally - which is why `read_group()` kept confirming the write
while no audio changed. Power-cycling the unit fixed it immediately: the
identical test then measured a clean +15 dB change, repeatably, on all three
bands.

If this ever fails again with a flat/zero delta, power-cycle the unit before
suspecting a code regression.
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

INPUT_CHANNEL = 0
FREQ_HZ = 1000
GAIN_DB = 15.0
TONE_DURATION_S = 1.5
TONE_AMPLITUDE = 0.2
MIN_EXPECTED_DELTA_DB = 10.0  # generous vs. the ~15 dB configured - well clear of the ~0.3 dB noise floor


def _dbfs_at(rig: Rig, band_index: int) -> float:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, INPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def _flat(rig: Rig, band_index: int) -> None:
    rig.client.set_dynamic_eq_band(
        INPUT_CHANNEL, band_index, freq_hz=FREQ_HZ, gain_db=0.0, qvalue=1.0, slope=0, type=0, startup_mode=0,
        threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False,
    )


def _boosted(rig: Rig, band_index: int) -> None:
    rig.client.set_dynamic_eq_band(
        INPUT_CHANNEL, band_index, freq_hz=FREQ_HZ, gain_db=GAIN_DB, qvalue=1.0, slope=0, type=0, startup_mode=0,
        threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=10.0, enabled=True,
    )


def test_91_dynamic_eq_band_produces_configured_gain_change(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    for band in (0, 1, 2):
        _flat(rig, band)
        baseline_db = _dbfs_at(rig, band)

        _boosted(rig, band)
        time.sleep(rig.settle_s)
        boosted_db = _dbfs_at(rig, band)

        delta = boosted_db - baseline_db
        print(f"band {band}: baseline={baseline_db:.2f} dBFS, boosted={boosted_db:.2f} dBFS, delta={delta:+.2f} dB")
        assert delta > MIN_EXPECTED_DELTA_DB, (
            f"band {band}: expected roughly +{GAIN_DB} dB, measured {delta:+.2f} dB - "
            "if this is ~0 dB, try power-cycling the unit before assuming a code regression "
            "(see this file's docstring)"
        )

        _flat(rig, band)

    print("CONFIRMED: Dynamic EQ produces its configured gain change on all 3 bands")
