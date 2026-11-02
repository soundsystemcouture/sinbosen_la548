"""Live confirmation of set_mix_route's on/off polarity.

The protocol alone doesn't say whether a mix-route `value` of 1 means
"connected" or "disconnected", and an inverted encoding was plausible (Mute's
is inverted - see test_00). `codecs.mix_route_u8` is a plain identity
passthrough (True = connected).

Indirect evidence already existed: `Rig.reset_both_channels_safe()` calls
`set_mix_route(0, 0, True)` / `set_mix_route(1, 1, True)` to wire up the
loopback path every other test depends on, and those tests measure real
signal getting through - which couldn't happen if `True` meant "disconnect".
This test measures it directly, using a crosspoint the rig doesn't otherwise
use: route input A (channel_id=0) to output 2 (channel_id=1) - normally
disconnected by `reset_both_channels_safe()` - while input B is muted, so
output 2's only possible source is that one crosspoint. A tone appearing on
output 2 exactly when the crosspoint is toggled true, and vanishing when
it's toggled false, is unambiguous.
"""

from __future__ import annotations

import time

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

TONE_DURATION_S = 2.0
TONE_AMPLITUDE = 0.2
FREQ_HZ = 1000
NOISE_FLOOR_DB = -80.0  # generous vs. the ~-109dBFS measured when disconnected
CONNECTED_DB = -60.0  # generous vs. the ~-38dBFS measured when connected

SOURCE_INPUT = 0  # input A
TARGET_OUTPUT = 1  # output 2 - not part of the rig's normal (0,0)/(1,1) routing


def _dbfs_at_output(rig: Rig, output_index: int) -> float:
    mono = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)[:, 0]
    silence = np.zeros_like(mono)
    signal = np.stack([mono, silence], axis=1).astype(np.float32)
    captured = rig.measure(signal)
    return analysis.onset_aligned_dbfs(captured[:, output_index], rig.sample_rate, TONE_DURATION_S)


def test_80_mix_route_true_connects_false_disconnects(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    rig.client.set_mute(1, True, output=False)  # silence input B - isolate output 2 to just this crosspoint
    time.sleep(rig.settle_s)

    baseline_db = _dbfs_at_output(rig, TARGET_OUTPUT)
    print(f"route ({SOURCE_INPUT},{TARGET_OUTPUT}) untouched (default off): {baseline_db:.2f} dBFS")
    assert baseline_db < NOISE_FLOOR_DB, f"expected noise floor before ever toggling this route, got {baseline_db}"

    rig.client.set_mix_route(SOURCE_INPUT, TARGET_OUTPUT, True)
    time.sleep(rig.settle_s)
    true_db = _dbfs_at_output(rig, TARGET_OUTPUT)
    print(f"set_mix_route({SOURCE_INPUT},{TARGET_OUTPUT}, True)  -> {true_db:.2f} dBFS")
    assert true_db > CONNECTED_DB, f"expected tone to appear once the crosspoint was set True, got {true_db} dBFS"

    rig.client.set_mix_route(SOURCE_INPUT, TARGET_OUTPUT, False)
    time.sleep(rig.settle_s)
    false_db = _dbfs_at_output(rig, TARGET_OUTPUT)
    print(f"set_mix_route({SOURCE_INPUT},{TARGET_OUTPUT}, False) -> {false_db:.2f} dBFS")
    assert false_db < NOISE_FLOOR_DB, f"expected tone to vanish once the crosspoint was set False, got {false_db} dBFS"

    # cleanup
    rig.client.set_mute(1, False, output=False)
    rig.reset_both_channels_safe()
    print("CONFIRMED: set_mix_route(..., True) connects, False disconnects - matches the shipped identity codec")
