"""eq_all_reset (opcode 0xB0): ACKs normally but is **non-functional on this
firmware**, like eq_all_bypass (0xAF, test_70_eq_bypass_polarity.py) -
except that one times out instead of ACKing.

On hardware, a band boosted +15 dB measured no change after `eq_all_reset`,
and a `read_group()` byte diff showed **zero of the blob's 2464 bytes
changed anywhere** - not just at the boosted band's offset. The frame is a
single opcode 0xB0 / module_id=4 / empty payload with no whole-struct write
behind it to replicate, so this is a firmware no-op, not a wire-format bug or
a client-side mistake. Use `set_eq_band(..., enabled=False)` per band
instead (`test_71_eq_bypass_fallback.py`) for a real, working reset/bypass.
"""

from __future__ import annotations

import time

import pytest

from . import analysis, signal_gen
from .rig import Rig

# `Client.eq_all_reset` refuses to send (NotImplementedError, no packet) because of the
# finding above, so this test can't reach the device.
pytestmark = pytest.mark.skip(
    reason="eq_all_reset has no effect on this firmware and the client refuses it; see the module docstring"
)

OUTPUT_CHANNEL = 0
BAND_INDEX = 0
FREQ_HZ = 1000
GAIN_DB = 15.0
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2


def _dbfs_at(rig: Rig) -> float:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, OUTPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def test_72_eq_all_reset_acks_but_does_not_reset_a_boosted_band(rig: Rig) -> None:
    """Regression test for the confirmed-non-functional finding above. If
    this ever starts actually flattening the boosted band (a firmware
    update, or a different unit), that's the cue to revisit this test and
    docs/PROTOCOL.md/client.py's docstring - not a reason to just relax the
    assertion."""
    rig.reset_both_channels_safe()
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=True
    )
    baseline_db = _dbfs_at(rig)

    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=GAIN_DB, q=2.0, type=0, slope=0,
        enabled=True, output=True,
    )
    time.sleep(rig.settle_s)
    blob_before_reset = rig.client.read_group()
    boosted_db = _dbfs_at(rig)
    boost_delta = boosted_db - baseline_db
    print(f"boosted: {boosted_db:.2f} dBFS (delta {boost_delta:+.2f})")
    assert boost_delta > 5.0, f"expected a large boost before testing reset, got {boost_delta:+.2f}"

    frame = rig.client.eq_all_reset(OUTPUT_CHANNEL, output=True)  # must not raise TimeoutError - it ACKs
    time.sleep(rig.settle_s)
    blob_after_reset = rig.client.read_group()
    after_db = _dbfs_at(rig)

    diffs = sum(1 for a, b in zip(blob_before_reset, blob_after_reset, strict=True) if a != b)
    residual = after_db - baseline_db
    print(
        f"eq_all_reset ack: opcode={frame.opcode!r}; storage diff: {diffs} bytes changed; "
        f"after: {after_db:.2f} dBFS (residual vs baseline {residual:+.2f})"
    )
    assert diffs == 0, (
        f"eq_all_reset changed {diffs} bytes of stored state - it may now be functional on this unit/firmware; "
        "revisit docs/PROTOCOL.md and client.py's eq_all_reset docstring"
    )

    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=True
    )
    print("CONFIRMED: eq_all_reset ACKs but is a genuine storage-level no-op on this hardware")
