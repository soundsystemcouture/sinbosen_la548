"""Live confirmation of save_program/load_program (opcodes 0xAB/0xAC) - the
highest blast radius of anything in this directory (`write_group` is
deliberately not implemented): load_program overwrites **every** channel's
live settings at once, not just the one this test is looking at.

save_program overwrites a stored program and a slot's name doesn't show
whether it holds one (saving never renames it), so the slot is never
assumed free: the operator names a scratch slot with LA548_SCRATCH_PROGRAM_SLOT
(0-29) or the test skips. The slot keeps the test's saved state afterwards.

Sets a single distinctive, easily-measured state (an EQ boost on output
channel 0), saves it, changes live state to something clearly different,
confirms the change is measurable, then loads the saved slot back and
confirms the original measurement returns - proving the full save/restore
round trip actually works, not just that both calls ACK. Passes on hardware:
the reloaded level measured within 0.1 dB of the saved one.
"""

from __future__ import annotations

import os
import time

import pytest

from . import analysis, signal_gen
from .rig import Rig

SLOT_ENV = "LA548_SCRATCH_PROGRAM_SLOT"
OUTPUT_CHANNEL = 0
BAND_INDEX = 0
FREQ_HZ = 1000
SAVED_GAIN_DB = 15.0
CHANGED_GAIN_DB = -12.0
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2
GAIN_TOLERANCE_DB = 1.5
LOAD_SETTLE_S = 1.0  # generous vs. rig.settle_s - this rewrites the whole device, not one param


def _dbfs(rig: Rig) -> float:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, OUTPUT_CHANNEL], rig.sample_rate, TONE_DURATION_S)


def _set_gain(rig: Rig, gain_db: float) -> None:
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=gain_db, q=2.0, type=0, slope=0,
        enabled=True, output=True,
    )


def _scratch_slot() -> int:
    value = os.environ.get(SLOT_ENV)
    if value is None:
        pytest.skip(f"set {SLOT_ENV}=<0-29> to a program slot that may be overwritten")
    if not value.isdigit() or not 0 <= int(value) <= 29:
        pytest.fail(f"{SLOT_ENV} must be a program slot 0-29, got {value!r}")
    return int(value)


def test_a6_save_then_load_restores_the_saved_eq_state(rig: Rig) -> None:
    slot = _scratch_slot()
    rig.reset_both_channels_safe()
    _set_gain(rig, SAVED_GAIN_DB)
    time.sleep(rig.settle_s)
    saved_db = _dbfs(rig)
    print(f"state to save: {SAVED_GAIN_DB:+.1f} dB EQ boost measured as {saved_db:.2f} dBFS")

    rig.client.save_program(slot)

    _set_gain(rig, CHANGED_GAIN_DB)
    time.sleep(rig.settle_s)
    changed_db = _dbfs(rig)
    print(f"changed to {CHANGED_GAIN_DB:+.1f} dB EQ, measured as {changed_db:.2f} dBFS")
    assert abs(changed_db - saved_db) > 5.0, (
        f"expected a clearly different measurement after changing gain, "
        f"got {changed_db:.2f} dBFS vs saved {saved_db:.2f} dBFS"
    )

    rig.client.load_program(slot)
    time.sleep(LOAD_SETTLE_S)
    restored_db = _dbfs(rig)
    print(f"after load_program({slot}): {restored_db:.2f} dBFS (originally saved: {saved_db:.2f} dBFS)")

    _set_gain(rig, 0.0)
    rig.client.set_eq_band(
        OUTPUT_CHANNEL, BAND_INDEX, freq_hz=FREQ_HZ, gain_db=0.0, q=1.0, type=0, slope=0,
        enabled=False, output=True,
    )

    assert abs(restored_db - saved_db) < GAIN_TOLERANCE_DB, (
        f"expected load_program to restore the originally-saved EQ state, "
        f"measured {restored_db:.2f} dBFS vs originally-saved {saved_db:.2f} dBFS"
    )
    print("CONFIRMED: save_program/load_program round-trip a real DSP parameter correctly")
