"""Payload codecs for the generic parameter-write opcode (0xA4).

Each `Codec` pairs the encode/decode logic for one control's payload shape.
`registry.ParamSpec` entries reference these; nothing outside this module
should need to hand-roll payload bytes for a write (see docs/PROTOCOL.md,
"Unsafe opcodes", for why that discipline matters).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Codec[T]:
    encode: Callable[[T], bytes]
    decode: Callable[[bytes], T]


# Display-unit limits: the same ranges the manufacturer's app offers for
# each control (docs/PROTOCOL.md, "Value bounds"). Encoders raise outside
# them rather than clamping, so a caller error is never turned into a
# different, well-formed frame. Decoders stay permissive: disabled blocks
# read back values outside these ranges (factory compressor threshold
# 340 dB, ratio 0).
VOLUME_DB = (-80.0, 12.0)  # input/output volume, input source gain
DYN_THRESHOLD_DB = (-60.0, 20.0)  # Compressor, Limiter, Dynamic EQ
EXPANDER_THRESHOLD_DB = (-100.0, 0.0)  # Expander
DYN_START_MS = (0.3, 200.0)  # all four dynamics blocks
DYN_RELEASE_MS = (50.0, 5000.0)  # all four dynamics blocks
DYN_RATIO = (1.0, 20.0)  # Expander, Dynamic EQ
COMPRESSOR_RATIO = (1.0, 128.0)  # Compressor
FIR_FREQ_HZ = (10, 20000)  # FIR low/high frequency
FIR_ATTENUATION_DB = (21.0, 120.0)  # FIR attenuation
INPUT_SOURCE_FREQ_MAX_HZ = 20000  # app range 10-20000 Hz; floor not enforced, see below


def _require_range(value: float, bounds: tuple[float, float], what: str, unit: str = "") -> None:
    low, high = bounds
    if not low <= value <= high:  # also rejects NaN
        raise ValueError(f"{what} out of range: {value}{unit} ({low:g} to {high:g}{unit})")


def _encode_u8(value: int) -> bytes:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"u8 value out of range: {value}")
    return bytes((value,))


def _decode_u8(data: bytes) -> int:
    return data[0]


u8: Codec[int] = Codec(encode=_encode_u8, decode=_decode_u8)


def _encode_u16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"u16 value out of range: {value}")
    return value.to_bytes(2, "little")


def _decode_u16(data: bytes) -> int:
    return int.from_bytes(data[:2], "little")


u16: Codec[int] = Codec(encode=_encode_u16, decode=_decode_u16)


def _encode_mute(muted: bool) -> bytes:
    # Confirmed inverted: 0x00 = muted, 0x01 = unmuted. This exact field
    # caused the 0xA4 incident (docs/PROTOCOL.md, "Unsafe opcodes") - a guessed
    # payload of 0x00 was assumed to be a harmless default and was actually
    # "mute this channel".
    return b"\x00" if muted else b"\x01"


def _decode_mute(data: bytes) -> bool:
    return data[0] == 0x00


mute_u8: Codec[bool] = Codec(encode=_encode_mute, decode=_decode_mute)


def _encode_volume(db: float) -> bytes:
    # raw = trunc((dB + 80) * 10), little-endian u16 - truncating, not
    # round(); int() truncates toward zero, which matches for non-negative
    # operands. Range -80..+12 dB (VOLUME_DB); the explicit floor matters
    # because truncation would otherwise let e.g. -80.09 through as raw 0.
    #
    # round(x, 6) before int(): binary floats can't represent most decimal
    # fractions exactly (e.g. 1.15 stores as ...114999999999999911...), so a
    # value that should truncate cleanly can land a hair under its intended
    # integer (1.15*100 == 114.99999999999999, int() -> 114, not 115) and
    # silently degrade by one raw step. Rounding to 6 decimal places first
    # clears that ~1e-13-scale float noise without touching genuine
    # fractional truncation (e.g. 49.9 stays 49.9, still truncates to 49) -
    # applied to every offset-then-scale formula below for the same reason.
    _require_range(db, VOLUME_DB, "volume", " dB")
    raw = int(round((db + 80.0) * 10, 6))
    return raw.to_bytes(2, "little")


def _decode_volume(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 10.0 - 80.0


volume_u16: Codec[float] = Codec(encode=_encode_volume, decode=_decode_volume)


def _encode_bool_u8(value: bool) -> bytes:
    # Identity, NOT inverted - unlike mute_u8. Hardware tests of EQ,
    # crossover, dynamics, input source and frequency shifter confirm True
    # engages the block.
    return b"\x01" if value else b"\x00"


def _decode_bool_u8(data: bytes) -> bool:
    return data[0] != 0x00


bool_u8: Codec[bool] = Codec(encode=_encode_bool_u8, decode=_decode_bool_u8)

# Plain identity, like bool_u8 - not Mute's inversion: inverted=True ->
# raw 1 (180 degrees), False -> raw 0. hardware_test/test_20_phase.py
# confirms each field flips the measured polarity on its own, but audio
# can't tell which raw value is "inverted": the unit has a baseline
# inversion of its own, and swapping the mapping for the input and output
# field together cancels out in the measured sign, so both mappings fit the
# same measurements (see docs/PROTOCOL.md). The absolute direction follows
# the usual convention that a polarity control's active state is the
# 180-degree one; it is not confirmed on hardware.
phase_u8: Codec[bool] = bool_u8


def _encode_freq(hz: int) -> bytes:
    # raw u16 == Hz directly, no transform. Range 10-20000 Hz, the same range
    # the manufacturer's app offers.
    if not 10 <= hz <= 20000:
        raise ValueError(f"frequency out of range: {hz} Hz (10-20000)")
    return int(hz).to_bytes(2, "little")


def _decode_freq(data: bytes) -> int:
    return int.from_bytes(data[:2], "little")


freq_u16: Codec[int] = Codec(encode=_encode_freq, decode=_decode_freq)


def _encode_xover_slope(db_per_oct: int) -> bytes:
    # display = (raw + 1) * 6  =>  raw = display/6 - 1, raw range 0-7 (i.e.
    # display must be a multiple of 6, 6-48). The manufacturer's app only
    # offers these 8 discrete steps - silently coercing an arbitrary value
    # (e.g. 45 -> 48, or worse, 51 -> 48) would change a live crossover slope
    # to something the caller never asked for with no indication.
    if db_per_oct % 6 != 0:
        raise ValueError(f"xover slope must be a multiple of 6 dB/oct: {db_per_oct}")
    raw = db_per_oct // 6 - 1
    if not 0 <= raw <= 7:
        raise ValueError(f"xover slope out of range: {db_per_oct} dB/oct (6-48)")
    return bytes((raw,))


def _decode_xover_slope(data: bytes) -> int:
    return (data[0] + 1) * 6


xover_slope_u8: Codec[int] = Codec(encode=_encode_xover_slope, decode=_decode_xover_slope)


def _encode_eq_gain(db: float) -> bytes:
    # raw = trunc((dB + 20) * 10), range +-20 dB - truncating, same as
    # volume_u16 above.
    if not -20.0 <= db <= 20.0:
        raise ValueError(f"EQ gain out of range: {db} dB (+-20 dB)")
    raw = int(round((db + 20.0) * 10, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_eq_gain(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 10.0 - 20.0


eq_gain_u16: Codec[float] = Codec(encode=_encode_eq_gain, decode=_decode_eq_gain)


def _encode_eq_q(q: float) -> bytes:
    # Bandwidth-in-octaves encoding (Q confirmed against measured response
    # in hardware_test/test_50_eq_band.py):
    #   n = 2 * log2( (1 + sqrt(1 + 4*Q^2)) / (2*Q) )   # octaves, 0.05-3.0
    #   raw = floor((n - 0.05) * 100 + 0.5)              # round-half-up
    # (algebraically identical to n = 2*log2(1/(2Q) + sqrt(1/(4Q^2)+1))).
    if q <= 0:
        raise ValueError(f"Q must be positive: {q}")
    n_octaves = 2.0 * math.log2((1.0 + math.sqrt(1.0 + 4.0 * q * q)) / (2.0 * q))
    # floor(), not int(): for Q above the encodable maximum (~28.85),
    # (n_octaves - 0.05) * 100 + 0.5 goes slightly negative before the range
    # check below - int() truncates a small negative float *toward* zero
    # (e.g. int(-0.38) == 0), silently letting an unencodable Q through as
    # raw 0 instead of being rejected. floor() correctly goes the other way.
    raw = math.floor((n_octaves - 0.05) * 100 + 0.5)
    if not 0 <= raw <= 295:
        raise ValueError(f"Q out of encodable range: {q} (raw {raw}, valid raw 0-295)")
    return raw.to_bytes(2, "little")


def _decode_eq_q(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    n_octaves = raw * 0.01 + 0.05
    return float(2.0 ** (n_octaves / 2.0) / (2.0**n_octaves - 1.0))


eq_q_u16: Codec[float] = Codec(encode=_encode_eq_q, decode=_decode_eq_q)


def _encode_xover_struct(fields: tuple[int, int, int, bool]) -> bytes:
    # Packs the crossover block's 4 fields in wire order: freq, type, slope,
    # enable (u16, u8, u8, u8). Offsets confirmed by a read_group() byte
    # diff (they match this layout bit-for-bit). The whole struct goes out
    # as one frame with data_id=0, confirmed on hardware to shape the output
    # signal (hardware_test/test_40_xover.py); one frame per field (data_id
    # 38-41) is ACK'd but has no effect.
    freq_hz, filter_type, slope_db_per_oct, enabled = fields
    # The manufacturer's app offers only 3 types (0=Butterworth,
    # 1=Linkwitz-Riley, 2=Bessel - see docs/PROTOCOL.md) - same reasoning as
    # eq_type's 0-5 check in _encode_eq_struct below.
    if not 0 <= filter_type <= 2:
        raise ValueError(f"xover filter_type out of range: {filter_type} (0-2)")
    return (
        freq_u16.encode(freq_hz)
        + u8.encode(filter_type)
        + xover_slope_u8.encode(slope_db_per_oct)
        + bool_u8.encode(enabled)
    )


def _decode_xover_struct(data: bytes) -> tuple[int, int, int, bool]:
    return (
        freq_u16.decode(data[0:2]),
        u8.decode(data[2:3]),
        xover_slope_u8.decode(data[3:4]),
        bool_u8.decode(data[4:5]),
    )


xover_struct: Codec[tuple[int, int, int, bool]] = Codec(encode=_encode_xover_struct, decode=_decode_xover_struct)


def _encode_eq_struct(fields: tuple[int, float, float, int, int, bool]) -> bytes:
    # Packs the EQ band's 6 fields in wire order: freq, gain, Q, slope, type,
    # enable (u16, u16, u16, u8, u8, u8). Offsets confirmed by a read_group()
    # byte diff; as with xover_struct above, the whole struct goes out as one
    # frame with data_id=0, not one frame per field (hardware_test/
    # test_50_eq_band.py).
    freq_hz, gain_db, q, slope, eq_type, enabled = fields
    # The manufacturer's app offers 6 types (0=peaking EQ, 1=low shelf,
    # 2=high shelf, 3=1st-order all-pass, 4=2nd-order all-pass, 5=notch
    # (negative gain only, bandwidth convention switches at -6 dB) - see
    # docs/PROTOCOL.md) - anything outside 0-5 can't be reached from the app
    # and risks undefined firmware behavior, same precedent as
    # xover_slope_u8's own bound.
    if not 0 <= eq_type <= 5:
        raise ValueError(f"eq type out of range: {eq_type} (0-5)")
    # Slope: 0 = 6 dB, 1 = 12 dB, the only two options the app offers.
    if slope not in (0, 1) or isinstance(slope, bool):
        raise ValueError(f"EQ slope out of range: {slope} (0-1)")
    return (
        freq_u16.encode(freq_hz)
        + eq_gain_u16.encode(gain_db)
        + eq_q_u16.encode(q)
        + u8.encode(slope)
        + u8.encode(eq_type)
        + bool_u8.encode(enabled)
    )


def _decode_eq_struct(data: bytes) -> tuple[int, float, float, int, int, bool]:
    return (
        freq_u16.decode(data[0:2]),
        eq_gain_u16.decode(data[2:4]),
        eq_q_u16.decode(data[4:6]),
        u8.decode(data[6:7]),
        u8.decode(data[7:8]),
        bool_u8.decode(data[8:9]),
    )


eq_struct: Codec[tuple[int, float, float, int, int, bool]] = Codec(encode=_encode_eq_struct, decode=_decode_eq_struct)


def _encode_delay(ms: float) -> bytes:
    # Piecewise; both branches truncate, not round(). Confirmed on hardware
    # across the 49-50 ms breakpoint (hardware_test/test_30_delay.py).
    if not 0.0 <= ms <= 1200.0:
        raise ValueError(f"delay out of range: {ms} ms (0-1200)")
    # see _encode_volume for why round(x, 6)
    raw = int(round(ms * 100.0, 6)) if ms < 50.0 else int(round(ms + 4950.0, 6))
    return raw.to_bytes(2, "little")


def _decode_delay(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 100.0 if raw <= 4999 else raw - 4950


delay_u16: Codec[float] = Codec(encode=_encode_delay, decode=_decode_delay)


def _encode_dyneq_starttime(ms: float) -> bytes:
    # Piecewise, truncating (same style as delay_u16/eq_gain_u16 above).
    # Compressor/Limiter/Expander use this exact formula too - the name is
    # kept rather than renamed to avoid unrelated churn in the Dynamic EQ
    # registry entries.
    #   ms < 1.0:  raw = 7 - (1.0 - ms) * 10   (0.3-0.9 ms in 0.1 ms steps)
    #   ms >= 1.0: raw = ms + 6                (1 ms steps beyond that)
    # The 0.3 ms floor also falls out of the formula (raw can't go
    # negative); the 200 ms ceiling is the app's (DYN_START_MS).
    _require_range(ms, DYN_START_MS, "start time", " ms")
    # see _encode_volume for why round(x, 6)
    raw = int(round(7.0 - (1.0 - ms) * 10.0, 6)) if ms < 1.0 else int(round(ms + 6.0, 6))
    return raw.to_bytes(2, "little")


def _decode_dyneq_starttime(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return 1.0 - (7 - raw) / 10.0 if raw < 7 else float(raw - 6)


dyneq_starttime_u16: Codec[float] = Codec(encode=_encode_dyneq_starttime, decode=_decode_dyneq_starttime)


def _encode_dyneq_release_time(ms: float) -> bytes:
    # raw = ms - 50, truncating. Range 50..5000 ms (DYN_RELEASE_MS).
    # Compressor/Limiter/Expander use the same formula - name kept as-is,
    # see dyneq_starttime_u16.
    _require_range(ms, DYN_RELEASE_MS, "release time", " ms")
    raw = int(round(ms - 50.0, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_dyneq_release_time(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return float(raw + 50)


dyneq_release_time_u16: Codec[float] = Codec(encode=_encode_dyneq_release_time, decode=_decode_dyneq_release_time)


def _encode_dyneq_ratio(ratio: float) -> bytes:
    # raw = (ratio - 1.0) * 100, truncating. Range 1..20 (DYN_RATIO,
    # Dynamic EQ and Expander alike). Expander's ratio field uses the same
    # formula - name kept as-is, see dyneq_starttime_u16. Compressor's ratio
    # uses a different formula - see compressor_ratio_u16 below.
    _require_range(ratio, DYN_RATIO, "compression ratio")
    raw = int(round((ratio - 1.0) * 100.0, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_dyneq_ratio(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 100.0 + 1.0


dyneq_ratio_u16: Codec[float] = Codec(encode=_encode_dyneq_ratio, decode=_decode_dyneq_ratio)


def _encode_dyneq_threshold(db: float) -> bytes:
    # raw = (dB + 60.0) * 2, truncating. Range -60..+20 dB for Dynamic EQ,
    # Compressor and Limiter alike (DYN_THRESHOLD_DB). Compressor and
    # Limiter use the same formula - name kept as-is, see
    # dyneq_starttime_u16. Expander's threshold uses a different offset -
    # see expander_threshold_u16 below.
    _require_range(db, DYN_THRESHOLD_DB, "threshold", " dB")
    raw = int(round((db + 60.0) * 2.0, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_dyneq_threshold(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 2.0 - 60.0


dyneq_threshold_u16: Codec[float] = Codec(encode=_encode_dyneq_threshold, decode=_decode_dyneq_threshold)


def _encode_dyneq_struct(
    fields: tuple[int, float, float, int, int, int, float, float, float, float, bool],
) -> bytes:
    # Packs the Dynamic EQ band's 11 fields in wire order: freq, gain, Q,
    # slope, type, mode, threshold, start time, release time, ratio, enable
    # (u16, u16, u16, u8, u8, u8, u16, u16, u16, u16, u8 - 18 bytes total).
    # As with xover_struct/eq_struct above, the whole struct goes out as one
    # frame with data_id=0. One frame per field (data_id 55-65) is ACK'd but
    # has no measurable effect, while this whole-struct write reaches storage
    # and processes audio (hardware_test/test_90_dyneq_wire_format.py,
    # test_91_dyneq_audible_effect.py).
    (
        freq_hz,
        gain_db,
        qvalue,
        slope,
        dyneq_type,
        startup_mode,
        threshold_db,
        start_time_ms,
        release_time_ms,
        ratio,
        enabled,
    ) = fields
    if not 0 <= slope <= 1:
        raise ValueError(f"dynamic EQ slope out of range: {slope} (0-1)")
    if not 0 <= dyneq_type <= 4:
        raise ValueError(f"dynamic EQ type out of range: {dyneq_type} (0-4)")
    if not 0 <= startup_mode <= 1:
        raise ValueError(f"dynamic EQ startup mode out of range: {startup_mode} (0-1)")
    return (
        freq_u16.encode(freq_hz)
        + eq_gain_u16.encode(gain_db)
        + eq_q_u16.encode(qvalue)
        + u8.encode(slope)
        + u8.encode(dyneq_type)
        + u8.encode(startup_mode)
        + dyneq_threshold_u16.encode(threshold_db)
        + dyneq_starttime_u16.encode(start_time_ms)
        + dyneq_release_time_u16.encode(release_time_ms)
        + dyneq_ratio_u16.encode(ratio)
        + bool_u8.encode(enabled)
    )


def _decode_dyneq_struct(data: bytes) -> tuple[int, float, float, int, int, int, float, float, float, float, bool]:
    return (
        freq_u16.decode(data[0:2]),
        eq_gain_u16.decode(data[2:4]),
        eq_q_u16.decode(data[4:6]),
        u8.decode(data[6:7]),
        u8.decode(data[7:8]),
        u8.decode(data[8:9]),
        dyneq_threshold_u16.decode(data[9:11]),
        dyneq_starttime_u16.decode(data[11:13]),
        dyneq_release_time_u16.decode(data[13:15]),
        dyneq_ratio_u16.decode(data[15:17]),
        bool_u8.decode(data[17:18]),
    )


dyneq_struct: Codec[tuple[int, float, float, int, int, int, float, float, float, float, bool]] = Codec(
    encode=_encode_dyneq_struct, decode=_decode_dyneq_struct
)


def _encode_expander_threshold(db: float) -> bytes:
    # raw = (dB + 100.0) * 2, truncating - same shape as
    # dyneq_threshold_u16's formula but a -100 dB offset instead of -60 dB
    # (Expander's threshold range goes lower than a gate/compressor's, which
    # matches its purpose - gating out low-level noise). Range -100..0 dB
    # (EXPANDER_THRESHOLD_DB).
    _require_range(db, EXPANDER_THRESHOLD_DB, "expander threshold", " dB")
    raw = int(round((db + 100.0) * 2.0, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_expander_threshold(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 2.0 - 100.0


expander_threshold_u16: Codec[float] = Codec(encode=_encode_expander_threshold, decode=_decode_expander_threshold)


def _encode_compressor_ratio(ratio: float) -> bytes:
    # raw = ratio * 10, truncating - unlike Expander's ratio formula
    # (dyneq_ratio_u16, an offset-then-scale shape), this one has no offset,
    # so the formula alone would accept 0.0. The manufacturer's app limits it
    # to 1..128 (COMPRESSOR_RATIO); factory readback of a disabled compressor
    # shows 0.0, which decodes but can't be re-sent.
    _require_range(ratio, COMPRESSOR_RATIO, "compressor ratio")
    raw = int(round(ratio * 10.0, 6))  # see _encode_volume for why round(x, 6)
    return raw.to_bytes(2, "little")


def _decode_compressor_ratio(data: bytes) -> float:
    raw = int.from_bytes(data[:2], "little")
    return raw / 10.0


compressor_ratio_u16: Codec[float] = Codec(encode=_encode_compressor_ratio, decode=_decode_compressor_ratio)


def _encode_compressor_struct(fields: tuple[float, float, float, float, bool]) -> bytes:
    # Compressor, Limiter and Expander share one wire layout: threshold,
    # start time, release time, ratio, enable, detection type (u16, u16,
    # u16, u16, u8, u8 - 10 bytes total, no padding). Each block leaves the
    # field it doesn't use at 0; Compressor has no detection type. As with
    # xover_struct/eq_struct/dyneq_struct above, the whole struct goes out as
    # one frame with data_id=0, not one frame per field (data_id 14-18).
    # Confirmed by readback and audio on output channels
    # (hardware_test/test_a0_dynamics_wire_format.py,
    # test_a1_dynamics_audible_effect.py).
    threshold_db, start_time_ms, release_time_ms, ratio, enabled = fields
    return (
        dyneq_threshold_u16.encode(threshold_db)
        + dyneq_starttime_u16.encode(start_time_ms)
        + dyneq_release_time_u16.encode(release_time_ms)
        + compressor_ratio_u16.encode(ratio)
        + bool_u8.encode(enabled)
        + u8.encode(0)
    )


def _decode_compressor_struct(data: bytes) -> tuple[float, float, float, float, bool]:
    return (
        dyneq_threshold_u16.decode(data[0:2]),
        dyneq_starttime_u16.decode(data[2:4]),
        dyneq_release_time_u16.decode(data[4:6]),
        compressor_ratio_u16.decode(data[6:8]),
        bool_u8.decode(data[8:9]),
    )


compressor_struct: Codec[tuple[float, float, float, float, bool]] = Codec(
    encode=_encode_compressor_struct, decode=_decode_compressor_struct
)


def _encode_limiter_struct(fields: tuple[float, float, float, bool, int]) -> bytes:
    # Same wire layout as compressor_struct above, but Limiter has no ratio
    # (sent as 0) and does use detection type: 0 is average-sensitive (RMS)
    # and 1 peak-sensitive, as measured on hardware - the only two settings
    # the manufacturer's app offers.
    threshold_db, start_time_ms, release_time_ms, enabled, detection_type = fields
    if detection_type not in (0, 1) or isinstance(detection_type, bool):
        raise ValueError(f"limiter detection_type out of range: {detection_type} (0=RMS, 1=peak)")
    return (
        dyneq_threshold_u16.encode(threshold_db)
        + dyneq_starttime_u16.encode(start_time_ms)
        + dyneq_release_time_u16.encode(release_time_ms)
        + u16.encode(0)
        + bool_u8.encode(enabled)
        + u8.encode(detection_type)
    )


def _decode_limiter_struct(data: bytes) -> tuple[float, float, float, bool, int]:
    return (
        dyneq_threshold_u16.decode(data[0:2]),
        dyneq_starttime_u16.decode(data[2:4]),
        dyneq_release_time_u16.decode(data[4:6]),
        bool_u8.decode(data[8:9]),
        u8.decode(data[9:10]),
    )


limiter_struct: Codec[tuple[float, float, float, bool, int]] = Codec(
    encode=_encode_limiter_struct, decode=_decode_limiter_struct
)


def _encode_expander_struct(fields: tuple[float, float, float, float, bool]) -> bytes:
    # Same wire layout as compressor_struct above, but using
    # expander_threshold_u16's distinct -100 dB-floor formula for threshold
    # and dyneq_ratio_u16 for ratio (Dynamic EQ's ratio formula, not
    # Compressor's compressor_ratio_u16). Expander has no detection type -
    # sent as 0.
    threshold_db, start_time_ms, release_time_ms, ratio, enabled = fields
    return (
        expander_threshold_u16.encode(threshold_db)
        + dyneq_starttime_u16.encode(start_time_ms)
        + dyneq_release_time_u16.encode(release_time_ms)
        + dyneq_ratio_u16.encode(ratio)
        + bool_u8.encode(enabled)
        + u8.encode(0)
    )


def _decode_expander_struct(data: bytes) -> tuple[float, float, float, float, bool]:
    return (
        expander_threshold_u16.decode(data[0:2]),
        dyneq_starttime_u16.decode(data[2:4]),
        dyneq_release_time_u16.decode(data[4:6]),
        dyneq_ratio_u16.decode(data[6:8]),
        bool_u8.decode(data[8:9]),
    )


expander_struct: Codec[tuple[float, float, float, float, bool]] = Codec(
    encode=_encode_expander_struct, decode=_decode_expander_struct
)


def _encode_mix_route(route: tuple[int, int, bool]) -> bytes:
    # Packs the crosspoint's 3 bytes in wire order: input index, output
    # index, value. The whole struct is the 0xA4 payload.
    #
    # Layout confirmed by readback: client.read_mix_matrix()'s decode of a
    # read_group() diff matched the unit's untouched factory-default routes
    # (InC->{Out5,Out6}, InD->{Out7,Out8}, never written by this client)
    # exactly. `value`'s polarity is confirmed on hardware by a dBFS
    # measurement (hardware_test/test_80_mix_route_polarity.py): toggling an
    # otherwise unused crosspoint moved a 1 kHz tone from the noise floor to
    # a clean connected level exactly when set to `True`, and back when set
    # to `False` - `value=1` is crosspoint CONNECTED, `value=0` is
    # disconnected, under the plain identity encoding below. See
    # client.read_mix_matrix's docstring for the layout evidence and
    # client.set_mix_route's docstring for the polarity evidence.
    in_index, out_index, enabled = route
    if not 0 <= in_index <= 0xFF:
        raise ValueError(f"mix in_index out of range: {in_index}")
    if not 0 <= out_index <= 0xFF:
        raise ValueError(f"mix out_index out of range: {out_index}")
    return bytes((in_index, out_index, 1 if enabled else 0))


def _decode_mix_route(data: bytes) -> tuple[int, int, bool]:
    return (data[0], data[1], data[2] != 0)


mix_route_u8: Codec[tuple[int, int, bool]] = Codec(encode=_encode_mix_route, decode=_decode_mix_route)


def _encode_fir(fields: tuple[bool, int, int, int, int, float, int]) -> bytes:
    # Packs the FIR block's 7 fields in wire order: enable, filter type,
    # window type, low freq, high freq, attenuation, order (all u8 except
    # the two frequencies, u16). Like the mix crosspoint, the whole struct
    # is always one 0xA4 payload, whichever field changed.
    #
    # filter type: raw u8, 0=low-pass, 1=high-pass (both confirmed on
    # hardware, hardware_test/test_a3_fir.py), 2=band-pass (untested),
    # 5=the channel's coefficient block (read_fir_coefs/write_fir_coefs,
    # see docs/PROTOCOL.md). The device stores 5 as 6, and 6 written back
    # behaves the same (hardware_test/test_b0_fir_coefficients.py), so
    # both are accepted and a readback can be written back unchanged.
    # These are the only values the manufacturer's app sends: 5 when the
    # coefficient type is selected, 6 when it re-sends a stored type. 3
    # and 4 are rejected. window type (window function) and order
    # (tap-count selector) are raw u8 too - see docs/PROTOCOL.md's
    # FIR section for the candidate window mapping and what's confirmed on
    # hardware vs. still open (hardware_test/test_a7_fir_win_type.py,
    # test_a8_fir_order.py). The two frequencies are raw Hz u16 with no
    # formula, range 10-20000 Hz (FIR_FREQ_HZ). Attenuation: raw = dB -
    # 21.0, range 21-120 dB (FIR_ATTENUATION_DB).
    enabled, fir_type, win_type, freq_low_hz, freq_high_hz, attenuation_db, order_index = fields
    if fir_type not in (0, 1, 2, 5, 6):
        raise ValueError(f"FIR filter type out of range: {fir_type} (0, 1, 2, 5 or 6)")
    # The manufacturer's app offers exactly 12 window types (0=Kaiser..
    # 11=sine, see docs/PROTOCOL.md) - same reasoning as eq_type's 0-5 check
    # in _encode_eq_struct, independent of hardware confirmation. The order
    # index below keeps only the u8 wire bound; Client.set_fir applies the
    # supported profile's narrower order bound (indices 0..1) before it
    # reaches the codec.
    if not 0 <= win_type <= 11:
        raise ValueError(f"FIR window type out of range: {win_type} (0-11)")
    if not 0 <= order_index <= 0xFF:
        raise ValueError(f"FIR order out of range: {order_index}")
    for name, value in (("freq_low_hz", freq_low_hz), ("freq_high_hz", freq_high_hz)):
        _require_range(value, FIR_FREQ_HZ, f"FIR {name}", " Hz")
    _require_range(attenuation_db, FIR_ATTENUATION_DB, "FIR attenuation", " dB")
    # int(), not round(): every other offset formula in this file truncates
    # to match the device (see _encode_volume) - round() here would silently
    # pick a different byte for any .5-or-above fraction (e.g. 30.7 dB ->
    # round() gives 0x0A, the device expects the truncated 0x09).
    beta_raw = int(round(attenuation_db - 21.0, 6))  # see _encode_volume for why round(x, 6)
    return (
        bytes((1 if enabled else 0, fir_type, win_type))
        + freq_low_hz.to_bytes(2, "little")
        + freq_high_hz.to_bytes(2, "little")
        + bytes((beta_raw, order_index))
    )


def _decode_fir(data: bytes) -> tuple[bool, int, int, int, int, float, int]:
    enabled = data[0] != 0
    fir_type = data[1]
    win_type = data[2]
    freq_low_hz = int.from_bytes(data[3:5], "little")
    freq_high_hz = int.from_bytes(data[5:7], "little")
    attenuation_db = data[7] + 21.0
    order_index = data[8]
    return (enabled, fir_type, win_type, freq_low_hz, freq_high_hz, attenuation_db, order_index)


fir_struct: Codec[tuple[bool, int, int, int, int, float, int]] = Codec(encode=_encode_fir, decode=_decode_fir)


def _encode_input_source(fields: tuple[int, int, float, bool]) -> bytes:
    # Packs the input source block's 4 fields in wire order: source, freq,
    # gain, enable (u8, u16, u16, u8). Like mix crosspoint/FIR above, the
    # whole struct is always one 0xA4 payload. Input-only.
    #
    # source: raw u8. Confirmed on hardware (hardware_test/
    # test_a2_input_source.py): 0=external analog input, 1=internal sine
    # generator, 2=internal pink noise, 3=internal white noise (see
    # client.set_input_source's docstring for the measurements). gain: raw =
    # (dB + 80.0) * 10, the *same* formula as volume_u16 above. freq: raw Hz
    # u16, no formula - only audible on source 1 (the sine generator), where
    # it sets the oscillator frequency. The manufacturer's app offers
    # 10-20000 Hz, but factory readback of an enabled external-source block
    # stores freq 0, so only the ceiling is enforced: requiring 10 would
    # block every edit of that factory block. gain uses the same -80..+12 dB
    # range as volume (VOLUME_DB).
    # enable: identity 0/1, confirmed not inverted.
    source_select, freq_hz, gain_db, enabled = fields
    if not 0 <= source_select <= 3:
        raise ValueError(f"input source select out of range: {source_select} (0-3)")
    if not 0 <= freq_hz <= INPUT_SOURCE_FREQ_MAX_HZ:
        raise ValueError(f"input source freq out of range: {freq_hz} Hz (0 to {INPUT_SOURCE_FREQ_MAX_HZ})")
    _require_range(gain_db, VOLUME_DB, "input source gain", " dB")
    gain_raw = int(round((gain_db + 80.0) * 10, 6))  # see _encode_volume for why round(x, 6)
    return (
        bytes((source_select,))
        + freq_hz.to_bytes(2, "little")
        + gain_raw.to_bytes(2, "little")
        + bytes((1 if enabled else 0,))
    )


def _decode_input_source(data: bytes) -> tuple[int, int, float, bool]:
    source_select = data[0]
    freq_hz = int.from_bytes(data[1:3], "little")
    gain_raw = int.from_bytes(data[3:5], "little")
    gain_db = gain_raw / 10.0 - 80.0
    enabled = data[5] != 0
    return (source_select, freq_hz, gain_db, enabled)


input_source_struct: Codec[tuple[int, int, float, bool]] = Codec(
    encode=_encode_input_source, decode=_decode_input_source
)


def _encode_freq_shifter(fields: tuple[int, int, bool]) -> bytes:
    # Packs the frequency shifter's 3 fields in wire order: level,
    # direction, enable (all u8). Like mix crosspoint/FIR/input source above,
    # the whole struct is always one 0xA4 payload. Input-only.
    #
    # level (one of 6 discrete presets) and direction are raw u8. Confirmed
    # on hardware (hardware_test/test_a4_freq_shifter.py): direction 0
    # shifts down and 1 shifts up; the six levels shift by approximately
    # 1/2.5/3.5/5/7/9 Hz.
    # enable: identity 0/1, confirmed not inverted.
    # This codec sends whatever `direction` the caller gives it (no forced
    # zero); 0 is "shift down", not a neutral default.
    freq_level, direction, enabled = fields
    if not 0 <= freq_level <= 5:
        raise ValueError(f"freq shifter level out of range: {freq_level} (0-5)")
    if not 0 <= direction <= 1:
        raise ValueError(f"freq shifter direction out of range: {direction} (0-1)")
    return bytes((freq_level, direction, 1 if enabled else 0))


def _decode_freq_shifter(data: bytes) -> tuple[int, int, bool]:
    return (data[0], data[1], data[2] != 0)


freq_shifter_struct: Codec[tuple[int, int, bool]] = Codec(encode=_encode_freq_shifter, decode=_decode_freq_shifter)


def _encode_channel_copy(fields: tuple[int, frozenset[int]]) -> bytes:
    # ASCII bitmap, one byte per channel: 'N' (0x4E) = not a copy target,
    # 'T' (0x54) = target. The bitmap length is the unit's channel count for
    # that direction (4 for inputs and 8 for outputs on 4-in/8-out models; 2
    # and 6 on 2-in/6-out models). Client.channel_copy restricts this to the
    # supported profile's 4/8 widths. The manufacturer's app picks a single
    # target channel, and only one 'T' at a time is confirmed on hardware
    # (hardware_test/test_a5_channel_copy.py); Client.channel_copy therefore
    # rejects multiple targets.
    total_channels, targets = fields
    if not 1 <= total_channels <= 0xFF:
        raise ValueError(f"channel copy total_channels out of range: {total_channels}")
    for t in targets:
        if not 0 <= t < total_channels:
            raise ValueError(f"channel copy target out of range: {t} (0-{total_channels - 1})")
    bitmap = bytearray(b"N" * total_channels)
    for t in targets:
        bitmap[t] = ord("T")
    return bytes(bitmap)


def _decode_channel_copy(data: bytes) -> tuple[int, frozenset[int]]:
    targets = frozenset(i for i, b in enumerate(data) if b == ord("T"))
    return (len(data), targets)


channel_copy_bitmap: Codec[tuple[int, frozenset[int]]] = Codec(encode=_encode_channel_copy, decode=_decode_channel_copy)


def _encode_empty(_: None) -> bytes:
    return b""


def _decode_empty(_: bytes) -> None:
    return None


empty: Codec[None] = Codec(encode=_encode_empty, decode=_decode_empty)
