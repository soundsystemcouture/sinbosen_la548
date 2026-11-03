"""Measurement helpers turning raw captures into numbers the tests assert on."""

from __future__ import annotations

import numpy as np


def dbfs(samples: np.ndarray) -> float:
    """RMS level in dBFS (0 dBFS = full-scale sine RMS of 1/sqrt(2), i.e. this is
    plain full-scale RMS, not sine-referenced — callers compare deltas, which
    cancels the convention out)."""
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 0.0:
        return float("-inf")
    return float(20.0 * np.log10(rms))


def cross_correlate_delay(reference: np.ndarray, captured: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Find the lag (seconds) that best aligns `captured` with `reference`, plus the
    normalized correlation coefficient at that lag (sign tells you phase polarity:
    ~+1 = in phase, ~-1 = inverted).

    Positive lag means `captured` lags `reference` (arrived later), which is the
    expected case for anything with nonzero delay (including the loopback path's
    own fixed latency and the capture pre-roll).
    """
    ref = reference.astype(np.float64)
    cap = captured.astype(np.float64)
    ref = ref - ref.mean()
    cap = cap - cap.mean()

    correlation = np.correlate(cap, ref, mode="full")
    lags = np.arange(-len(ref) + 1, len(cap))
    best_index = int(np.argmax(np.abs(correlation)))
    best_lag_samples = int(lags[best_index])

    norm = np.sqrt(np.sum(ref**2) * np.sum(cap**2))
    coefficient = float(correlation[best_index] / norm) if norm > 0 else 0.0

    return best_lag_samples / sample_rate, coefficient


def frequency_response(
    reference: np.ndarray,
    captured: np.ndarray,
    sample_rate: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """FFT-ratio magnitude/phase response of `captured` relative to `reference`.

    Both arrays must be the same length and already time-aligned (trim using
    cross_correlate_delay's lag first). Returns (freqs_hz, magnitude_db, phase_rad).
    This is a simple ratio, not a proper Farina deconvolution — fine for a clean
    electrical-loopback signal chain; upgrade only if edge artifacts show up on a
    real sweep.
    """
    if len(reference) != len(captured):
        raise ValueError(f"reference and captured must be the same length, got {len(reference)} vs {len(captured)}")
    ref_spectrum = np.fft.rfft(reference)
    cap_spectrum = np.fft.rfft(captured)
    freqs = np.fft.rfftfreq(len(reference), d=1.0 / sample_rate)

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(np.abs(ref_spectrum) > 0, cap_spectrum / ref_spectrum, 0.0)
    magnitude_db = 20.0 * np.log10(np.abs(ratio) + 1e-20)
    phase_rad = np.angle(ratio)
    return freqs, magnitude_db, phase_rad


def peak_frequency(samples: np.ndarray, sample_rate: int) -> float:
    """The dominant FFT bin frequency in `samples` — for frequency-shifter measurement."""
    spectrum = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / sample_rate)
    return float(freqs[int(np.argmax(spectrum))])


def onset_aligned_dbfs(
    samples: np.ndarray,
    sample_rate: int,
    duration_s: float,
    *,
    trim_s: float = 0.05,
    block_s: float = 0.005,
    onset_drop_db: float = 20.0,
) -> float:
    """dBFS of the `duration_s` signal inside a capture, aligned on its onset.

    A centred window (`(len(capture) - n) // 2`) would be wrong:
    play_and_record starts capturing `startup_settle_s + pre_roll_s` plus
    pw-record's own startup before playback but stops `post_roll_s` after
    it, so a centred window is shifted by ~150 ms plus path latency, with
    jitter that differs run to run. The onset is the first `block_s` block whose RMS is within
    `onset_drop_db` of the loudest block; `trim_s` is dropped at both ends.
    A capture that never rises above the noise (e.g. a muted path) has no
    meaningful onset, so the trimmed window from the first block is used.
    """
    if samples.ndim != 1:
        raise ValueError(f"samples must be one capture column, got shape {samples.shape}")
    block = max(1, int(round(block_s * sample_rate)))
    n_blocks = len(samples) // block
    if n_blocks == 0:
        raise ValueError("capture shorter than one onset block")
    blocks = samples[: n_blocks * block].astype(np.float64).reshape(n_blocks, block)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    loudest = float(rms.max())
    onset = int(np.argmax(rms >= loudest * 10 ** (-onset_drop_db / 20))) * block if loudest > 0 else 0
    start = onset + int(round(trim_s * sample_rate))
    stop = onset + int(round((duration_s - trim_s) * sample_rate))
    if stop <= start or stop > len(samples):
        raise ValueError(
            f"capture too short for a {duration_s:.2f} s window after onset at {onset / sample_rate:.3f} s"
        )
    return dbfs(samples[start:stop])


def band_average_db(freqs: np.ndarray, mag_db: np.ndarray, f_lo: float, f_hi: float) -> float:
    """Mean magnitude over [f_lo, f_hi]; raises rather than returning NaN for an empty band."""
    if not f_lo < f_hi:
        raise ValueError(f"empty band: {f_lo:g}-{f_hi:g} Hz")
    mask = (freqs >= f_lo) & (freqs <= f_hi)
    if not mask.any():
        raise ValueError(f"no frequency bins in {f_lo:g}-{f_hi:g} Hz")
    return float(np.mean(mag_db[mask]))


def passband_beyond_corner(
    corner_hz: float, *, highpass: bool, low_limit_hz: float = 25.0, high_limit_hz: float = 9000.0
) -> tuple[float, float]:
    """A reference band 2-4 octaves past a crossover corner, clamped to the
    measurement's trustworthy range and never collapsing to an empty or
    inverted band (an unclamped 3 kHz high-pass would ask for 12000-9000 Hz)."""
    if highpass:
        hi = min(corner_hz * 16, high_limit_hz)
        lo = min(corner_hz * 4, hi / 1.5)
        clear = lo >= corner_hz * 1.5
    else:
        lo = max(corner_hz / 16, low_limit_hz)
        hi = max(corner_hz / 4, lo * 1.5)
        clear = hi <= corner_hz / 1.5
    if not clear:
        raise ValueError(f"no passband clear of a {corner_hz:g} Hz corner within {low_limit_hz:g}-{high_limit_hz:g} Hz")
    return lo, hi


def smooth_fractional_octave(freqs: np.ndarray, mag_db: np.ndarray, fraction: int = 6) -> np.ndarray:
    """Mean of `mag_db` over a 1/`fraction`-octave band centred on each bin
    (bins at or below 0 Hz are left as-is). Keeps single noisy FFT-ratio
    bins from being taken as a crossing."""
    smoothed = np.array(mag_db, dtype=np.float64)
    positive = np.flatnonzero(freqs > 0)
    if positive.size == 0:
        return smoothed
    f = freqs[positive]
    m = smoothed[positive]
    cumulative = np.concatenate(([0.0], np.cumsum(m)))
    half = 2 ** (1 / (2 * fraction))
    lo = np.searchsorted(f, f / half, side="left")
    hi = np.searchsorted(f, f * half, side="right")
    smoothed[positive] = (cumulative[hi] - cumulative[lo]) / (hi - lo)
    return smoothed


def corner_frequency(
    freqs: np.ndarray,
    mag_db: np.ndarray,
    *,
    highpass: bool,
    passband: tuple[float, float],
    search_hz: tuple[float, float] = (20.0, 20000.0),
    drop_db: float = 3.0,
) -> float:
    """Frequency where the response first falls `drop_db` below the passband
    level, walking away from the passband toward the stopband.

    The search only covers the stopband side of the passband (below it for a
    high-pass, above it for a low-pass) and stays inside `search_hz`, so a
    low-pass can't latch onto the loopback path's own sub-20 Hz roll-off and a
    high-pass can't latch onto anything above its passband.
    """
    reference = band_average_db(freqs, mag_db, *passband)
    threshold = reference - drop_db
    smoothed = smooth_fractional_octave(freqs, mag_db)
    if highpass:
        side = np.flatnonzero((freqs >= search_hz[0]) & (freqs < passband[0]))[::-1]
    else:
        side = np.flatnonzero((freqs > passband[1]) & (freqs <= search_hz[1]))
    below = side[smoothed[side] < threshold]
    if below.size == 0:
        side_name = "low" if highpass else "high"
        raise ValueError(f"never fell to {threshold:.1f} dB on the {side_name} side of the passband")
    return float(freqs[below[0]])


def transfer_function(
    reference: np.ndarray, captured: np.ndarray, sample_rate: int, *, segment: int = 8192
) -> tuple[np.ndarray, np.ndarray]:
    """Welch-averaged H1 estimate (cross spectrum / reference auto spectrum,
    Hann window, 50% overlap) of `captured` relative to `reference`.

    Both columns must come from the same capture so they share one clock and
    one start time; then no lag search is needed and the phase is not forced
    to 0/180 deg the way re-aligning a pure tone per capture forces it.
    Returns (freqs_hz, complex H).
    """
    if len(reference) != len(captured):
        raise ValueError(f"reference and captured must be the same length, got {len(reference)} vs {len(captured)}")
    if len(reference) < segment:
        raise ValueError(f"need at least {segment} samples, got {len(reference)}")
    window = np.hanning(segment)
    step = segment // 2
    cross = np.zeros(segment // 2 + 1, dtype=np.complex128)
    auto = np.zeros(segment // 2 + 1, dtype=np.float64)
    for start in range(0, len(reference) - segment + 1, step):
        x = np.fft.rfft(window * reference[start : start + segment].astype(np.float64))
        y = np.fft.rfft(window * captured[start : start + segment].astype(np.float64))
        cross += np.conj(x) * y
        auto += np.abs(x) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        transfer = np.where(auto > 0, cross / auto, 0.0)
    return np.fft.rfftfreq(segment, d=1.0 / sample_rate), transfer


def group_delay(reference: np.ndarray, captured: np.ndarray, sample_rate: int, f_lo: float, f_hi: float) -> float:
    """Delay (seconds) of `captured` relative to `reference` from the slope of
    their transfer function's phase over [f_lo, f_hi]. Resolves fractions of
    a sample, unlike cross_correlate_delay; both columns must come from the
    same capture (see transfer_function)."""
    freqs, transfer = transfer_function(reference, captured, sample_rate)
    band = (freqs >= f_lo) & (freqs <= f_hi)
    if not band.any():
        raise ValueError(f"no frequency bins in {f_lo:g}-{f_hi:g} Hz")
    slope = np.polyfit(freqs[band], np.unwrap(np.angle(transfer[band])), 1)[0]
    return float(-slope / (2 * np.pi))


def phase_at_deg(freqs: np.ndarray, transfer: np.ndarray, freq_hz: float) -> float:
    """Wrapped phase (deg) of a complex response at `freq_hz`, interpolating
    real and imaginary parts so no unwrapping is involved."""
    value = np.interp(freq_hz, freqs, transfer.real) + 1j * np.interp(freq_hz, freqs, transfer.imag)
    return float(np.degrees(np.angle(value)))


def relative_response(
    baseline: np.ndarray,
    processed: np.ndarray,
    sample_rate: int,
    at_hz: tuple[float, ...],
    *,
    test_column: int = 0,
    reference_column: int = 1,
    segment: int = 8192,
) -> list[tuple[float, float]]:
    """(phase change deg, level change dB) of `test_column` between two
    captures, each measured against `reference_column` of the *same* capture.

    The reference column carries the same broadband signal through an
    untouched path, so per-capture start jitter and the fixed path latency
    cancel inside each capture, and the baseline removes any static
    difference between the two paths.
    """
    freqs, base = transfer_function(baseline[:, reference_column], baseline[:, test_column], sample_rate,
                                    segment=segment)
    _, changed = transfer_function(processed[:, reference_column], processed[:, test_column], sample_rate,
                                   segment=segment)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(base != 0, changed / base, 0.0)
    result = []
    for freq in at_hz:
        level = abs(np.interp(freq, freqs, ratio.real) + 1j * np.interp(freq, freqs, ratio.imag))
        result.append((phase_at_deg(freqs, ratio, freq), float(20 * np.log10(max(level, 1e-15)))))
    return result
