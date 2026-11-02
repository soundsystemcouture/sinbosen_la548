"""Immutable OSM/native and text transfer-function traces; no measurement capture."""

from __future__ import annotations

import csv
import json
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from .response import FloatArray, Response, _make_response, finite_segments, unwrap_phase


@dataclass(frozen=True)
class Trace:
    id: str
    name: str
    frequencies: FloatArray
    magnitude_db: FloatArray
    phase_rad: FloatArray | None
    coherence: FloatArray | None
    notes: str = ""
    sample_rate_hz: int | None = None
    impulse_time_ms: FloatArray | None = None
    impulse_values: FloatArray | None = None
    raw: bytes = field(default=b"", repr=False)
    format: str = "text"

    def response(self, *, gain_db: float = 0, delay_ms: float = 0, inverted: bool = False,
                 coherence_threshold: float = 0) -> Response:
        if not math.isfinite(gain_db) or not math.isfinite(delay_ms) or not 0 <= coherence_threshold <= 1:
            raise ValueError("invalid trace offset or coherence threshold")
        magnitude = self.magnitude_db + gain_db
        phase = (np.array(self.phase_rad, copy=True) if self.phase_rad is not None
                 else np.zeros(len(self.frequencies), dtype=np.float64))
        if self.phase_rad is not None:
            phase += -2 * np.pi * self.frequencies * delay_ms / 1000 + (np.pi if inverted else 0)
        mask = ~np.isfinite(magnitude)
        if self.coherence is not None and coherence_threshold:
            mask |= ~np.isfinite(self.coherence) | (self.coherence < coherence_threshold)
        magnitude = np.where(mask, np.nan, magnitude)
        phase = np.where(mask, np.nan, phase)
        transfer = np.asarray(10 ** (magnitude / 20) * np.exp(1j * phase), dtype=np.complex128)
        response = _make_response(self.frequencies, transfer, phase,
                                  notes=("Measured trace; filters are not reapplied.",))
        # Missing phase must not discard an otherwise valid magnitude sample.
        response = replace(response, magnitude_db=_array(magnitude))
        if self.phase_rad is None:
            missing = np.full(len(self.frequencies), np.nan, dtype=np.float64)
            missing.setflags(write=False)
            response = replace(response, phase_deg=missing, phase_unwrapped_deg=missing, group_delay_ms=missing,
                               continuous_phase_rad=missing)
        return response


def _array(values: Any) -> FloatArray:
    result = np.asarray(values, dtype=np.float64)
    result.setflags(write=False)
    return result


