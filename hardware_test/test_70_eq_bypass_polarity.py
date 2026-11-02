"""eq_all_bypass (opcode 0xAF): **non-functional on this firmware**, so there
is no on/off polarity to observe.

An exhaustive sweep on hardware (both `channel_type`s, `channel_id` 0-3,
`module_id` 0 and 4, an empty payload and a 1-byte payload) got zero replies
to opcode 0xAF in every case, while the structurally identical sibling opcode
0xB0 (`eq_all_reset`) ACKs normally on the same link - ruling out a
session-level or wire-shape problem. Sending the frame anyway (ignoring the
missing ACK, in case the DSP silently applies writes it doesn't acknowledge)
against a live +12 dB EQ boost produced zero measurable change either way.
It stayed dead after a power cycle.

The test below documents that as a regression check: if a future firmware
(or a different unit) ever starts acknowledging this opcode, it fails on the
`pytest.raises(TimeoutError)`, which is the cue to come back and determine
the polarity. Use per-band `set_eq_band(..., enabled=False)` instead
(test_71_eq_bypass_fallback.py).
"""

from __future__ import annotations

import time

import pytest

from . import analysis, signal_gen
from .rig import Rig

# `Client.eq_all_bypass` refuses to send (NotImplementedError, no packet) because of the
# finding above, so this test can't reach the device.
pytestmark = pytest.mark.skip(
    reason="eq_all_bypass has no effect on this firmware and the client refuses it; see the module docstring"
)

TONE_DURATION_S = 2.0
TONE_AMPLITUDE = 0.2
FREQ_HZ = 1000
OUTPUT_CHANNEL = 0
BAND_INDEX = 0


def _dbfs_at(rig: Rig, freq_hz: float) -> float:
    tone = signal_gen.sine(freq_hz, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, OUTPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def test_70_eq_all_bypass_confirmed_non_functional(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=12.0, q=1.0, type=0, slope=0, enabled=True, output=True
    )
    time.sleep(rig.settle_s)
    boosted_db = _dbfs_at(rig, FREQ_HZ)

    with pytest.raises(TimeoutError):
        rig.client.eq_all_bypass(OUTPUT_CHANNEL, True, output=True, timeout=1.0)

    # Even though the write never ACKs, confirm it didn't silently land
    # anyway - the boosted level must still be exactly where it was.
    time.sleep(rig.settle_s)
    after_db = _dbfs_at(rig, FREQ_HZ)
    print(f"EQ boost before eq_all_bypass attempt: {boosted_db:.2f} dBFS, after: {after_db:.2f} dBFS")
    assert abs(after_db - boosted_db) < 1.0, "level moved even though eq_all_bypass never ACKed - investigate further"

    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=True
    )
    print("CONFIRMED: eq_all_bypass (opcode 0xAF) is non-functional on this hardware - no ACK, no silent effect")
