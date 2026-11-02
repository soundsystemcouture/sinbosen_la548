"""Immutable, read-only settings decoded from the confirmed snapshot shape.

The default profile describes the DA0408X *layout*, not an automatically
identified firmware/model. See docs/SETTINGS_READBACK.md for evidence and
limitations. No snapshot encoding or device writes are provided here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import codecs


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    input_count: int
    output_count: int
    input_eq_bands: int
    output_eq_bands: int
    dynamic_eq_bands: int
    sample_rate_hz: int
    sample_rate_verified: bool
    supports_input_compressor: bool


DA0408X = DeviceProfile(
    name="DA0408X",
    input_count=4,
    output_count=8,
    input_eq_bands=7,
    output_eq_bands=15,
    dynamic_eq_bands=3,
    sample_rate_hz=96000,
    sample_rate_verified=False,
    supports_input_compressor=False,
)

# Packed (no padding) field offsets and array counts, located on hardware by
# read_group() diffs and checked against tests/fixtures/read_group_sample.bin.
_INPUT_STRIDE = 153
_OUTPUT_STRIDE = 182
_OUTPUT_START = 612
_MIXER_START = 2068
_NAMES_START = 2164
_NAME_SIZE = 25
_SNAPSHOT_SIZE = 2464


@dataclass(frozen=True)
class BaseSettings:
    muted: bool
    inverted: bool
    source_raw: int
    fbx_raw: int
    gain_db: float
    delay_ms: float
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class EQBandSettings:
    freq_hz: int
    gain_db: float
    q: float
    slope: int
    type: int
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class CrossoverSettings:
    freq_hz: int
    filter_type: int
    slope_db_per_oct: int
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class DynamicEQSettings:
    freq_hz: int
    gain_db: float
    q: float
    slope: int
    type: int
    startup_mode: int
    threshold_db: float
    start_time_ms: float
    release_time_ms: float
    ratio: float
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class CompressorSettings:
    threshold_db: float
    start_time_ms: float
    release_time_ms: float
    ratio: float
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class LimiterSettings:
    threshold_db: float
    start_time_ms: float
    release_time_ms: float
    enabled: bool
    detection_type: int
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class ExpanderSettings:
    threshold_db: float
    start_time_ms: float
    release_time_ms: float
    ratio: float
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class InputSourceSettings:
    source_select: int
    freq_hz: int
    gain_db: float
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class FrequencyShifterSettings:
    level: int
    direction: int
    enabled: bool
    raw: bytes = field(repr=False)


@dataclass(frozen=True)
class FIRSnapshot:
    """Stable boundary for concurrent FIR work: enable flag plus opaque bytes.

    No dependency on the evolving FIR parameter/coefficient interpretation.
    This block is not the separate read_fir_coefs() payload.
    """

    enabled: bool
    raw: bytes = field(repr=False)

    @classmethod
    def from_bytes(cls, data: bytes) -> FIRSnapshot:
        raw = bytes(data)
        if len(raw) != 9:
            raise ValueError(f"FIR snapshot block must be 9 bytes, got {len(raw)}")
        _validate_flags(raw, (0,), "FIR")
        return cls(enabled=codecs.bool_u8.decode(raw[:1]), raw=raw)


@dataclass(frozen=True)
class ChannelSettings:
    channel_id: int
    output: bool
    name: str
    base: BaseSettings
    eq_bands: tuple[EQBandSettings, ...]
    fir: FIRSnapshot
    raw: bytes = field(repr=False)
    name_raw: bytes = field(repr=False)
    # None/empty means this profile has no such block, never a default value.
    input_source: InputSourceSettings | None = None
    expander: ExpanderSettings | None = None
    dynamic_eq_bands: tuple[DynamicEQSettings, ...] = ()
    frequency_shifter: FrequencyShifterSettings | None = None
    compressor: CompressorSettings | None = None
    limiter: LimiterSettings | None = None
    hp: CrossoverSettings | None = None
    lp: CrossoverSettings | None = None


@dataclass(frozen=True)
class DeviceSettings:
    profile: DeviceProfile
    inputs: tuple[ChannelSettings, ...]
    outputs: tuple[ChannelSettings, ...]
    # Indexed [input_id][output_id], matching Client.read_mix_matrix().
    mix_matrix: tuple[tuple[bool, ...], ...]
    raw: bytes = field(repr=False)


def _validate_flags(raw: bytes, offsets: tuple[int, ...], label: str) -> None:
    for offset in offsets:
        if raw[offset] not in (0, 1):
            raise ValueError(f"{label}: invalid boolean byte {raw[offset]} at block offset {offset}")


def _validate_eq_values(raw: bytes, label: str) -> None:
    freq = codecs.freq_u16.decode(raw[:2])
    if not 10 <= freq <= 20000:
        raise ValueError(f"{label}: frequency {freq} outside 10-20000 Hz")
    gain = codecs.u16.decode(raw[2:4])
    if gain > 400:
        raise ValueError(f"{label}: EQ gain raw value {gain} outside 0-400")
    q = codecs.u16.decode(raw[4:6])
    if q > 295:
        raise ValueError(f"{label}: EQ Q raw value {q} outside 0-295")


def _decode_base(raw: bytes, label: str) -> BaseSettings:
    _validate_flags(raw, (0, 1), label)
    delay = codecs.delay_u16.decode(raw[6:8])
    if not 0 <= delay <= 1200:
        raise ValueError(f"{label}: delay {delay} outside 0-1200 ms")
    return BaseSettings(
        muted=codecs.mute_u8.decode(raw[:1]),
        inverted=codecs.phase_u8.decode(raw[1:2]),
        source_raw=raw[2],
        fbx_raw=raw[3],
        gain_db=codecs.volume_u16.decode(raw[4:6]),
        delay_ms=delay,
        raw=raw,
    )


def _decode_eq(raw: bytes, label: str) -> EQBandSettings:
    _validate_flags(raw, (8,), label)
    _validate_eq_values(raw, label)
    return EQBandSettings(*codecs.eq_struct.decode(raw), raw=raw)


def _decode_xover(raw: bytes, label: str) -> CrossoverSettings:
    _validate_flags(raw, (4,), label)
    freq = codecs.freq_u16.decode(raw[:2])
    if not 10 <= freq <= 20000:
        raise ValueError(f"{label}: frequency {freq} outside 10-20000 Hz")
    if raw[3] > 7:
        raise ValueError(f"{label}: crossover slope raw value {raw[3]} outside 0-7")
    return CrossoverSettings(*codecs.xover_struct.decode(raw), raw=raw)


def _decode_dyneq(raw: bytes, label: str) -> DynamicEQSettings:
    _validate_flags(raw, (17,), label)
    _validate_eq_values(raw, label)
    return DynamicEQSettings(*codecs.dyneq_struct.decode(raw), raw=raw)


def _decode_channel(raw: bytes, name_raw: bytes, channel_id: int, *, output: bool) -> ChannelSettings:
    label = f"{'output' if output else 'input'} {channel_id}"
    # Encoding is not established; raw name slots remain available losslessly.
    name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    base = _decode_base(raw[:8], f"{label} base")
    if output:
        comp_raw, limit_raw = raw[8:18], raw[18:28]
        _validate_flags(comp_raw, (8,), f"{label} compressor")
        _validate_flags(limit_raw, (8,), f"{label} limiter")
        return ChannelSettings(
            channel_id=channel_id,
            output=True,
            name=name,
            base=base,
            eq_bands=tuple(
                _decode_eq(raw[38 + i * 9 : 47 + i * 9], f"{label} EQ {i}")
                for i in range(DA0408X.output_eq_bands)
            ),
            fir=FIRSnapshot.from_bytes(raw[173:182]),
            raw=raw,
            name_raw=name_raw,
            compressor=CompressorSettings(*codecs.compressor_struct.decode(comp_raw), raw=comp_raw),
            limiter=LimiterSettings(*codecs.limiter_struct.decode(limit_raw), raw=limit_raw),
            hp=_decode_xover(raw[28:33], f"{label} HP"),
            lp=_decode_xover(raw[33:38], f"{label} LP"),
        )
    source_raw, expander_raw, shifter_raw = raw[8:14], raw[14:24], raw[141:144]
    _validate_flags(source_raw, (5,), f"{label} source")
    _validate_flags(expander_raw, (8,), f"{label} expander")
    _validate_flags(shifter_raw, (2,), f"{label} frequency shifter")
    return ChannelSettings(
        channel_id=channel_id,
        output=False,
        name=name,
        base=base,
        eq_bands=tuple(
            _decode_eq(raw[78 + i * 9 : 87 + i * 9], f"{label} EQ {i}") for i in range(DA0408X.input_eq_bands)
        ),
        fir=FIRSnapshot.from_bytes(raw[144:153]),
        raw=raw,
        name_raw=name_raw,
        input_source=InputSourceSettings(*codecs.input_source_struct.decode(source_raw), raw=source_raw),
        expander=ExpanderSettings(*codecs.expander_struct.decode(expander_raw), raw=expander_raw),
        dynamic_eq_bands=tuple(
            _decode_dyneq(raw[24 + i * 18 : 42 + i * 18], f"{label} dynamic EQ {i}")
            for i in range(DA0408X.dynamic_eq_bands)
        ),
        frequency_shifter=FrequencyShifterSettings(*codecs.freq_shifter_struct.decode(shifter_raw), raw=shifter_raw),
    )


def decode_settings(blob: bytes, *, profile: DeviceProfile = DA0408X) -> DeviceSettings:
    """Decode one complete ReadGroup payload under an explicit layout profile.

    Only DA0408X is implemented. Exact size, parameter invariants, and
    every mixer coordinate are checked; size alone is not model detection.
    Unresolved enum values and unused fields are preserved, not guessed.
    sample_rate_hz comes from the provisional profile, not from the reply.
    """
    if profile != DA0408X:
        raise ValueError(f"unsupported settings profile: {profile.name}")
    raw = bytes(blob)
    if len(raw) != _SNAPSHOT_SIZE:
        raise ValueError(f"{profile.name} snapshot must be {_SNAPSHOT_SIZE} bytes, got {len(raw)}")

    matrix: list[tuple[bool, ...]] = []
    for input_id in range(profile.input_count):
        row: list[bool] = []
        for output_id in range(profile.output_count):
            offset = _MIXER_START + (input_id * profile.output_count + output_id) * 3
            # Snapshot coordinates are (out, in), confirmed live. The write
            # codec uses (in, out): deliberately do NOT decode with that codec.
            got_out, got_in, value = raw[offset : offset + 3]
            if (got_out, got_in) != (output_id, input_id):
                raise ValueError(
                    f"mixer entry at snapshot offset {offset} has (out={got_out}, in={got_in}), "
                    f"expected (out={output_id}, in={input_id})"
                )
            if value not in (0, 1):
                raise ValueError(f"mixer entry at snapshot offset {offset}: invalid boolean byte {value}")
            row.append(bool(value))
        matrix.append(tuple(row))

    names = tuple(
        raw[_NAMES_START + i * _NAME_SIZE : _NAMES_START + (i + 1) * _NAME_SIZE]
        for i in range(profile.input_count + profile.output_count)
    )
    inputs = tuple(
        _decode_channel(raw[i * _INPUT_STRIDE : (i + 1) * _INPUT_STRIDE], names[i], i, output=False)
        for i in range(profile.input_count)
    )
    outputs = tuple(
        _decode_channel(
            raw[_OUTPUT_START + i * _OUTPUT_STRIDE : _OUTPUT_START + (i + 1) * _OUTPUT_STRIDE],
            names[profile.input_count + i],
            i,
            output=True,
        )
        for i in range(profile.output_count)
    )
    return DeviceSettings(profile=profile, inputs=inputs, outputs=outputs, mix_matrix=tuple(matrix), raw=raw)
