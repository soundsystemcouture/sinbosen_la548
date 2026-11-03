"""Estimated electrical transfer functions, computed locally without hardware access."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray
from scipy import signal

from . import codecs
from .settings import ChannelSettings, CrossoverSettings, DeviceSettings, EQBandSettings

type FloatArray = NDArray[np.float64]
type ComplexArray = NDArray[np.complex128]

MODEL_NOTE = "Estimated filter coefficients; the device's implementation requires verification."

# (section frequency / configured cutoff, Q); None denotes a first-order section.
# Identified from simultaneous-reference hardware sweeps at 1 kHz, independently
# checked at other cutoffs. The LA548's HP sections do not use scipy's Bessel
# pole locations. Scale each section's frequency BEFORE digital prewarping.
# See docs/FILTER_MODELS.md for measurements, calibration, and validation limits.
_BESSEL_HP_SECTIONS: dict[int, tuple[tuple[float, float | None], ...]] = {
    1: ((1.0, None),),
    2: ((0.841083, 0.609203),),
    3: ((0.601194, None), (0.794811, 0.689963)),
    4: ((0.680936, 0.519915), (0.680936, 0.810077)),
    5: ((0.482679, None), (0.726932, 0.559701), (0.726932, 0.920309)),
    6: ((0.626091, 0.507701), (0.626091, 0.613690), (0.626091, 1.018337)),
    7: ((0.404482, None), (0.682802, 0.529614), (0.682802, 0.660546), (0.682802, 1.129998)),
    8: ((0.592476, 0.499637), (0.592476, 0.580406), (0.592476, 0.698036), (0.592476, 1.232027)),
}

# Odd-order Linkwitz-Riley modes are exposed by the LA548 even though they are
# not part of the usual textbook LR family.  The values below are the LA548's
# damping constants, confirmed against HP/LP hardware captures.  Each
# second-order section has Q = 1/(2*damping).
_LR_ODD_DAMPING: dict[int, tuple[float, ...]] = {
    3: (0.70422535211,),
    5: (0.62, 0.62),
    7: (0.453134315, 0.92592592593, 0.453134315),
}


@dataclass(frozen=True)
class Response:
    frequencies: FloatArray
    transfer: ComplexArray
    magnitude_db: FloatArray
    phase_deg: FloatArray
    phase_unwrapped_deg: FloatArray
    group_delay_ms: FloatArray
    continuous_phase_rad: FloatArray
    complete: bool = True
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelResponse:
    total: Response
    contributions: tuple[tuple[str, Response], ...]


def frequency_grid(*, sample_rate_hz: int = 96000, low: float = 10, high: float = 20000,
                   points: int = 4096) -> FloatArray:
    if not 0 < low < high < sample_rate_hz / 2 or points < 3:
        raise ValueError("frequency range must be positive, below Nyquist, with at least 3 points")
    return np.asarray(np.geomspace(low, high, points), dtype=np.float64)


def normalize_eq(band: EQBandSettings) -> EQBandSettings:
    raw = codecs.eq_struct.encode((band.freq_hz, band.gain_db, band.q, band.slope, band.type, band.enabled))
    return EQBandSettings(*codecs.eq_struct.decode(raw), raw=raw)


def normalize_xover(block: CrossoverSettings) -> CrossoverSettings:
    if block.filter_type not in (0, 1, 2):
        raise ValueError("crossover family must be Butterworth, Linkwitz–Riley, or Bessel")
    raw = codecs.xover_struct.encode((block.freq_hz, block.filter_type, block.slope_db_per_oct, block.enabled))
    return CrossoverSettings(*codecs.xover_struct.decode(raw), raw=raw)


def _validate_frequencies(frequencies: FloatArray, sample_rate_hz: int) -> None:
    if frequencies.ndim != 1 or len(frequencies) < 3 or not np.all(np.isfinite(frequencies)):
        raise ValueError("frequencies must be a finite one-dimensional array with at least 3 points")
    if frequencies[0] <= 0 or frequencies[-1] >= sample_rate_hz / 2 or np.any(np.diff(frequencies) <= 0):
        raise ValueError("frequencies must increase, be positive, and remain below Nyquist")


def _make_response(frequencies: FloatArray, transfer: ComplexArray, phase: FloatArray | None = None,
                   *, complete: bool = True, notes: tuple[str, ...] = (),
                   group_delay_ms: FloatArray | None = None, reference_peak: float = 0) -> Response:
    magnitude = np.abs(transfer)
    finite = magnitude[np.isfinite(magnitude)]
    peak = max(float(np.max(finite)) if len(finite) else 0.0, reference_peak)
    valid = np.isfinite(magnitude) & (magnitude > max(peak * 1e-6, 1e-15))
    magnitude_db = np.asarray(20 * np.log10(np.maximum(magnitude, 1e-15)), dtype=np.float64)
    phase = np.asarray(unwrap_phase(np.angle(transfer)) if phase is None else phase, dtype=np.float64)
    wrapped = np.asarray(np.rad2deg(np.angle(transfer)), dtype=np.float64)
    unwrapped = np.asarray(np.rad2deg(phase), dtype=np.float64)
    delay = np.asarray(
        phase_group_delay(frequencies, phase)
        if group_delay_ms is None else group_delay_ms, dtype=np.float64,
    )
    wrapped = np.where(valid, wrapped, np.nan)
    unwrapped = np.where(valid, unwrapped, np.nan)
    delay = np.where(valid, delay, np.nan)
    arrays = [np.array(frequencies, copy=True), np.array(transfer, copy=True), magnitude_db, wrapped, unwrapped, delay,
              np.array(phase, copy=True)]
    for array in arrays:
        array.setflags(write=False)
    return Response(frequencies=arrays[0], transfer=arrays[1], magnitude_db=arrays[2], phase_deg=arrays[3],
                    phase_unwrapped_deg=arrays[4], group_delay_ms=arrays[5], continuous_phase_rad=arrays[6],
                    complete=complete,
                    notes=tuple(dict.fromkeys(notes)))


def finite_segments(values: FloatArray) -> list[NDArray[np.int64]]:
    indices = np.flatnonzero(np.isfinite(values))
    return [part for part in np.split(indices, np.flatnonzero(np.diff(indices) != 1) + 1) if len(part)]


def unwrap_phase(phase: FloatArray) -> FloatArray:
    result = np.full(len(phase), np.nan, dtype=np.float64)
    for part in finite_segments(phase):
        result[part] = np.unwrap(phase[part])
    return result


def phase_group_delay(frequencies: FloatArray, phase: FloatArray) -> FloatArray:
    result = np.full(len(phase), np.nan, dtype=np.float64)
    for part in finite_segments(phase):
        if len(part) >= 2:
            result[part] = -np.gradient(phase[part], frequencies[part], edge_order=2 if len(part) > 2 else 1)
    return np.asarray(result * 1000 / (2 * np.pi), dtype=np.float64)


def _sos_response(sos: FloatArray, frequencies: FloatArray, sample_rate_hz: int,
                  notes: tuple[str, ...] = ()) -> Response:
    transfer = np.ones(len(frequencies), dtype=np.complex128)
    phase = np.zeros(len(frequencies), dtype=np.float64)
    # Sum each section's continuous phase rather than unwrap the entire cascade.
    for section in sos:
        _, h = signal.freqz(section[:3], section[3:], worN=frequencies, fs=sample_rate_hz)
        transfer *= h
        phase += np.unwrap(np.angle(h))
    return _make_response(frequencies, transfer, phase, notes=notes)


def _shelf_sos(band: EQBandSettings, sample_rate_hz: int) -> FloatArray:
    # Hardware uses fixed shelf shapes (Q is ignored), with freq_hz specifying
    # the prototype corner rather than the half-gain midpoint. First-order
    # shelves have a right-half-plane zero: their phase is not minimum phase.
    # Zero gain is a firmware bypass, not the limit of the nonzero-gain curve.
    if band.gain_db == 0:
        return np.asarray([[1, 0, 0, 1, 0, 0]], dtype=np.float64)
    level = 10 ** (abs(band.gain_db) / 20)
    if band.slope == 0:
        if band.type == 1:
            numerator = [-1, level] if band.gain_db > 0 else [-1, 1]
            denominator = [1, 1] if band.gain_db > 0 else [1, level]
        else:
            numerator = [-level, 1] if band.gain_db > 0 else [-1, 1]
            denominator = [1, 1] if band.gain_db > 0 else [level, 1]
    else:
        numerator = ([1, np.sqrt(2 * level), level] if band.type == 1
                     else [level, np.sqrt(2 * level), 1])
        denominator = [1, np.sqrt(2), 1]
        if band.gain_db < 0:
            numerator, denominator = denominator, numerator
    # Bilinear substitution for normalized s: s=(1-z^-1)/(t*(1+z^-1)).
    t = np.tan(np.pi * band.freq_hz / sample_rate_hz)
    b, a = signal.bilinear(numerator, denominator, fs=1 / (2 * t))
    section = np.r_[np.pad(b, (0, 3 - len(b))), np.pad(a, (0, 3 - len(a)))]
    return np.asarray([section], dtype=np.float64)


def eq_response(band: EQBandSettings, frequencies: FloatArray, *, sample_rate_hz: int = 96000) -> Response:
    _validate_frequencies(frequencies, sample_rate_hz)
    if not band.enabled:
        return _make_response(frequencies, np.ones(len(frequencies), dtype=np.complex128))
    if band.type not in (0, 1, 2, 3, 4, 5):
        return _make_response(frequencies, np.ones(len(frequencies), dtype=np.complex128), complete=False,
                              notes=(f"Unresolved EQ type {band.type} omitted.",))
    # Shelves: 0=first order, 1=second order. For EQ, AllPass1 and notch,
    # 0/1 produced the same response. Only AllPass2 was also checked at 2/3.
    if band.slope not in (0, 1) and band.type != 4:
        return _make_response(frequencies, np.ones(len(frequencies), dtype=np.complex128), complete=False,
                              notes=(f"EQ slope {band.slope} has unresolved semantics; band omitted.",))
    if not 0 < band.freq_hz < sample_rate_hz / 2 or not np.isfinite(band.q) or band.q <= 0:
        raise ValueError("invalid EQ frequency or Q")
    if band.type in (1, 2):
        return _sos_response(_shelf_sos(band, sample_rate_hz), frequencies, sample_rate_hz)
    omega = 2 * np.pi * band.freq_hz / sample_rate_hz
    c, s = np.cos(omega), np.sin(omega)
    alpha = s / (2 * band.q)
    a = 10 ** (band.gain_db / 40)
    notes: tuple[str, ...] = ()
    if band.type == 3:
        # First-order allpass, phase-prewarped so phase is exactly -90 degrees
        # at freq_hz by construction; hardware shows no Q/gain dependence.
        t = s / (1 + c)
        a1 = (t - 1) / (t + 1)
        b = [a1, 1.0, 0.0]
        denominator = [1.0, a1, 0.0]
    elif band.type == 5:
        if band.gain_db >= 0:
            # Confirmed flat for zero/positive gain; only negative gain notches.
            b = [1.0, 0.0, 0.0]
            denominator = [1.0, 0.0, 0.0]
        else:
            # The device switches notch formulas at -6 dB (confirmed on
            # hardware). Deep cuts use the -3 dB bandwidth definition;
            # shallow cuts use the same coefficients as peaking EQ. A line
            # fitted to deep cuts extrapolates incorrectly across this branch.
            level = a * a  # linear amplitude at the notch center
            q_effective = (band.q * np.sqrt(1 / level - 2 * level)
                           if band.gain_db <= -6 else band.q * a)
            notch_alpha = s / (2 * q_effective)
            b = [1 + notch_alpha * a, -2 * c, 1 - notch_alpha * a]
            denominator = [1 + notch_alpha / a, -2 * c, 1 - notch_alpha / a]
    elif band.type == 0:
        q_effective = band.q / 10 ** (abs(band.gain_db) / 40)
        peaking_alpha = s / (2 * q_effective)
        b = [1 + peaking_alpha * a, -2 * c, 1 - peaking_alpha * a]
        denominator = [1 + peaking_alpha / a, -2 * c, 1 - peaking_alpha / a]
    elif band.type == 4:
        b = [1 - alpha, -2 * c, 1 + alpha]
        denominator = [1 + alpha, -2 * c, 1 - alpha]
    sos = np.asarray([b + denominator], dtype=np.float64)
    sos /= denominator[0]
    return _sos_response(sos, frequencies, sample_rate_hz, notes=notes)


def _bessel_highpass_sos(order: int, cutoff_hz: float, sample_rate_hz: int) -> FloatArray:
    sections = []
    for scale, q in _BESSEL_HP_SECTIONS[order]:
        omega = 2 * np.pi * cutoff_hz * scale / sample_rate_hz
        if q is None:
            t = np.tan(omega / 2)
            sections.append([1 / (1 + t), -1 / (1 + t), 0, 1, (t - 1) / (t + 1), 0])
        else:
            c = np.cos(omega)
            alpha = np.sin(omega) / (2 * q)
            section = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha])
            sections.append((section / (1 + alpha)).tolist())
    return np.asarray(sections, dtype=np.float64)


def _lr_odd_sos(order: int, cutoff_hz: float, band: str, sample_rate_hz: int) -> FloatArray:
    # The 6 dB/oct mode moves the HP and LP poles in opposite directions so
    # both sides are -6 dB at the configured crossover.  Higher odd orders use
    # an unshifted first-order section followed by the _LR_ODD_DAMPING biquads.
    first_scale = (1.732 if band == "hp" else 1 / 1.732) if order == 1 else 1.0
    omega = 2 * np.pi * cutoff_hz * first_scale / sample_rate_hz
    t = np.tan(omega / 2)
    if band == "hp":
        sections = [[1 / (1 + t), -1 / (1 + t), 0, 1, (t - 1) / (t + 1), 0]]
    else:
        sections = [[t / (1 + t), t / (1 + t), 0, 1, (t - 1) / (t + 1), 0]]
    omega = 2 * np.pi * cutoff_hz / sample_rate_hz
    c, s = np.cos(omega), np.sin(omega)
    for damping in _LR_ODD_DAMPING.get(order, ()):
        alpha = damping * s
        if band == "hp":
            numerator = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2])
        else:
            numerator = np.array([(1 - c) / 2, 1 - c, (1 - c) / 2])
        denominator = np.array([1 + alpha, -2 * c, 1 - alpha])
        sections.append(np.r_[numerator / denominator[0], denominator / denominator[0]].tolist())
    return np.asarray(sections, dtype=np.float64)


def crossover_response(block: CrossoverSettings, band: str, frequencies: FloatArray,
                       *, sample_rate_hz: int = 96000) -> Response:
    _validate_frequencies(frequencies, sample_rate_hz)
    if band not in ("hp", "lp"):
        raise ValueError("crossover band must be hp or lp")
    if not block.enabled:
        return _make_response(frequencies, np.ones(len(frequencies), dtype=np.complex128))
    order = block.slope_db_per_oct // 6
    if block.filter_type not in (0, 1, 2) or block.slope_db_per_oct not in range(6, 49, 6):
        return _make_response(frequencies, np.ones(len(frequencies), dtype=np.complex128), complete=False,
                              notes=("Unsupported crossover family or slope omitted.",))
    btype = "highpass" if band == "hp" else "lowpass"
    if not 0 < block.freq_hz < sample_rate_hz / 2:
        raise ValueError("invalid crossover frequency")
    notes: tuple[str, ...] = ()
    if block.filter_type == 1 and order % 2:
        sos = _lr_odd_sos(order, block.freq_hz, band, sample_rate_hz)
        notes = ("Odd-order Linkwitz–Riley uses LA548-specific sections; "
                 "see docs/FILTER_MODELS.md for measured validation.",)
    elif block.filter_type == 2:
        if band == "hp":
            sos = _bessel_highpass_sos(order, block.freq_hz, sample_rate_hz)
            notes = ("Bessel highpass uses sections calibrated against LA548 hardware; "
                     "see docs/FILTER_MODELS.md for the measured ranges.",)
        else:
            sos = signal.bessel(order, block.freq_hz, btype=btype, norm="mag", fs=sample_rate_hz, output="sos")
    else:
        sos = signal.butter(order // 2 if block.filter_type == 1 else order, block.freq_hz,
                            btype=btype, fs=sample_rate_hz, output="sos")
        if block.filter_type == 1:
            sos = np.concatenate([sos, sos])
    return _sos_response(np.asarray(sos, dtype=np.float64), frequencies, sample_rate_hz, notes=notes)


def compose_responses(*responses: Response) -> Response:
    if not responses:
        raise ValueError("at least one response is required")
    frequencies = responses[0].frequencies
    transfer = np.ones(len(frequencies), dtype=np.complex128)
    phase = np.zeros(len(frequencies), dtype=np.float64)
    delay = np.zeros(len(frequencies), dtype=np.float64)
    notes: list[str] = []
    for response in responses:
        if not np.array_equal(frequencies, response.frequencies):
            raise ValueError("response frequency grids must match")
        transfer *= response.transfer
        phase += response.continuous_phase_rad
        delay += np.nan_to_num(response.group_delay_ms)
        notes.extend(response.notes)
    return _make_response(frequencies, transfer, phase, complete=all(r.complete for r in responses),
                          notes=tuple(notes), group_delay_ms=delay)


def sum_responses(*responses: Response) -> Response:
    if not responses:
        raise ValueError("at least one response is required")
    frequencies = responses[0].frequencies
    if any(not np.array_equal(frequencies, r.frequencies) for r in responses):
        raise ValueError("response frequency grids must match")
    transfer = np.asarray(sum((r.transfer for r in responses), np.zeros(len(frequencies), dtype=np.complex128)))
    reference_peak = float(np.max(sum((np.abs(r.transfer) for r in responses),
                                     np.zeros(len(frequencies), dtype=np.float64))))
    derivative = np.zeros(len(frequencies), dtype=np.complex128)
    for r in responses:
        log_magnitude_derivative = np.gradient(np.log(np.maximum(np.abs(r.transfer), 1e-15)), frequencies)
        phase_derivative = -2 * np.pi * np.nan_to_num(r.group_delay_ms) / 1000
        derivative += r.transfer * (log_magnitude_derivative + 1j * phase_derivative)
    with np.errstate(divide="ignore", invalid="ignore"):
        delay = np.asarray(-np.imag(derivative / transfer) * 1000 / (2 * np.pi), dtype=np.float64)
    reference_phase = responses[0].continuous_phase_rad
    phase = reference_phase + unwrap_phase(np.angle(transfer * np.exp(-1j * reference_phase)))
    return _make_response(frequencies, transfer, phase, complete=all(r.complete for r in responses),
                          notes=tuple(n for r in responses for n in r.notes), group_delay_ms=delay,
                          reference_peak=reference_peak)


def channel_response(channel: ChannelSettings, frequencies: FloatArray | None = None,
                     *, sample_rate_hz: int = 96000) -> ChannelResponse:
    frequencies = frequency_grid(sample_rate_hz=sample_rate_hz) if frequencies is None else frequencies
    _validate_frequencies(frequencies, sample_rate_hz)
    base = channel.base
    phase = -2 * np.pi * frequencies * base.delay_ms / 1000 + (np.pi if base.inverted else 0)
    level = 0 if base.muted else 10 ** (base.gain_db / 20)
    transfer = np.asarray(level * np.exp(1j * phase), dtype=np.complex128)
    contributions: list[tuple[str, Response]] = [
        ("Gain / polarity / delay", _make_response(frequencies, transfer, phase,
         group_delay_ms=np.full(len(frequencies), base.delay_ms, dtype=np.float64)))
    ]
    for name, block in (("HP crossover", channel.hp), ("LP crossover", channel.lp)):
        if block is not None and block.enabled:
            contributions.append((name, crossover_response(block, "hp" if name.startswith("HP") else "lp",
                                                           frequencies, sample_rate_hz=sample_rate_hz)))
    for i, eq in enumerate(channel.eq_bands):
        if eq.enabled:
            label = "Allpass" if eq.type in (3, 4) else "Notch" if eq.type == 5 else "EQ"
            contributions.append((f"{label} {i + 1}",
                                  eq_response(eq, frequencies, sample_rate_hz=sample_rate_hz)))
    notes = [MODEL_NOTE]
    if channel.fir.enabled:
        notes.append("Enabled FIR omitted: coefficients and FIR interpretation are not integrated yet.")
    if any(b.enabled for b in channel.dynamic_eq_bands):
        notes.append("Dynamic EQ is level dependent and omitted from the static response.")
    if any(b is not None and b.enabled for b in (channel.compressor, channel.limiter, channel.expander)):
        notes.append("Dynamics are level dependent and omitted from the static response.")
    if channel.frequency_shifter is not None and channel.frequency_shifter.enabled:
        notes.append("Frequency shifting has no static transfer function.")
    if channel.input_source is not None and channel.input_source.source_select != 0:
        notes.append("Selecting an internal generator (sine/pink/white noise) replaces the external "
                     "signal entirely; a static transfer-function model cannot represent that "
                     "substitution and omits it here.")
    total = compose_responses(*(r for _, r in contributions))
    total = replace(total, complete=total.complete and len(notes) == 1,
                    notes=tuple(dict.fromkeys((*total.notes, *notes))))
    return ChannelResponse(total=total, contributions=tuple(contributions))


def path_response(settings: DeviceSettings, input_id: int, output_id: int,
                  frequencies: FloatArray | None = None) -> Response:
    if not 0 <= input_id < len(settings.inputs) or not 0 <= output_id < len(settings.outputs):
        raise ValueError("input/output channel out of range")
    rate = settings.profile.sample_rate_hz
    frequencies = frequency_grid(sample_rate_hz=rate) if frequencies is None else frequencies
    if not settings.mix_matrix[input_id][output_id]:
        return _make_response(frequencies, np.zeros(len(frequencies), dtype=np.complex128),
                              notes=("Selected input is not routed to this output.",))
    return compose_responses(channel_response(settings.inputs[input_id], frequencies, sample_rate_hz=rate).total,
                             channel_response(settings.outputs[output_id], frequencies, sample_rate_hz=rate).total)
