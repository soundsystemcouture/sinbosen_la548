"""Live confirmation that set_volume's configured dB delta matches the measured
dBFS delta, for both an input and an output channel. Also cross-validates the
channel_id <-> capture-column mapping test_00 established, via a second,
independent technique (level tracking instead of mute/frequency).
"""

from __future__ import annotations

import functools

from . import analysis, signal_gen
from .rig import Rig

CALIBRATION_HZ = 1000.0
SIGNAL_DURATION_S = 1.5
VOLUME_STEPS_DB = (-10.0, -5.0, 0.0, 5.0)
VARYING_MIN_RANGE_DB = 5.0
ISOLATION_MAX_RANGE_DB = 3.0
# The codec steps 0.1 dB and the onset-aligned level is stable to a few
# hundredths, so a looser tolerance would hide real errors. On hardware every
# 5 dB step measured 5.0 dB in both directions.
TRACKING_TOLERANCE_DB = 0.5


def _sweep_and_identify(rig: Rig, channel_id: int, *, output: bool) -> None:
    """Sweep VOLUME_STEPS_DB on one channel, confirm the measured dBFS delta on
    whichever capture column responds tracks the configured dB delta, and that
    the other capture column stays roughly flat (isolation)."""
    label = f"{'output' if output else 'input'} {channel_id}"
    tone = signal_gen.sine(CALIBRATION_HZ, SIGNAL_DURATION_S, sample_rate=rig.sample_rate)

    levels: list[list[float]] = [[], []]
    for db in VOLUME_STEPS_DB:
        captured = rig.write_then_measure(functools.partial(rig.client.set_volume, channel_id, db, output=output), tone)
        for i in range(2):
            levels[i].append(analysis.onset_aligned_dbfs(captured[:, i], rig.sample_rate, SIGNAL_DURATION_S))
        print(f"{label} volume={db:+.1f} dB -> ch0={levels[0][-1]:.1f} dBFS, ch1={levels[1][-1]:.1f} dBFS")

    ranges = [max(col) - min(col) for col in levels]
    varying = 0 if ranges[0] > ranges[1] else 1
    steady = 1 - varying

    assert ranges[varying] > VARYING_MIN_RANGE_DB, f"no capture channel showed meaningful variation: ranges={ranges}"
    assert ranges[steady] < ISOLATION_MAX_RANGE_DB, (
        f"the non-swept capture channel moved more than expected (isolation broke): ranges={ranges}"
    )

    zero_index = VOLUME_STEPS_DB.index(0.0)
    reference_level = levels[varying][zero_index]
    for db, measured in zip(VOLUME_STEPS_DB, levels[varying], strict=True):
        expected = reference_level + db
        assert abs(measured - expected) < TRACKING_TOLERANCE_DB, (
            f"{label} volume={db:+.1f} dB: expected ~{expected:.1f} dBFS, measured {measured:.1f} dBFS"
        )

    print(f"CONFIRMED: {label} -> capture column {varying}")


def test_10_input_volume_tracks_configured_db(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _sweep_and_identify(rig, 0, output=False)


def test_11_output_volume_tracks_configured_db(rig: Rig) -> None:
    rig.reset_both_channels_safe()
    _sweep_and_identify(rig, 0, output=True)
