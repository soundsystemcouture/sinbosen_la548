"""Live confirmation of channel_copy (opcode 0xB5), without a second
loopback leg.

Both output channels (0/1, physical outputs 1/2) are captured
**simultaneously** in a single `rig.measure()` call (they map straight to
the two capture columns of the returned array), and both input channels
(0/1, physical A/B) are driven simultaneously by the same playback signal.
So a copy's effect on a "different" channel is directly observable in one
capture, no extra wiring needed - set channel 0 to something distinctive,
leave channel 1 flat, capture both at once, copy 0 onto 1, capture both
again.

The LA548 has 4 inputs and 8 outputs (see `read_mix_matrix`'s docstring), so
`total_channels=4` for input-side copies and `total_channels=8` for
output-side copies.
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

SOURCE_CHANNEL = 0
TARGET_CHANNEL = 1
BAND_INDEX = 0
FREQ_HZ = 1000
GAIN_DB = 15.0
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2
MIN_BOOST_DELTA_DB = 5.0
MAX_RESIDUAL_DB = 1.0

OUT_TOTAL_CHANNELS = 8
IN_TOTAL_CHANNELS = 4


def _dbfs_pair(rig: Rig) -> tuple[float, float]:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return (analysis.onset_aligned_dbfs(captured[:, 0], rig.sample_rate, TONE_DURATION_S),
            analysis.onset_aligned_dbfs(captured[:, 1], rig.sample_rate, TONE_DURATION_S))


def _flatten_eq(rig: Rig, channel: int, *, output: bool) -> None:
    rig.client.set_eq_band(
        channel, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0, enabled=False, output=output
    )


def test_a5_channel_copy_output_propagates_eq_to_the_target_channel(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _flatten_eq(rig, SOURCE_CHANNEL, output=True)
    _flatten_eq(rig, TARGET_CHANNEL, output=True)
    time.sleep(rig.settle_s)

    rig.client.set_eq_band(
        SOURCE_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=GAIN_DB, q=2.0, type=0, slope=0,
        enabled=True, output=True,
    )
    time.sleep(rig.settle_s)
    before_source_db, before_target_db = _dbfs_pair(rig)
    print(f"before copy: source(ch{SOURCE_CHANNEL})={before_source_db:.2f} dBFS, "
          f"target(ch{TARGET_CHANNEL})={before_target_db:.2f} dBFS")
    boost_delta = before_source_db - before_target_db
    assert boost_delta > MIN_BOOST_DELTA_DB, (
        f"expected the source channel to show a clear boost the target doesn't have yet, got {boost_delta:.2f} dB"
    )

    rig.client.channel_copy(SOURCE_CHANNEL, TARGET_CHANNEL, OUT_TOTAL_CHANNELS, output=True)
    time.sleep(rig.settle_s)
    after_source_db, after_target_db = _dbfs_pair(rig)
    print(f"after copy: source(ch{SOURCE_CHANNEL})={after_source_db:.2f} dBFS, "
          f"target(ch{TARGET_CHANNEL})={after_target_db:.2f} dBFS")

    _flatten_eq(rig, SOURCE_CHANNEL, output=True)
    _flatten_eq(rig, TARGET_CHANNEL, output=True)

    residual = after_source_db - after_target_db
    assert abs(residual) < MAX_RESIDUAL_DB, (
        f"expected the target channel to match the source's boost after copying, residual was {residual:+.2f} dB"
    )
    print("CONFIRMED: channel_copy (output) propagates the source channel's EQ config to the target")


def test_a5_channel_copy_input_propagates_eq_to_the_target_channel(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _flatten_eq(rig, SOURCE_CHANNEL, output=False)
    _flatten_eq(rig, TARGET_CHANNEL, output=False)
    time.sleep(rig.settle_s)

    rig.client.set_eq_band(
        SOURCE_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=GAIN_DB, q=2.0, type=0, slope=0,
        enabled=True, output=False,
    )
    time.sleep(rig.settle_s)
    # Input A (channel 0) routes to output 0, input B (channel 1) routes to
    # output 1 by the rig's default routing (reset_both_channels_safe) -
    # so each input channel's config is read out via its own routed output.
    before_source_db, before_target_db = _dbfs_pair(rig)
    print(f"before copy: source(ch{SOURCE_CHANNEL})={before_source_db:.2f} dBFS, "
          f"target(ch{TARGET_CHANNEL})={before_target_db:.2f} dBFS")
    boost_delta = before_source_db - before_target_db
    assert boost_delta > MIN_BOOST_DELTA_DB, (
        f"expected the source channel to show a clear boost the target doesn't have yet, got {boost_delta:.2f} dB"
    )

    rig.client.channel_copy(SOURCE_CHANNEL, TARGET_CHANNEL, IN_TOTAL_CHANNELS, output=False)
    time.sleep(rig.settle_s)
    after_source_db, after_target_db = _dbfs_pair(rig)
    print(f"after copy: source(ch{SOURCE_CHANNEL})={after_source_db:.2f} dBFS, "
          f"target(ch{TARGET_CHANNEL})={after_target_db:.2f} dBFS")

    _flatten_eq(rig, SOURCE_CHANNEL, output=False)
    _flatten_eq(rig, TARGET_CHANNEL, output=False)

    residual = after_source_db - after_target_db
    assert abs(residual) < MAX_RESIDUAL_DB, (
        f"expected the target channel to match the source's boost after copying, residual was {residual:+.2f} dB"
    )
    print("CONFIRMED: channel_copy (input) propagates the source channel's EQ config to the target")
