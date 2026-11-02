"""Run this file first: validates the whole physical + software chain end to
end, mute_u8's inverted encoding, and the channel_id <-> physical port mapping
rig.py relies on (input channel_id 0/1 = A/B, output channel_id 0/1 =
outputs 1/2).

Uses distinct per-side tones (not one shared calibration tone) specifically so
a mute's effect can be attributed to one physical side or the other by
frequency, not just by presence/absence.
"""

from __future__ import annotations

import functools

import numpy as np

from . import analysis, signal_gen
from .rig import Rig

CALIBRATION_HZ = 1000.0
TONE_A_HZ = 1000.0
TONE_B_HZ = 1700.0  # far enough from TONE_A_HZ that peak_frequency can't confuse them
SIGNAL_DURATION_S = 1.5
PRESENT_DBFS = -55.0  # generous floor; a -20 dBFS source through any sane gain stage clears this easily
MUTE_DROP_DB = 20.0  # minimum level drop to call a capture channel "silenced" by mute


def _dual_tone(freq_a: float, freq_b: float, duration_s: float, *, sample_rate: int) -> np.ndarray:
    """Stereo signal with a different tone per channel, so a mute's effect can be
    attributed to a physical side by frequency instead of just presence/absence."""
    left = signal_gen.sine(freq_a, duration_s, channels=1, sample_rate=sample_rate)
    right = signal_gen.sine(freq_b, duration_s, channels=1, sample_rate=sample_rate)
    return np.concatenate([left, right], axis=1)


def test_00_calibration_tone_present(rig: Rig) -> None:
    """Baseline sanity check: the whole chain (interface out -> LA548 -> interface in)
    carries a plain tone at a sane level on both capture channels. If this fails,
    nothing else in this directory can be trusted - check wiring and the
    LA548_HIL_* channel names before debugging anything further down the list."""
    rig.reset_both_channels_safe()
    tone = signal_gen.sine(CALIBRATION_HZ, SIGNAL_DURATION_S, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)

    levels = [analysis.dbfs(captured[:, i]) for i in range(2)]
    print(f"capture levels: ch0={levels[0]:.1f} dBFS, ch1={levels[1]:.1f} dBFS")

    for i, level in enumerate(levels):
        assert level > PRESENT_DBFS, f"capture channel {i} too quiet ({level:.1f} dBFS) - check wiring"


def test_01_input_mute_polarity_and_mapping(rig: Rig) -> None:
    """Mutes each input channel_id in turn and watches which capture channel
    (and therefore which frequency, and therefore which physical LA548 input)
    goes quiet. Confirms both that Client.set_mute(..., True) actually silences
    audio and which physical input port each channel_id corresponds to."""
    rig.reset_both_channels_safe()
    tone = _dual_tone(TONE_A_HZ, TONE_B_HZ, SIGNAL_DURATION_S, sample_rate=rig.sample_rate)

    baseline = rig.measure(tone)
    base_levels = [analysis.dbfs(baseline[:, i]) for i in range(2)]
    base_peaks = [analysis.peak_frequency(baseline[:, i], rig.sample_rate) for i in range(2)]
    print(
        f"baseline: ch0={base_levels[0]:.1f} dBFS @ {base_peaks[0]:.0f} Hz, "
        f"ch1={base_levels[1]:.1f} dBFS @ {base_peaks[1]:.0f} Hz"
    )
    for level in base_levels:
        assert level > PRESENT_DBFS, "baseline capture too quiet to run a mute test against"

    for muted_channel in (0, 1):
        try:
            captured = rig.write_then_measure(
                functools.partial(rig.client.set_mute, muted_channel, True, output=False), tone
            )
            levels = [analysis.dbfs(captured[:, i]) for i in range(2)]
            drops = [base_levels[i] - levels[i] for i in range(2)]
            print(f"mute input {muted_channel}: ch0 drop={drops[0]:.1f} dB, ch1 drop={drops[1]:.1f} dB")

            silenced = [i for i, d in enumerate(drops) if d > MUTE_DROP_DB]
            assert len(silenced) == 1, (
                f"expected exactly one capture channel to go quiet when muting input {muted_channel}, got drops {drops}"
            )
            quiet_channel = silenced[0]
            quiet_freq = base_peaks[quiet_channel]
            physical_side = "A" if abs(quiet_freq - TONE_A_HZ) < abs(quiet_freq - TONE_B_HZ) else "B"
            print(
                f"CONFIRMED: input channel_id={muted_channel} -> physical input {physical_side} "
                f"(capture channel {quiet_channel} went quiet, was carrying {quiet_freq:.0f} Hz)"
            )
        finally:
            rig.client.set_mute(muted_channel, False, output=False)


def test_02_output_mute_polarity_and_mapping(rig: Rig) -> None:
    """Same idea as test_01, but for the two output channel_ids - confirms which
    physical LA548 output (1 or 2) each output channel_id corresponds to."""
    rig.reset_both_channels_safe()
    tone = signal_gen.sine(CALIBRATION_HZ, SIGNAL_DURATION_S, sample_rate=rig.sample_rate)

    baseline = rig.measure(tone)
    base_levels = [analysis.dbfs(baseline[:, i]) for i in range(2)]
    for level in base_levels:
        assert level > PRESENT_DBFS, "baseline capture too quiet to run a mute test against"

    for muted_channel in (0, 1):
        try:
            captured = rig.write_then_measure(
                functools.partial(rig.client.set_mute, muted_channel, True, output=True), tone
            )
            levels = [analysis.dbfs(captured[:, i]) for i in range(2)]
            drops = [base_levels[i] - levels[i] for i in range(2)]
            print(f"mute output {muted_channel}: ch0 drop={drops[0]:.1f} dB, ch1 drop={drops[1]:.1f} dB")

            silenced = [i for i, d in enumerate(drops) if d > MUTE_DROP_DB]
            assert len(silenced) == 1, (
                f"expected exactly one capture channel to go quiet when muting output {muted_channel}, "
                f"got drops {drops}"
            )
            physical_output = silenced[0] + 1  # capture columns 0/1 <- physical LA548 outputs 1/2
            print(f"CONFIRMED: output channel_id={muted_channel} -> physical LA548 output {physical_output}")
        finally:
            rig.client.set_mute(muted_channel, False, output=True)
