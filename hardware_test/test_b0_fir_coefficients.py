"""What the DSP does with each channel's FIR coefficient block (0xC8 read,
0xC9 write: 512 float32 values).

The manufacturer's app writes the block only when it imports coefficients
from a file: it selects FIR type 5, then writes the block. For types 0-2
the device designs the filter itself. Measured here: with the FIR enabled
and type 5, the channel is convolved with the block, tap 0 first, at the
96 kHz DSP rate, with the values used as given and 128 samples of latency
on top, the same fixed part a designed FIR has (test_a92). A disabled FIR
and the designed types ignore the block.

Wiring and measurement are test_a9's: LA548 output 1 against a reference leg
that bypasses the unit, so a latency or level change is the LA548 alone.
Every test block is a single impulse or silence: 1.0 at tap d delays by d
samples, 0.5 halves the level, and an all-zero block silences the channel.
Unless a test says otherwise, each block is written the way the
manufacturer's app imports one, FIR parameters first and then the block,
and is read back before measuring.

- test_b00: the device stores type 5 as 6; 6 written back stays 6.
- test_b01: a disabled FIR ignores the block.
- test_b02: a designed low-pass ignores the block (its delay is test_a92's).
- test_b03: type 5 delays by 128 samples plus the impulse's tap, on output
  1 and on input A.
- test_b04: type 5 uses the values as given: 1.0 is unity gain, 0.5 is
  -6 dB and -0.5 also inverts.
- test_b05: an all-zero block silences the channel.
- test_b06: order index 0 uses the block's first 256 values, 1 all 512.
- test_b07: selecting type 5 alone does not load the block; writing one does.
- test_b08: once loaded, later FIR parameter writes keep the block.

Every test saves the blocks of output 1 and input A first and writes them
back afterwards, verified by readback; the `rig` fixture then restores the
FIR parameters.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from sinbosen_la548.fir_adapter import FIRParameters

from . import analysis
from .rig import Rig
from .test_a9_base_latency import (
    FIR_BANDS_HZ,
    FIR_EXTRA_SAMPLES,
    FIR_TAPS,
    OUTPUT_COLUMN,
    REFERENCE_COLUMN,
    Latency,
    _describe_coefficients,
    _dsp_samples_ms,
    _measure,
    _set_fir,
    _start,
)

BLOCK_VALUES = 512  # float32 values per channel on the DA0408X
COEFFICIENT_BLOCK = 5  # fir_type that applies the block
STORED_COEFFICIENT_BLOCK = 6  # what the device stores when 5 is written
DESIGNED_LOWPASS = 0
ORDER_256 = 0
ORDER_512 = 1
IMPULSE_TAPS = (0, 100, 200)
LEVEL_TOLERANCE_DB = 0.25
SILENCE_DB = 40.0  # how far below the FIR-off level an unused or all-zero block must be
REFERENCE_FLOOR_DBFS = -80.0
PAST_256_TAP = 300
LAST_TAP = BLOCK_VALUES - 1
# A corner inside the capture band: a filter designed from it would cut the
# noise by about 3 dB, against 0.6 dB for test_a9's 20 kHz pass corner.
SELECT_CORNER_HZ = 12000
OTHER_WINDOW = 3  # Blackman candidate; not Kaiser, so the Kaiser 21 dB state (test_a94) stays clear
OTHER_ATTENUATION_DB = 90.0


def _block(taps: dict[int, float]) -> bytes:
    values = [0.0] * BLOCK_VALUES
    for tap, value in taps.items():
        values[tap] = value
    return struct.pack(f"<{BLOCK_VALUES}f", *values)


@pytest.fixture
def saved_blocks(rig: Rig) -> Iterator[None]:
    """Write output 1's and input A's original blocks back after the test."""
    originals = {output: rig.client.read_fir_coefs(0, output=output) for output in (True, False)}
    for output, block in originals.items():
        print(f"original {'output 1' if output else 'input A'} block: {_describe_coefficients(block)}")
    try:
        yield
    finally:
        failed = []
        for output, block in originals.items():
            rig.client.write_fir_coefs(0, block, output=output)
            if rig.client.read_fir_coefs(0, output=output) != block:
                failed.append("output 1" if output else "input A")
        assert not failed, f"original FIR coefficient block did not read back on {', '.join(failed)}"


def _write_block(rig: Rig, taps: dict[int, float], *, output: bool = True) -> None:
    block = _block(taps)
    rig.client.write_fir_coefs(0, block, output=output)
    time.sleep(rig.settle_s)
    assert rig.client.read_fir_coefs(0, output=output) == block, "written FIR coefficient block did not read back"


def _load(rig: Rig, taps: dict[int, float], *, output: bool = True, enabled: bool = True,
          fir_type: int = COEFFICIENT_BLOCK, order_index: int = ORDER_512) -> None:
    """FIR parameters, then the block, as the manufacturer's app imports one.
    Kaiser at 60 dB, so the Kaiser 21 dB state (test_a94) stays clear."""
    _set_fir(rig, 0, output=output, enabled=enabled, fir_type=fir_type, order_index=order_index)
    _write_block(rig, taps, output=output)


def _gain_db(latency: Latency) -> float:
    return latency.output_dbfs - latency.reference_dbfs


def _levels(rig: Rig, noise: np.ndarray) -> tuple[float, float]:
    """Output 1 and reference levels (dBFS) without the correlation check,
    for a capture whose output may be silent."""
    captured = rig.measure_with_reference(noise)
    output, reference = analysis.dbfs(captured[:, OUTPUT_COLUMN]), analysis.dbfs(captured[:, REFERENCE_COLUMN])
    assert reference > REFERENCE_FLOOR_DBFS, f"reference silent ({reference:.1f} dBFS): check the Y-split"
    return output, reference


def _added_samples(latency: Latency, baseline: Latency) -> float:
    """Added delay in DSP samples."""
    return (latency.group_delay_ms - baseline.group_delay_ms) / _dsp_samples_ms(1)


def _assert_block_delay(latency: Latency, baseline: Latency, tap: int, sample_ms: float, label: str) -> None:
    """Every band delayed by 128 samples plus `tap`, at unity gain."""
    added = [on - off for on, off in zip(latency.band_delays_ms, baseline.band_delays_ms, strict=True)]
    expected = _dsp_samples_ms(FIR_EXTRA_SAMPLES + tap)
    gain = _gain_db(latency) - _gain_db(baseline)
    print(f"{label}: added {_added_samples(latency, baseline):.2f} samples at 96 kHz "
          f"(expected {FIR_EXTRA_SAMPLES + tap}), gain {gain:+.2f} dB")
    assert max(added) - min(added) <= sample_ms, f"{label}: added delay differs across bands: {added}"
    assert abs(float(np.mean(added)) - expected) <= sample_ms, (
        f"{label}: added {float(np.mean(added)):.4f} ms, expected {expected:.4f} ms "
        f"({FIR_EXTRA_SAMPLES} + {tap} samples)"
    )
    assert abs(gain) <= LEVEL_TOLERANCE_DB, f"{label}: gain {gain:+.2f} dB, expected unity"


@pytest.mark.usefixtures("saved_blocks")
def test_b00_type5_stored_as_6(rig: Rig) -> None:
    for written in (COEFFICIENT_BLOCK, STORED_COEFFICIENT_BLOCK):
        _load(rig, {100: 1.0}, fir_type=written)
        fir = FIRParameters.read(rig.client.read_settings().outputs[0].fir)
        print(f"wrote type {written}: output 1 FIR reads back {fir}")
        assert fir.enabled and fir.order_index == ORDER_512, f"FIR parameters did not read back as written: {fir}"
        assert fir.fir_type == STORED_COEFFICIENT_BLOCK, (
            f"wrote type {written}, read back {fir.fir_type}, expected {STORED_COEFFICIENT_BLOCK}"
        )


@pytest.mark.usefixtures("saved_blocks")
def test_b01_disabled_fir_ignores_block(rig: Rig) -> None:
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate
    baseline = _measure(rig, noise)

    _load(rig, {100: 1.0}, enabled=False)
    latency = _measure(rig, noise)
    moved = latency.group_delay_ms - baseline.group_delay_ms
    print(f"FIR off: {baseline.group_delay_ms:.4f} ms; FIR off, type 5, impulse at tap 100: "
          f"{latency.group_delay_ms:.4f} ms ({moved:+.4f} ms, gain {_gain_db(latency) - _gain_db(baseline):+.2f} dB)")
    assert abs(moved) <= sample_ms, f"a disabled FIR applied the block: latency moved {moved:+.4f} ms"


@pytest.mark.usefixtures("saved_blocks")
def test_b02_designed_type_ignores_block(rig: Rig) -> None:
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate
    baseline = _measure(rig, noise)

    _load(rig, {100: 1.0}, fir_type=DESIGNED_LOWPASS)
    latency = _measure(rig, noise)
    added = [on - off for on, off in zip(latency.band_delays_ms, baseline.band_delays_ms, strict=True)]
    expected = _dsp_samples_ms((FIR_TAPS[ORDER_512] - 1) / 2 + FIR_EXTRA_SAMPLES)
    bands = ", ".join(f"{low:g}-{high:g} Hz {delay:+.4f} ms" for (low, high), delay in zip(FIR_BANDS_HZ, added,
                                                                                         strict=True))
    print(f"type 0 low-pass, 512 taps, impulse at tap 100 in the block: added {bands}; "
          f"the designed filter alone adds {expected:.4f} ms")
    assert max(added) - min(added) <= sample_ms, f"added delay differs across bands: {added}"
    assert abs(float(np.mean(added)) - expected) <= sample_ms, (
        f"added {float(np.mean(added)):.4f} ms, expected the designed filter's {expected:.4f} ms: "
        "the block was applied while a designed type was selected"
    )


@pytest.mark.usefixtures("saved_blocks")
@pytest.mark.parametrize("output", [True, False], ids=["output1", "inputA"])
def test_b03_type5_delays_by_impulse_tap(rig: Rig, output: bool) -> None:
    noise = _start(rig)
    label = "output 1" if output else "input A"
    sample_ms = 1000.0 / rig.sample_rate
    _set_fir(rig, 0, output=output)
    baseline = _measure(rig, noise)
    print(f"{label} FIR off: {baseline.group_delay_ms:.4f} ms")

    for tap in IMPULSE_TAPS:
        _load(rig, {tap: 1.0}, output=output)
        _assert_block_delay(_measure(rig, noise), baseline, tap, sample_ms, f"{label} type 5, impulse at tap {tap}")


@pytest.mark.usefixtures("saved_blocks")
def test_b04_type5_uses_values_as_given(rig: Rig) -> None:
    noise = _start(rig)
    baseline = _measure(rig, noise)

    results = {}
    for value in (1.0, 0.5, -0.5):
        _load(rig, {0: value})
        results[value] = latency = _measure(rig, noise)
        print(f"type 5, {value:+g} at tap 0: gain {_gain_db(latency) - _gain_db(baseline):+.2f} dB vs FIR off, "
              f"polarity {latency.polarity * baseline.polarity:+d} vs FIR off")

    unit = _gain_db(results[1.0]) - _gain_db(baseline)
    assert abs(unit) <= LEVEL_TOLERANCE_DB, f"a unit impulse changed the level by {unit:+.2f} dB"
    assert results[1.0].polarity == baseline.polarity, "a unit impulse inverted the polarity"
    for value in (0.5, -0.5):
        change = _gain_db(results[value]) - _gain_db(results[1.0])
        expected = 20.0 * np.log10(abs(value))
        assert abs(change - expected) <= LEVEL_TOLERANCE_DB, (
            f"{value:+g} at tap 0: {change:+.2f} dB vs a unit impulse, expected {expected:+.2f} dB"
        )
    assert results[0.5].polarity == baseline.polarity, "+0.5 inverted the polarity"
    assert results[-0.5].polarity == -baseline.polarity, "-0.5 did not invert the polarity"


@pytest.mark.usefixtures("saved_blocks")
def test_b05_zero_block_silences(rig: Rig) -> None:
    noise = _start(rig)
    base_output, base_reference = _levels(rig, noise)

    _load(rig, {})
    output, reference = _levels(rig, noise)
    print(f"FIR off: output {base_output:.1f} dBFS, reference {base_reference:.1f} dBFS; "
          f"type 5, all-zero block: output {output:.1f} dBFS, reference {reference:.1f} dBFS")
    assert output <= base_output - SILENCE_DB, (
        f"an all-zero block left output 1 at {output:.1f} dBFS, only {base_output - output:.1f} dB "
        f"below FIR off ({base_output:.1f} dBFS)"
    )


@pytest.mark.usefixtures("saved_blocks")
def test_b06_order_sets_block_length(rig: Rig) -> None:
    """The manufacturer's app imports 256 values for order index 0 and 512
    for 1: an impulse past tap 255 is silent at order 0 only."""
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate
    baseline = _measure(rig, noise)
    base_output, base_reference = _levels(rig, noise)

    _load(rig, {100: 1.0}, order_index=ORDER_256)
    _assert_block_delay(_measure(rig, noise), baseline, 100, sample_ms, "order index 0, impulse at tap 100")

    for order_index, tap in ((ORDER_256, PAST_256_TAP), (ORDER_512, PAST_256_TAP), (ORDER_512, LAST_TAP)):
        _load(rig, {tap: 1.0}, order_index=order_index)
        output, reference = _levels(rig, noise)
        level = (output - reference) - (base_output - base_reference)
        label = f"order index {order_index} ({FIR_TAPS[order_index]} taps), impulse at tap {tap}"
        print(f"{label}: {level:+.1f} dB vs FIR off")
        if tap < FIR_TAPS[order_index]:
            assert abs(level) <= LEVEL_TOLERANCE_DB, f"{label}: {level:+.2f} dB, expected unity"
        else:
            assert level <= -SILENCE_DB, f"{label}: {level:+.1f} dB, expected silence"


@pytest.mark.usefixtures("saved_blocks")
def test_b07_selecting_type5_does_not_load_block(rig: Rig) -> None:
    """_start leaves output 1 on a designed low-pass at 20 kHz, 512 taps,
    disabled. Selecting type 5 with a 12 kHz corner keeps that filter
    running, not the stored block and not a filter designed from the new
    fields, until a block is written."""
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate
    baseline = _measure(rig, noise)
    _set_fir(rig, 0, output=True, enabled=True, fir_type=DESIGNED_LOWPASS)
    designed = _measure(rig, noise)
    print(f"designed low-pass at 20 kHz: added {_added_samples(designed, baseline):.2f} samples, "
          f"gain {_gain_db(designed) - _gain_db(baseline):+.2f} dB")
    _set_fir(rig, 0, output=True, enabled=False)

    _set_fir(rig, 0, output=True, enabled=True, fir_type=COEFFICIENT_BLOCK, corner_hz=SELECT_CORNER_HZ)
    selected = _measure(rig, noise)
    print(f"type 5 selected, no block written: added {_added_samples(selected, baseline):.2f} samples, "
          f"gain {_gain_db(selected) - _gain_db(baseline):+.2f} dB")
    moved = selected.group_delay_ms - designed.group_delay_ms
    assert abs(moved) <= sample_ms, (
        f"selecting type 5 moved the latency {moved:+.4f} ms from the designed filter's: the stored block "
        "was loaded without a write"
    )
    level = _gain_db(selected) - _gain_db(designed)
    assert abs(level) <= LEVEL_TOLERANCE_DB, (
        f"selecting type 5 changed the level {level:+.2f} dB from the 20 kHz designed filter's: "
        f"a filter was designed from the {SELECT_CORNER_HZ} Hz corner"
    )

    _write_block(rig, {100: 1.0})
    _assert_block_delay(_measure(rig, noise), baseline, 100, sample_ms, "after writing the block")


@pytest.mark.usefixtures("saved_blocks")
def test_b08_later_parameter_writes_keep_block(rig: Rig) -> None:
    """After a reconnect the manufacturer's app re-sends the stored type, 6,
    with every later FIR edit, so each of these must keep the block."""
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate
    baseline = _measure(rig, noise)
    _load(rig, {100: 1.0})
    _assert_block_delay(_measure(rig, noise), baseline, 100, sample_ms, "block loaded")

    writes: list[tuple[str, dict[str, Any]]] = [
        ("type 6 re-sent", {}),
        ("order index 0", {"order_index": ORDER_256}),
        (f"corner {SELECT_CORNER_HZ} Hz", {"corner_hz": SELECT_CORNER_HZ}),
        (f"window {OTHER_WINDOW}, {OTHER_ATTENUATION_DB:g} dB",
         {"win_type": OTHER_WINDOW, "attenuation_db": OTHER_ATTENUATION_DB}),
    ]
    for label, fields in writes:
        _set_fir(rig, 0, output=True, enabled=True, fir_type=STORED_COEFFICIENT_BLOCK, **fields)
        _assert_block_delay(_measure(rig, noise), baseline, 100, sample_ms, f"after {label}")

    _set_fir(rig, 0, output=True, enabled=False, fir_type=STORED_COEFFICIENT_BLOCK)
    _set_fir(rig, 0, output=True, enabled=True, fir_type=STORED_COEFFICIENT_BLOCK)
    _assert_block_delay(_measure(rig, noise), baseline, 100, sample_ms, "after disable then enable")
