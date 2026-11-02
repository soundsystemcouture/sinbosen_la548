"""Live confirmation that set_compressor/set_limiter/set_expander's
whole-struct wire format (docs/PROTOCOL.md, "Compressor, Limiter, Expander")
actually reaches the device - not just that it ACKs. Same `read_group()`
byte-diff technique as Dynamic EQ (test_90_dyneq_wire_format.py).

This only checks that each whole-struct write lands in stored parameter
state; test_a1_dynamics_audible_effect.py checks that it's audible. If that
comes back flat while these writes reach storage, power-cycle the unit
before assuming the struct format is wrong.

Compressor is tested on the OUTPUT channel: input compressor has no effect on
this firmware and the client refuses it (see the skipped test at the end).
"""

from __future__ import annotations

import pytest

from .rig import Rig

CHANNEL = 0
MIN_EXPECTED_DIFFERING_BYTES = 5  # generous vs. the struct's 10 bytes (1 byte, detectionType, is fixed at 0)


def _diff_count(blob_a: bytes, blob_b: bytes) -> int:
    assert len(blob_a) == len(blob_b), "read_group() blob length changed between reads - unexpected"
    return sum(1 for a, b in zip(blob_a, blob_b, strict=True) if a != b)


def test_a0_set_compressor_output_whole_struct_write_reaches_device(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_compressor(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=True
    )
    blob_a = rig.client.read_group()

    rig.client.set_compressor(
        CHANNEL, threshold_db=-10.0, start_time_ms=20.0, release_time_ms=200.0, ratio=8.0, enabled=True, output=True
    )
    blob_b = rig.client.read_group()

    differing = _diff_count(blob_a, blob_b)
    print(f"read_group() blob: {differing} bytes differ after writing a fully-changed output Compressor struct")
    assert differing >= MIN_EXPECTED_DIFFERING_BYTES, (
        f"only {differing} bytes changed - expected at least {MIN_EXPECTED_DIFFERING_BYTES} "
        "for a write touching 4 of 5 meaningful struct fields; the whole-struct write may not be reaching the device"
    )

    rig.client.set_compressor(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=True
    )
    print("CONFIRMED: set_compressor's whole-struct write reaches the device's stored parameter state (output)")


def test_a0_set_limiter_whole_struct_write_reaches_device(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_limiter(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, enabled=False, detection_type=0
    )
    blob_a = rig.client.read_group()

    rig.client.set_limiter(
        CHANNEL, threshold_db=-10.0, start_time_ms=20.0, release_time_ms=200.0, enabled=True, detection_type=1
    )
    blob_b = rig.client.read_group()

    differing = _diff_count(blob_a, blob_b)
    print(f"read_group() blob: {differing} bytes differ after writing a fully-changed Limiter struct")
    assert differing >= MIN_EXPECTED_DIFFERING_BYTES, (
        f"only {differing} bytes changed - expected at least {MIN_EXPECTED_DIFFERING_BYTES} "
        "for a write touching 5 of 5 meaningful struct fields; the whole-struct write may not be reaching the device"
    )

    rig.client.set_limiter(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, enabled=False, detection_type=0
    )
    print("CONFIRMED: set_limiter's whole-struct write reaches the device's stored parameter state")


def test_a0_set_expander_whole_struct_write_reaches_device(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_expander(
        CHANNEL, threshold_db=-100.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False
    )
    blob_a = rig.client.read_group()

    rig.client.set_expander(
        CHANNEL, threshold_db=-10.0, start_time_ms=20.0, release_time_ms=200.0, ratio=8.0, enabled=True
    )
    blob_b = rig.client.read_group()

    differing = _diff_count(blob_a, blob_b)
    print(f"read_group() blob: {differing} bytes differ after writing a fully-changed Expander struct")
    assert differing >= MIN_EXPECTED_DIFFERING_BYTES, (
        f"only {differing} bytes changed - expected at least {MIN_EXPECTED_DIFFERING_BYTES} "
        "for a write touching 4 of 5 meaningful struct fields; the whole-struct write may not be reaching the device"
    )

    rig.client.set_expander(
        CHANNEL, threshold_db=-100.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False
    )
    print("CONFIRMED: set_expander's whole-struct write reaches the device's stored parameter state")


@pytest.mark.skip(reason="input compressor has no effect on this firmware and the client refuses it "
                         "(Client.set_compressor(output=False) sends nothing); see the test docstring")
def test_a0_set_compressor_input_does_not_reach_storage_on_this_unit(rig: Rig) -> None:
    """Regression test for the input compressor gap. On hardware a fully
    changed input Compressor write produced a **zero-byte** `read_group()`
    diff - not just "no audible effect", the write never reaches storage at
    all, unlike output Compressor/Limiter/Expander (the tests above). That is
    the signature of a block this model doesn't have, rather than a wedged
    DSP block (which would show storage updated with no audio change, as
    Dynamic EQ did before a power cycle). If this ever produces a nonzero
    diff (a different unit or a firmware update), revisit the client's
    refusal.
    """
    rig.reset_both_channels_safe()

    rig.client.set_compressor(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=False
    )
    blob_a = rig.client.read_group()

    rig.client.set_compressor(
        CHANNEL, threshold_db=-10.0, start_time_ms=20.0, release_time_ms=200.0, ratio=8.0, enabled=True, output=False
    )
    blob_b = rig.client.read_group()

    differing = _diff_count(blob_a, blob_b)
    print(f"read_group() blob: {differing} bytes differ after writing a fully-changed input Compressor struct")
    assert differing == 0, (
        f"{differing} bytes changed - input Compressor may now be functional on this unit/firmware; "
        "revisit Client.set_compressor's refusal and docs/PROTOCOL.md"
    )

    rig.client.set_compressor(
        CHANNEL, threshold_db=-60.0, start_time_ms=0.3, release_time_ms=50.0, ratio=1.0, enabled=False, output=False
    )
    print("CONFIRMED: input Compressor is absent on this unit - the write never reaches storage")