def _number(value: Any, label: str, *, missing: bool = False) -> float:
    if missing and (value is None or isinstance(value, str)
                    and value.strip().lower() in ("*", "", "nan", "n/a", "na", "null")):
        return math.nan
    if isinstance(value, bool):
        raise ValueError(f"{label} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {label}: {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _rows(rows: list[list[Any]]) -> tuple[FloatArray, FloatArray, FloatArray | None, FloatArray | None]:
    if len(rows) < 3:
        raise ValueError("a trace needs at least three frequency samples")
    frequencies = _array([row[0] for row in rows])
    if frequencies[0] <= 0 or np.any(np.diff(frequencies) <= 0):
        raise ValueError("trace frequencies must be positive and strictly increasing")
    magnitude = _array([row[1] for row in rows])
    if not np.any(np.isfinite(magnitude)):
        raise ValueError("trace has no valid magnitude samples")
    has_phase = any(len(row) > 2 and np.isfinite(row[2]) for row in rows)
    phase = _array(unwrap_phase(_array([row[2] if len(row) > 2 else math.nan for row in rows]))) if has_phase else None
    has_coherence = any(len(row) > 3 and np.isfinite(row[3]) for row in rows)
    coherence = _array([row[3] if len(row) > 3 else math.nan for row in rows]) if has_coherence else None
    if coherence is not None and np.any(np.isfinite(coherence) & ((coherence < 0) | (coherence > 1))):
        raise ValueError("coherence must be between 0 and 1")
    return frequencies, magnitude, phase, coherence


def _native(raw: bytes, name: str) -> Trace:
    document = json.loads(raw)
    if not isinstance(document, dict) or str(document.get("type", "")).lower() != "stored":
        raise ValueError("import a single OSM stored trace; project/live sources are not supported")
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("ftdata"), list):
        raise ValueError("OSM stored trace has no ftdata array")
    rows = []
    for source in data["ftdata"]:
        if not isinstance(source, list) or len(source) < 3:
            raise ValueError("invalid OSM frequency row")
        freq = _number(source[0], "frequency")
        if freq == 0:  # Native FFT traces may include a DC bin, outside log plots.
            continue
        magnitude = _number(source[2], "linear transfer magnitude", missing=True)
        if magnitude < 0:
            raise ValueError("linear transfer magnitude cannot be negative")
        db = 20 * math.log10(magnitude) if magnitude > 0 else math.nan
        phase = _number(source[3], "phase", missing=True) if len(source) > 3 else math.nan
        row_coherence = _number(source[4], "coherence", missing=True) if len(source) > 4 else math.nan
        rows.append([freq, db, phase, row_coherence])
    frequencies, magnitude_db, phase_rad, coherence = _rows(rows)
    gain = _number(data.get("gain", 0), "stored gain")
    delay = _number(data.get("delay", 0), "stored delay")
    for flag in ("polarity", "inverse", "icoherence"):
        if flag in data and not isinstance(data[flag], bool):
            raise ValueError(f"stored {flag} must be boolean")
    # Reproduce OSM's magnitude chart, including gain inside the inverse sign.
    magnitude_db = _array((magnitude_db + gain) * (-1 if data.get("inverse", False) else 1))
    if phase_rad is not None:
        phase_rad = _array(phase_rad - 2 * np.pi * frequencies * delay / 1000
                           + (np.pi if data.get("polarity", False) else 0))
    if data.get("icoherence", False):
        coherence = _array(np.ones(len(frequencies)))
    impulse = data.get("impulse", [])
    times = values = None
    if impulse:
        if not isinstance(impulse, list) or any(not isinstance(row, list) or len(row) < 2 for row in impulse):
            raise ValueError("invalid OSM impulse array")
        times = _array([_number(row[0], "impulse time") + delay for row in impulse])
        values = _array([_number(row[1], "impulse value") for row in impulse])
        values = _array(values * 10 ** (gain / 20) * (-1 if data.get("polarity", False) else 1))
    rate = data.get("sampleRate")
    if rate is not None and (not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0):
        raise ValueError("invalid OSM sample rate")
    return Trace(id=uuid.uuid4().hex, name=str(data.get("name") or name), frequencies=frequencies,
                 magnitude_db=magnitude_db, phase_rad=phase_rad, coherence=coherence,
                 notes=str(data.get("notes", "")), sample_rate_hz=rate, impulse_time_ms=times,
                 impulse_values=values, raw=raw, format="osm")


