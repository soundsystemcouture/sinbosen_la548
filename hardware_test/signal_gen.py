"""Test-signal generation for the HIL rig.

Every function returns a float32 numpy array shaped (n_frames, n_channels),
in [-1.0, 1.0], ready for audio_io.play_and_record(). Amplitudes default to
a conservative 0.1 (-20 dBFS peak) — callers pick louder levels deliberately,
not by default.
"""

from __future__ import annotations

import numpy as np

DEFAULT_SAMPLE_RATE = 48000


def _stereo(mono: np.ndarray, channels: int) -> np.ndarray:
    return np.tile(mono[:, None], (1, channels)).astype(np.float32)


def silence(duration_s: float, *, channels: int = 2, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    return np.zeros((int(round(duration_s * sample_rate)), channels), dtype=np.float32)


def sine(
    freq_hz: float,
    duration_s: float,
    *,
    amplitude: float = 0.1,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    n = int(round(duration_s * sample_rate))
    t = np.arange(n) / sample_rate
    mono = (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    return _stereo(mono, channels)


def log_sweep(
    f0_hz: float,
    f1_hz: float,
    duration_s: float,
    *,
    amplitude: float = 0.1,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """Exponential (log) sine sweep from f0_hz to f1_hz, phase-continuous."""
    n = int(round(duration_s * sample_rate))
    t = np.arange(n) / sample_rate
    k = (f1_hz / f0_hz) ** (1.0 / duration_s)
    phase = 2 * np.pi * f0_hz * (k**t - 1.0) / np.log(k)
    mono = (amplitude * np.sin(phase)).astype(np.float32)
    return _stereo(mono, channels)


def white_noise(
    duration_s: float,
    *,
    amplitude: float = 0.1,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    seed: int | None = None,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(round(duration_s * sample_rate))
    mono = (amplitude * rng.uniform(-1.0, 1.0, n)).astype(np.float32)
    return _stereo(mono, channels)


def pink_noise(
    duration_s: float,
    *,
    amplitude: float = 0.1,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    seed: int | None = None,
) -> np.ndarray:
    """Voss-McCartney-ish pink noise via 1/f spectral shaping in the frequency domain."""
    rng = np.random.default_rng(seed)
    n = int(round(duration_s * sample_rate))
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    freqs[0] = freqs[1] if len(freqs) > 1 else 1.0
    spectrum /= np.sqrt(freqs)
    pink = np.fft.irfft(spectrum, n=n)
    pink /= np.max(np.abs(pink)) + 1e-12
    mono = (amplitude * pink).astype(np.float32)
    return _stereo(mono, channels)


def am_ramp(
    freq_hz: float,
    duration_s: float,
    *,
    min_amplitude: float = 0.001,
    max_amplitude: float = 0.5,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """A tone whose amplitude ramps linearly from min to max — for dynamics tests
    (compressor/limiter/expander threshold + ratio measurement)."""
    n = int(round(duration_s * sample_rate))
    t = np.arange(n) / sample_rate
    envelope = np.linspace(min_amplitude, max_amplitude, n)
    mono = (envelope * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    return _stereo(mono, channels)


def step_tone(
    freq_hz: float,
    amplitudes: list[float],
    step_duration_s: float,
    *,
    channels: int = 2,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """Concatenated constant-level steps at `amplitudes`, each held for step_duration_s —
    for measuring per-level steady-state gain (dynamics threshold/ratio)."""
    parts = [
        sine(freq_hz, step_duration_s, amplitude=a, channels=channels, sample_rate=sample_rate) for a in amplitudes
    ]
    return np.concatenate(parts, axis=0)
