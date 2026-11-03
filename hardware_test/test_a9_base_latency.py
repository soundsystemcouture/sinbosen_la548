"""Absolute input-to-output latency of the LA548, and what changes it,
measured against a reference path that bypasses the unit.

Every other test measures lag from the played signal to the capture. That
lag also contains the interface's own playback and capture latency and the
start offset between pw-cat and pw-record, which alone varies by about 16 ms
between captures. Those tests only compare lags within one capture, so the
offsets cancel. An absolute latency needs them removed instead.

Wiring: the interface output that drives LA548 input A is Y-split, and the
second leg goes straight into a spare interface input, named by
LA548_HIL_REFERENCE_CHANNEL. Both legs leave the same output jack and come
back through matching interface inputs in one capture, so

    latency = lag(LA548 output 1) - lag(reference)

is the LA548 alone: analog input, ADC, DSP, DAC and analog output.

Each measurement takes two estimates. The cross-correlation peak has
whole-sample resolution. The group delay is the phase slope of output 1
against the reference between 1 and 4 kHz, which resolves fractions of a
sample. They must agree within one sample, which guards against a
correlation peak locking onto the wrong lag.

The latency also depends on a device-wide state set by FIR parameter writes:
the most recent write that uses the Kaiser window (win_type 0) adds 64
samples at 96 kHz to every path when its attenuation is 21 dB, the bottom of
the range, and removes them when it is higher. Writes with other windows
leave the state alone. Every test here therefore starts by writing output
1's FIR block, disabled, Kaiser at 60 dB, which clears it.

- test_a90: base latency, input A to output 1.
- test_a91: a delay on output 2 or input B does not move it (report-only).
- test_a92: an enabled FIR adds (N - 1) / 2 + 128 samples at 96 kHz, the
  same in every band (linear phase), on input A and on output 1.
- test_a93: the FIR stage's impulse response, with a rectangular window,
  spans 256 or 512 taps at 96 kHz.
- test_a94: the Kaiser 21 dB state, written on output 8, outside the
  measured path.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

import numpy as np
import pytest

from . import analysis, signal_gen
from .rig import Rig

SIGNAL_DURATION_S = 1.0
COEFF_THRESHOLD = 0.6
# test_a93's 2 kHz low-pass passes about a tenth of the noise's bandwidth,
# which caps output 1's correlation near 0.25; the reference still gates.
LOWPASS_COEFF_THRESHOLD = 0.1
MAX_MEASURE_ATTEMPTS = 3
REPEATS = 3
CLIP_LEVEL = 0.99
GROUP_DELAY_BAND_HZ = (1000.0, 4000.0)
# The converters alone take longer than the minimum. A smaller value means
# the reference leg passes through the LA548 too, or the columns are swapped.
PLAUSIBLE_LATENCY_MS = (0.1, 50.0)
DELAY_ELSEWHERE_MS = (1.0, 1000.0)

DSP_RATE_HZ = 96000
FIR_TAPS = {0: 256, 1: 512}  # order_index -> taps
FIR_EXTRA_SAMPLES = 128  # added by an enabled FIR on top of its (N - 1) / 2 group delay
KAISER_21DB_SAMPLES = 64
KAISER = 0
FAST_ATTENUATION_DB = 60.0  # any Kaiser attenuation above 21 dB clears the state
FIR_BANDS_HZ = ((200.0, 800.0), (1000.0, 4000.0), (5000.0, 10000.0))
FIR_PASS_CORNER_HZ = 20000  # low-pass corner (freq_low_hz, see test_a3) that keeps every band in the passband
FIR_IR_CORNER_HZ = 2000  # a low corner gives a long sinc whose truncation is easy to see
IR_EDGE_DB = -45.0  # the truncated sinc ends near -26 dB; the noise floor is below -55 dB
TAP_TOLERANCE = 16

OUTPUT_COLUMN = 0  # LA548 output 1, fed by input A
REFERENCE_COLUMN = 2  # measure_with_reference appends the reference channel


@dataclass(frozen=True)
class Latency:
    peak_ms: float  # cross-correlation peak, whole samples
    group_delay_ms: float  # phase slope over GROUP_DELAY_BAND_HZ
    polarity: int  # sign of output 1 relative to the reference
    output_dbfs: float
    reference_dbfs: float
    band_delays_ms: tuple[float, ...]  # group delay over each of FIR_BANDS_HZ


def _require_reference(rig: Rig) -> None:
    if rig.reference_channel is None:
        pytest.skip(
            "needs LA548_HIL_REFERENCE_CHANNEL: a capture channel fed by a Y-split of the interface "
            "output that drives LA548 input A (see hardware_test/README.md)"
        )


def _capture(rig: Rig, noise: np.ndarray, *, output_threshold: float = COEFF_THRESHOLD
             ) -> tuple[np.ndarray, float, float]:
    """A capture whose output 1 and reference columns both carry the noise
    unclipped, plus the lag difference (s) and the polarity of output 1
    relative to the reference."""
    played = noise[:, 0]
    for attempt in range(MAX_MEASURE_ATTEMPTS):
        captured = rig.measure_with_reference(noise)
        out_lag_s, out_coeff = analysis.cross_correlate_delay(played, captured[:, OUTPUT_COLUMN], rig.sample_rate)
        ref_lag_s, ref_coeff = analysis.cross_correlate_delay(played, captured[:, REFERENCE_COLUMN], rig.sample_rate)
        if abs(out_coeff) > output_threshold and abs(ref_coeff) > COEFF_THRESHOLD:
            break
        print(f"  (attempt {attempt + 1}/{MAX_MEASURE_ATTEMPTS}: weak correlation, output {out_coeff:.2f}, "
              f"reference {ref_coeff:.2f}, retrying)")
    else:
        raise AssertionError(
            f"correlation too weak after {MAX_MEASURE_ATTEMPTS} attempts: output {out_coeff:.2f}, reference "
            f"{ref_coeff:.2f}. A silent reference means the Y-split is not on the output that drives input A, "
            "or LA548_HIL_REFERENCE_CHANNEL names the wrong capture channel"
        )

    peak_output = float(np.max(np.abs(captured[:, OUTPUT_COLUMN])))
    peak_reference = float(np.max(np.abs(captured[:, REFERENCE_COLUMN])))
    assert max(peak_output, peak_reference) < CLIP_LEVEL, (
        f"capture clipped (peak output {peak_output:.3f}, reference {peak_reference:.3f}): "
        "lower the gain on that interface input"
    )
    return captured, out_lag_s - ref_lag_s, 1.0 if out_coeff * ref_coeff > 0 else -1.0


def _measure(rig: Rig, noise: np.ndarray) -> Latency:
    captured, lag_s, polarity = _capture(rig, noise)
    output, reference = captured[:, OUTPUT_COLUMN], captured[:, REFERENCE_COLUMN]
    latency = Latency(
        peak_ms=lag_s * 1000.0,
        group_delay_ms=analysis.group_delay(reference, output, rig.sample_rate, *GROUP_DELAY_BAND_HZ) * 1000.0,
        polarity=int(polarity),
        output_dbfs=analysis.dbfs(output),
        reference_dbfs=analysis.dbfs(reference),
        band_delays_ms=tuple(analysis.group_delay(reference, output, rig.sample_rate, low, high) * 1000.0
                             for low, high in FIR_BANDS_HZ),
    )
    low, high = PLAUSIBLE_LATENCY_MS
    assert low < latency.group_delay_ms < high, (
        f"implausible latency {latency.group_delay_ms:.3f} ms: check that the reference leg bypasses the LA548"
    )
    sample_ms = 1000.0 / rig.sample_rate
    assert abs(latency.peak_ms - latency.group_delay_ms) <= sample_ms, (
        f"correlation peak {latency.peak_ms:.3f} ms and group delay {latency.group_delay_ms:.4f} ms "
        f"disagree by more than one sample ({sample_ms:.4f} ms)"
    )
    return latency


def _noise(rig: Rig) -> np.ndarray:
    # One column: input A gets the noise and input B plays silence.
    return signal_gen.white_noise(SIGNAL_DURATION_S, channels=1, sample_rate=rig.sample_rate, seed=0)


def _set_fir(rig: Rig, channel_id: int, *, output: bool, enabled: bool = False, order_index: int = 1,
             win_type: int = KAISER, attenuation_db: float = FAST_ATTENUATION_DB,
             corner_hz: int = FIR_PASS_CORNER_HZ) -> None:
    rig.client.set_fir(
        channel_id, enabled=enabled, fir_type=0, win_type=win_type, freq_low_hz=corner_hz,
        freq_high_hz=FIR_PASS_CORNER_HZ, attenuation_db=attenuation_db, order_index=order_index, output=output,
    )
    time.sleep(rig.settle_s)


def _start(rig: Rig) -> np.ndarray:
    """Common setup: a reference is wired, A/B and outputs 1/2 are at their
    safe baseline, and the Kaiser 21 dB state is clear."""
    _require_reference(rig)
    rig.reset_both_channels_safe()
    _set_fir(rig, 0, output=True)
    return _noise(rig)


def _dsp_samples_ms(samples: float) -> float:
    return samples / DSP_RATE_HZ * 1000.0


def _describe_coefficients(block: bytes) -> str:
    taps = np.array(struct.unpack(f"<{len(block) // 4}f", block))
    nonzero = np.flatnonzero(taps)
    if not nonzero.size:
        return "all zero"
    return (f"{nonzero.size} of {taps.size} non-zero, taps {nonzero[0]}-{nonzero[-1]}, "
            f"peak at {int(np.argmax(np.abs(taps)))}")


def test_a90_base_latency(rig: Rig) -> None:
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate

    results = [_measure(rig, noise) for _ in range(REPEATS)]
    for latency in results:
        print(f"latency: peak {latency.peak_ms:.3f} ms, group delay {latency.group_delay_ms:.4f} ms, "
              f"polarity {latency.polarity:+d} (output {latency.output_dbfs:.1f} dBFS, "
              f"reference {latency.reference_dbfs:.1f} dBFS)")

    delays = [latency.group_delay_ms for latency in results]
    spread = max(delays) - min(delays)
    assert spread <= sample_ms, (
        f"repeated captures disagree by {spread:.4f} ms, more than one sample ({sample_ms:.4f} ms): {delays}"
    )
    mean_ms = float(np.mean(delays))
    print(f"CONFIRMED: input A -> output 1 latency {mean_ms:.3f} ms "
          f"({mean_ms / _dsp_samples_ms(1):.1f} samples at {DSP_RATE_HZ} Hz), spread {spread * 1000:.1f} us")


def test_a91_latency_with_delay_elsewhere(rig: Rig) -> None:
    """Report-only."""
    noise = _start(rig)

    baseline = _measure(rig, noise).group_delay_ms
    print(f"every delay 0 ms: output 1 latency {baseline:.4f} ms")
    for output, label in ((True, "output 2"), (False, "input B")):
        for ms in DELAY_ELSEWHERE_MS:
            rig.client.set_delay(1, ms, output=output)
            time.sleep(rig.settle_s)
            latency = _measure(rig, noise).group_delay_ms
            print(f"{label} delay {ms:g} ms: output 1 latency {latency:.4f} ms ({latency - baseline:+.4f} ms)")
        rig.client.set_delay(1, 0.0, output=output)


@pytest.mark.parametrize("output", [True, False], ids=["output1", "inputA"])
def test_a92_fir_latency(rig: Rig, output: bool) -> None:
    """The coefficient block (0xC8) is reported, not graded."""
    noise = _start(rig)
    label = "output 1" if output else "input A"
    sample_ms = 1000.0 / rig.sample_rate
    _set_fir(rig, 0, output=output)
    baseline = _measure(rig, noise)

    for order_index, taps in FIR_TAPS.items():
        _set_fir(rig, 0, output=output, enabled=True, order_index=order_index)
        latency = _measure(rig, noise)
        added = [on - off for on, off in zip(latency.band_delays_ms, baseline.band_delays_ms, strict=True)]
        expected = _dsp_samples_ms((taps - 1) / 2 + FIR_EXTRA_SAMPLES)
        bands = ", ".join(f"{low:g}-{high:g} Hz {delay:+.4f} ms"
                          for (low, high), delay in zip(FIR_BANDS_HZ, added, strict=True))
        print(f"{label} FIR {taps} taps: latency {latency.group_delay_ms:.4f} ms; added {bands}; "
              f"expected {expected:.4f} ms")
        print(f"  coefficient block: {_describe_coefficients(rig.client.read_fir_coefs(0, output=output))}")
        assert max(added) - min(added) <= sample_ms, (
            f"{label} {taps} taps: added delay differs across bands by more than one sample: {added}"
        )
        assert abs(float(np.mean(added)) - expected) <= sample_ms, (
            f"{label} {taps} taps: added {float(np.mean(added)):.4f} ms, expected {expected:.4f} ms"
        )


@pytest.mark.parametrize("order_index", list(FIR_TAPS))
def test_a93_fir_tap_count(rig: Rig, order_index: int) -> None:
    """A Kaiser window at 21 dB has beta 0, a rectangular window, so the
    low-pass's truncated sinc stops abruptly at its first and last tap. The
    impulse response of the FIR stage alone is the transfer function with
    the FIR enabled divided by the one with it disabled, both measured
    against the reference."""
    noise = _start(rig)

    def transfer(enabled: bool) -> np.ndarray:
        _set_fir(rig, 0, output=True, enabled=enabled, order_index=order_index, attenuation_db=21.0,
                 corner_hz=FIR_IR_CORNER_HZ)
        captured, _, _ = _capture(rig, noise, output_threshold=LOWPASS_COEFF_THRESHOLD)
        _, h = analysis.transfer_function(captured[:, REFERENCE_COLUMN], captured[:, OUTPUT_COLUMN], rig.sample_rate)
        return h

    off = transfer(False)
    on = transfer(True)
    with np.errstate(divide="ignore", invalid="ignore"):
        fir = np.where(off != 0, on / off, 0.0)
    impulse = np.fft.irfft(fir)
    impulse = np.roll(impulse, len(impulse) // 2)
    level_db = 20 * np.log10(np.abs(impulse) / np.max(np.abs(impulse)) + 1e-12)
    support = np.flatnonzero(level_db > IR_EDGE_DB)
    taps = (support[-1] - support[0]) / rig.sample_rate * DSP_RATE_HZ + 1
    print(f"order index {order_index}: impulse response spans {taps:.0f} taps at {DSP_RATE_HZ} Hz "
          f"(above {IR_EDGE_DB:g} dB)")
    assert abs(taps - FIR_TAPS[order_index]) <= TAP_TOLERANCE, (
        f"order index {order_index}: measured {taps:.0f} taps, expected {FIR_TAPS[order_index]}"
    )


def test_a94_kaiser_21db_write_adds_64_samples_everywhere(rig: Rig) -> None:
    noise = _start(rig)
    sample_ms = 1000.0 / rig.sample_rate

    def after_output8_write(win_type: int, attenuation_db: float) -> float:
        _set_fir(rig, 7, output=True, win_type=win_type, attenuation_db=attenuation_db)
        latency = _measure(rig, noise).group_delay_ms
        print(f"after output 8 FIR write, window {win_type}, {attenuation_db:g} dB: {latency:.4f} ms")
        return latency

    fast = after_output8_write(KAISER, FAST_ATTENUATION_DB)
    other_window_21 = after_output8_write(1, 21.0)
    slow = after_output8_write(KAISER, 21.0)
    other_window_60 = after_output8_write(1, FAST_ATTENUATION_DB)
    cleared = after_output8_write(KAISER, 22.0)

    expected = _dsp_samples_ms(KAISER_21DB_SAMPLES)
    assert abs((slow - fast) - expected) <= sample_ms, (
        f"Kaiser 21 dB added {slow - fast:.4f} ms, expected {expected:.4f} ms"
    )
    assert abs(other_window_21 - fast) <= sample_ms, "a non-Kaiser write at 21 dB set the state"
    assert abs(other_window_60 - slow) <= sample_ms, "a non-Kaiser write cleared the state"
    assert abs(cleared - fast) <= sample_ms, "a Kaiser write above 21 dB did not clear the state"