def _text(raw: bytes, name: str) -> Trace:
    text = raw.decode("utf-8-sig")
    rows = []
    osm = "created with open sound meter" in text.lower()
    notes: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", "*", ";", "//")):
            continue
        if "," in line or ";" in line:
            cells = [cell.strip() for cell in next(csv.reader([line], delimiter="," if "," in line else ";"))]
        else:
            cells = re.split(r"\s+", line)
        # Vendor/comment/header lines are allowed; malformed numeric data is not.
        try:
            freq = float(cells[0])
        except ValueError:
            continue
        if not math.isfinite(freq):
            raise ValueError(f"line {line_number}: frequency must be finite")
        if freq == 0:
            continue
        if not 2 <= len(cells) <= 4:
            raise ValueError(f"line {line_number}: expected frequency, dB, optional phase and coherence")
        mag = _number(cells[1], "magnitude", missing=True)
        phase = math.radians(_number(cells[2], "phase degrees", missing=True)) if len(cells) > 2 else math.nan
        row_coherence = _number(cells[3], "coherence", missing=True) if len(cells) > 3 else math.nan
        rows.append([freq, mag, phase, row_coherence])
    frequencies, magnitude_db, phase_rad, coherence = _rows(rows)
    if osm:
        notes.append("OSM text exports may omit stored phase adjustments; native .osm preserves them.")
    if phase_rad is None:
        notes.append("Magnitude only; phase and group delay are unavailable.")
    return Trace(id=uuid.uuid4().hex, name=name, frequencies=frequencies, magnitude_db=magnitude_db,
                 phase_rad=phase_rad, coherence=coherence, notes=" ".join(notes), raw=raw)


def load_trace(path: str | Path) -> Trace:
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("trace file exceeds 32 MiB")
    raw = path.read_bytes()
    if path.suffix.lower() == ".osm" or raw.lstrip().startswith(b"{"):
        return _native(raw, path.stem)
    return _text(raw, path.stem)


def _resample(response: Response, frequencies: FloatArray) -> tuple[FloatArray, FloatArray]:
    magnitude = np.full(len(frequencies), np.nan, dtype=np.float64)
    phase = np.full(len(frequencies), np.nan, dtype=np.float64)
    valid_values = np.where(np.isfinite(response.continuous_phase_rad), response.magnitude_db, np.nan)
    for part in finite_segments(valid_values):
        if len(part) < 2:
            continue
        source = response.frequencies[part]
        mask = (frequencies >= source[0]) & (frequencies <= source[-1])
        magnitude[mask] = np.interp(np.log(frequencies[mask]), np.log(source), response.magnitude_db[part])
        phase[mask] = np.interp(frequencies[mask], source, response.continuous_phase_rad[part])
    return magnitude, phase


def sum_traces(a: Trace, b: Trace, *, timing_confirmed: bool = False, coherence_threshold: float = 0) -> Response:
    if not timing_confirmed or a.phase_rad is None or b.phase_rad is None:
        raise ValueError("measured sums require phase and a confirmed common level/timing reference")
    if a.sample_rate_hz and b.sample_rate_hz and a.sample_rate_hz != b.sample_rate_hz:
        raise ValueError("measured traces have different sample rates")
    low = max(a.frequencies[0], b.frequencies[0])
    high = min(a.frequencies[-1], b.frequencies[-1])
    if low >= high:
        raise ValueError("measured traces have no overlapping frequency range")
    frequencies = _array(np.unique(np.concatenate([a.frequencies, b.frequencies])))
    frequencies = _array(frequencies[(frequencies >= low) & (frequencies <= high)])
    if len(frequencies) < 3:
        raise ValueError("too few overlapping samples")
    ma, pa = _resample(a.response(coherence_threshold=coherence_threshold), frequencies)
    mb, pb = _resample(b.response(coherence_threshold=coherence_threshold), frequencies)
    aa, ab = 10 ** (ma / 20), 10 ** (mb / 20)
    relative = aa + ab * np.exp(1j * (pb - pa))
    relative = np.where(np.abs(relative) < (aa + ab) * 1e-12, 0, relative)
    transfer = np.asarray(relative * np.exp(1j * pa), dtype=np.complex128)
    if not np.any(np.isfinite(transfer)):
        raise ValueError("no valid overlapping phase samples")
    phase = pa + unwrap_phase(np.angle(transfer * np.exp(-1j * pa)))
    envelope = aa + ab
    reference_peak = float(np.max(envelope[np.isfinite(envelope)], initial=0))
    return _make_response(frequencies, transfer, phase, notes=("Measured complex sum; common timing confirmed.",),
                          reference_peak=reference_peak)
