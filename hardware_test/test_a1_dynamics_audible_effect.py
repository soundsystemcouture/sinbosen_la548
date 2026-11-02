"""Live confirmation that Compressor/Limiter/Expander's whole-struct write
(test_a0_dynamics_wire_format.py) actually processes audio, not just that it
reaches device storage - the same two-step check as Dynamic EQ (test_90 for
storage, test_91 for audio), because those are two separate questions
(docs/PROTOCOL.md, "Dynamic EQ").

Compressor/Expander: play a steady tone comfortably above a low, aggressive
threshold with a steep ratio and no automatic makeup gain - a working
downward compressor/expander should measurably move the output level toward
(compressor) or away from (expander) the threshold, once its start_time has
elapsed. Compared against the flat/disabled baseline at the same input
level, this should be a large, easy-to-detect delta if the block is doing
anything at all - deliberately not a precise ratio/threshold-formula
verification.

If any of these come back flat/silent despite test_a0's confirmed-reaching-
storage write, power-cycle the unit before assuming the struct format is
wrong (see test_91_dyneq_audible_effect.py).
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

CHANNEL = 0
FREQ_HZ = 1000
TONE_DURATION_S = 1.5
TONE_AMPLITUDE = 0.5  # ~-6 dBFS, comfortably above every threshold used below
MIN_EXPECTED_DELTA_DB = 10.0  # generous vs. the tens of dB a steep ratio at this margin should produce


def _dbfs_at(rig: Rig) -> float:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, CHANNEL], rig.sample_rate, TONE_DURATION_S)


def test_a1_compressor_output_reduces_level_of_a_tone_above_threshold(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_compressor(
        CHANNEL, threshold_db=0.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=True
    )
    baseline_db = _dbfs_at(rig)

    rig.client.set_compressor(
        CHANNEL, threshold_db=-40.0, start_time_ms=0.3, release_time_ms=50.0, ratio=20.0, enabled=True, output=True
    )
    time.sleep(rig.settle_s)
    compressed_db = _dbfs_at(rig)

    delta = baseline_db - compressed_db
    print(f"compressor output: baseline={baseline_db:.2f}, compressed={compressed_db:.2f}, drop={delta:.2f} dB")
    assert delta > MIN_EXPECTED_DELTA_DB, (
        f"expected a large level drop with threshold=-40dB/ratio=20 on a {TONE_AMPLITUDE} amplitude tone, "
        f"measured only {delta:.2f} dB - if this is ~0 dB, try power-cycling before assuming a code regression"
    )

    rig.client.set_compressor(
        CHANNEL, threshold_db=0.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=True
    )
    print("CONFIRMED: Compressor (output) measurably reduces the level of a tone above threshold")


def test_a1_limiter_reduces_level_of_a_tone_above_threshold(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_limiter(
        CHANNEL, threshold_db=0.0, start_time_ms=0.3, release_time_ms=50.0, enabled=False, detection_type=0
    )
    baseline_db = _dbfs_at(rig)

    rig.client.set_limiter(
        CHANNEL, threshold_db=-40.0, start_time_ms=0.3, release_time_ms=50.0, enabled=True, detection_type=0
    )
    time.sleep(rig.settle_s)
    limited_db = _dbfs_at(rig)

    delta = baseline_db - limited_db
    print(f"limiter: baseline={baseline_db:.2f} dBFS, limited={limited_db:.2f} dBFS, drop={delta:.2f} dB")
    assert delta > MIN_EXPECTED_DELTA_DB, (
        f"expected a large level drop with threshold=-40dB on a {TONE_AMPLITUDE} amplitude tone, "
        f"measured only {delta:.2f} dB - if this is ~0 dB, try power-cycling before assuming a code regression"
    )

    rig.client.set_limiter(
        CHANNEL, threshold_db=0.0, start_time_ms=0.3, release_time_ms=50.0, enabled=False, detection_type=0
    )
    print("CONFIRMED: Limiter measurably reduces the level of a tone above threshold")


def test_a1_expander_reduces_level_of_a_tone_below_threshold(rig: Rig) -> None:
    # Expander is a downward expander (gates/attenuates BELOW threshold, the
    # opposite direction from Compressor/Limiter) - so the tone must be set
    # BELOW the threshold to trigger it, not above.
    rig.reset_both_channels_safe()

    rig.client.set_expander(
        CHANNEL, threshold_db=-100.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False
    )
    baseline_db = _dbfs_at(rig)

    rig.client.set_expander(
        CHANNEL, threshold_db=-3.0, start_time_ms=0.3, release_time_ms=50.0, ratio=20.0, enabled=True
    )
    time.sleep(rig.settle_s)
    expanded_db = _dbfs_at(rig)

    delta = baseline_db - expanded_db
    print(f"expander: baseline={baseline_db:.2f} dBFS, expanded={expanded_db:.2f} dBFS, drop={delta:.2f} dB")
    assert delta > MIN_EXPECTED_DELTA_DB, (
        f"expected a large level drop with threshold=-3dB/ratio=20 on a {TONE_AMPLITUDE} amplitude "
        f"(~-6dBFS, below threshold) tone, measured only {delta:.2f} dB - if this is ~0 dB, "
        "try power-cycling before assuming a code regression"
    )

    rig.client.set_expander(
        CHANNEL, threshold_db=-100.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False
    )
    print("CONFIRMED: Expander measurably reduces the level of a tone below threshold")
