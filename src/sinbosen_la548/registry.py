"""Registry of verified, real controls addressable via opcode 0xA4.

This is the structural fix for the 0xA4 incident (docs/PROTOCOL.md,
"Unsafe opcodes"): every write goes through a `ParamSpec` here, never through
hand-typed header fields at a call site. Every spec's header fields must be
confirmed by a hardware test or a readback diff before it is added here;
never hand-type header fields anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .codecs import (
    Codec,
    bool_u8,
    channel_copy_bitmap,
    compressor_struct,
    delay_u16,
    dyneq_struct,
    empty,
    eq_struct,
    expander_struct,
    fir_struct,
    freq_shifter_struct,
    input_source_struct,
    limiter_struct,
    mix_route_u8,
    mute_u8,
    phase_u8,
    volume_u16,
    xover_struct,
)
from .protocol import Opcode


class ChannelType:
    """The frame header's channel_type byte (see docs/PROTOCOL.md)."""

    IN = 0
    OUT = 1
    MIX = 2
    DEFAULT = 3


@dataclass(frozen=True)
class ParamSpec[T]:
    """One verified real control.

    `channel_id` (which physical channel) is supplied per-call by the
    caller; everything else here is fixed per control.
    """

    name: str
    opcode: Opcode
    channel_type: int
    module_id: int
    data_id: int
    codec: Codec[T]


REGISTRY: dict[str, ParamSpec[Any]] = {}


def _register[T](spec: ParamSpec[T]) -> ParamSpec[T]:
    REGISTRY[spec.name] = spec
    return spec


def _require_direction(channel_type: int) -> None:
    # Anything but IN/OUT would otherwise build an "_out" spec and send a
    # well-formed frame with a meaningless channel_type.
    if type(channel_type) is not int or channel_type not in (ChannelType.IN, ChannelType.OUT):
        raise ValueError(f"channel_type must be ChannelType.IN or ChannelType.OUT, got {channel_type!r}")


def _require_band(band_index: int, last: int, what: str) -> None:
    if type(band_index) is not int:
        raise TypeError(f"{what} band_index must be an int, got {type(band_index).__name__}: {band_index!r}")
    if not 0 <= band_index <= last:
        raise ValueError(f"{what} band_index out of range: {band_index} (0-{last})")


# Mute / volume / polarity: module_id 0 / 1 / 2 with data_id equal to
# module_id, on both input and output channels (hardware_test/
# test_00_rig_and_mute.py, test_10_volume.py, test_20_phase.py).
MUTE_IN = _register(
    ParamSpec(
        name="mute_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=0,
        data_id=0,
        codec=mute_u8,
    )
)
MUTE_OUT = _register(
    ParamSpec(
        name="mute_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=0,
        data_id=0,
        codec=mute_u8,
    )
)
VOLUME_IN = _register(
    ParamSpec(
        name="volume_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=1,
        data_id=1,
        codec=volume_u16,
    )
)
VOLUME_OUT = _register(
    ParamSpec(
        name="volume_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=1,
        data_id=1,
        codec=volume_u16,
    )
)
PHASE_IN = _register(
    ParamSpec(
        name="phase_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=2,
        data_id=2,
        codec=phase_u8,
    )
)
PHASE_OUT = _register(
    ParamSpec(
        name="phase_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=2,
        data_id=2,
        codec=phase_u8,
    )
)

# Delay: module_id=3, data_id=3 (data_id matches module_id, same convention
# as mute/volume/polarity above), both directions (hardware_test/
# test_30_delay.py).
DELAY_IN = _register(
    ParamSpec(
        name="delay_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=3,
        data_id=3,
        codec=delay_u16,
    )
)
DELAY_OUT = _register(
    ParamSpec(
        name="delay_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=3,
        data_id=3,
        codec=delay_u16,
    )
)

# EQ all-bypass (0xAF) / EQ all-reset (0xB0): both use a constant
# module_id=4. data_id is not a meaningful field for these two opcodes; it
# is pinned to 0 for determinism.
EQ_ALL_BYPASS_IN = _register(
    ParamSpec(
        name="eq_all_bypass_in",
        opcode=Opcode.EQ_ALL_BYPASS,
        channel_type=ChannelType.IN,
        module_id=4,
        data_id=0,
        codec=bool_u8,
    )
)
EQ_ALL_BYPASS_OUT = _register(
    ParamSpec(
        name="eq_all_bypass_out",
        opcode=Opcode.EQ_ALL_BYPASS,
        channel_type=ChannelType.OUT,
        module_id=4,
        data_id=0,
        codec=bool_u8,
    )
)
EQ_ALL_RESET_IN = _register(
    ParamSpec(
        name="eq_all_reset_in",
        opcode=Opcode.EQ_ALL_RESET,
        channel_type=ChannelType.IN,
        module_id=4,
        data_id=0,
        codec=empty,
    )
)
EQ_ALL_RESET_OUT = _register(
    ParamSpec(
        name="eq_all_reset_out",
        opcode=Opcode.EQ_ALL_RESET,
        channel_type=ChannelType.OUT,
        module_id=4,
        data_id=0,
        codec=empty,
    )
)


# Registered so the frames stay documented, but both are confirmed
# non-functional on this firmware (hardware_test/test_70_eq_bypass_polarity.py:
# 0xAF never replies; test_72_eq_all_reset.py: 0xB0 acks and changes
# nothing). Client._write_param refuses them, so generic REGISTRY users
# can't send them either.
BLOCKED: frozenset[str] = frozenset(
    spec.name for spec in (EQ_ALL_BYPASS_IN, EQ_ALL_BYPASS_OUT, EQ_ALL_RESET_IN, EQ_ALL_RESET_OUT)
)

# --- Xover and EQ band structs ------------------------------------------
#
# Each write sends the block's whole struct as one 0xA4 payload with
# data_id=0. Confirmed by a read_group() byte diff (the stored bytes match
# the codec layout exactly) and by audio measurement (hardware_test/
# test_40_xover.py, test_50_eq_band.py). One frame per field (data_id 38-41
# for Xover, 49-54 for EQ) is ACK'd but has no effect.
#
# Crossover is output-only on this hardware: the same struct write to an
# input channel changes no stored byte and has no audio effect. EQ works on
# both directions.
#
# Use the factory functions below rather than a fixed REGISTRY entry, since
# band index isn't statically enumerable (models with up to 31 EQ bands).

XoverBand = Literal["hp", "lp"]

_XOVER_MODULE_ID: dict[XoverBand, int] = {"hp": 38, "lp": 39}


def xover_struct_spec(band: XoverBand) -> ParamSpec[tuple[int, int, int, bool]]:
    """One crossover block (module_id=38 HP / 39 LP), output channels only.

    Value tuple: `(freq_hz, filter_type, slope_db_per_oct, enabled)`, in
    wire order - see `codecs.xover_struct`.
    """
    return ParamSpec(
        name=f"xover_{band}_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=_XOVER_MODULE_ID[band],
        data_id=0,
        codec=xover_struct,
    )


def eq_band_struct_spec(channel_type: int, band_index: int) -> ParamSpec[tuple[int, float, float, int, int, bool]]:
    """One EQ band (module_id = 4 + band_index, 0-based), both directions.

    Value tuple: `(freq_hz, gain_db, q, slope, type, enabled)`, in wire
    order - see `codecs.eq_struct`.

    `type`'s values (see docs/PROTOCOL.md): `0` = peaking EQ, `1` = low
    shelf, `2` = high shelf - all confirmed on hardware and behave as named.
    `3` = 1st-order all-pass, `4` = 2nd-order all-pass and `5` = notch were
    resolved by multi-frequency hardware sweeps
    (hardware_test/test_60_allpass.py). The notch is active for negative
    gain; see `docs/FILTER_MODELS.md` for its piecewise bandwidth rule. There
    is no band-pass/band-stop type on this hardware.

    `band_index` must stay within 0-30 (EQ bands 1-31 = module_id 4..34) -
    an out-of-range value would otherwise silently address a different,
    unrelated module (e.g. band 31 lands on Dynamic EQ band 0, band -1 on
    Delay) via the same well-formed-frame-aimed-elsewhere hazard as the
    original 0xA4 incident.
    """
    _require_direction(channel_type)
    _require_band(band_index, 30, "EQ")
    return ParamSpec(
        name=f"eq_band{band_index}_{'in' if channel_type == ChannelType.IN else 'out'}",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=channel_type,
        module_id=4 + band_index,
        data_id=0,
        codec=eq_struct,
    )


# --- Dynamic EQ band fields ----------------------------------------------
#
# Input-only: the output channel's settings block has no Dynamic EQ (see
# settings.py). Like Xover/EQ, each write sends the band's whole struct
# (freq, gain, Q, slope, type, mode, threshold, start time, release time,
# ratio, enable, in that order) as one 0xA4 frame, module_id = 35 +
# band_index, data_id=0. One frame per field (data_id 55-65) is ACK'd but
# has no measurable effect; the whole-struct write reaches storage and
# processes audio (hardware_test/test_90_dyneq_wire_format.py,
# test_91_dyneq_audible_effect.py).
def dyneq_struct_spec(
    band_index: int,
) -> ParamSpec[tuple[int, float, float, int, int, int, float, float, float, float, bool]]:
    """One Dynamic EQ band (module_id = 35 + band_index, 0-based). Input-only.

    Value tuple: `(freq_hz, gain_db, qvalue, slope, type, startup_mode,
    threshold_db, start_time_ms, release_time_ms, ratio, enabled)`, in wire
    order - see `codecs.dyneq_struct`.

    `band_index` must stay within 0-2 (module_id 35..37) - out of that range
    silently bleeds into unrelated modules (e.g. band 5 lands on module_id
    40, the Compressor; band -1 on static EQ band 30), the same class of
    hazard as the original 0xA4 incident.
    """
    _require_band(band_index, 2, "Dynamic EQ")
    return ParamSpec(
        name=f"dyneq_band{band_index}_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=35 + band_index,
        data_id=0,
        codec=dyneq_struct,
    )


# No all-bands Dynamic EQ enable or reset is implemented. The device has no
# known all-bands write for either: changing every band means one
# whole-struct write per band (module_id 35, 36, 37), each carrying every
# field of that band, which this client doesn't cache between calls.
# Toggling one band's `enable` is exactly what `set_dynamic_eq_band` already
# does (one whole-struct write per call). An all-bands reset would also need
# each band's default values, including a log-spaced default frequency that
# hasn't been determined.


# --- Dynamics: Compressor, Limiter, Expander ------------------------------
#
# Like Xover/EQ/Dynamic EQ, each write sends the block's whole struct as one
# 0xA4 frame with data_id=0, so every field is required on each call.
# Confirmed by a read_group() byte diff and by audio on output channels
# (hardware_test/test_a0_dynamics_wire_format.py,
# test_a1_dynamics_audible_effect.py).
#
# Topology: Limiter is output-only and Expander input-only (the per-channel
# settings blocks in settings.py carry each only on that side). There is no
# known all-enable/all-reset opcode for any of the three (unlike EQ's
# 0xAF/0xB0).
#
# Compressor is confirmed on output channels. An input-channel Compressor
# may exist only on some models in this family: the supported profile's
# settings layout has no input-compressor block, so Client.set_compressor
# refuses input channels without sending anything. The IN spec below is
# unverified on hardware.
#
# Shared wire layout: threshold, start time, release time, ratio, enable,
# detection type (u16 x4, u8 x2 - 10 bytes). Each block leaves one field
# unused - see codecs.py (Compressor: detection type; Limiter: ratio;
# Expander: detection type).

_COMPRESSOR_MODULE_ID = 40
_LIMITER_MODULE_ID = 41
_EXPANDER_MODULE_ID = 42


def compressor_struct_spec(channel_type: int) -> ParamSpec[tuple[float, float, float, float, bool]]:
    """The Compressor block (module_id=40). Builds IN and OUT specs; only
    the output compressor is confirmed on hardware (see the note above).

    Value tuple: `(threshold_db, start_time_ms, release_time_ms, ratio,
    enabled)` - see `codecs.compressor_struct`.
    """
    _require_direction(channel_type)
    return ParamSpec(
        name=f"compressor_{'in' if channel_type == ChannelType.IN else 'out'}",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=channel_type,
        module_id=_COMPRESSOR_MODULE_ID,
        data_id=0,
        codec=compressor_struct,
    )


def limiter_struct_spec() -> ParamSpec[tuple[float, float, float, bool, int]]:
    """The Limiter block (module_id=41). Output-only.

    Value tuple: `(threshold_db, start_time_ms, release_time_ms, enabled,
    detection_type)` - see `codecs.limiter_struct`. Stored snapshots preserve
    the raw u8, while `Client.set_limiter` restricts new writes to the two
    values the manufacturer's app offers, both measured on hardware.
    """
    return ParamSpec(
        name="limiter_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=_LIMITER_MODULE_ID,
        data_id=0,
        codec=limiter_struct,
    )


def expander_struct_spec() -> ParamSpec[tuple[float, float, float, float, bool]]:
    """The Expander block (module_id=42). Input-only.

    Value tuple: `(threshold_db, start_time_ms, release_time_ms, ratio,
    enabled)` - see `codecs.expander_struct`. Unlike Compressor's ratio
    (compressor_ratio_u16), Expander's ratio formula matches Dynamic EQ's
    dyneq_ratio_u16 exactly. Threshold uses a different offset than
    Compressor/Limiter (expander_threshold_u16, -100 dB floor vs. -60 dB).
    """
    return ParamSpec(
        name="expander_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=_EXPANDER_MODULE_ID,
        data_id=0,
        codec=expander_struct,
    )


# --- Mix matrix ------------------------------------------------------------
#
# A crosspoint toggle sends the whole 3-byte crosspoint struct (input index,
# output index, value) as a single 0xA4 payload. Structurally different from
# the per-channel controls above in two more ways:
#
# - Addressed by channel_type=MIX(2), not IN/OUT - the crosspoint address
#   (which input, which output) lives entirely inside the payload, not in
#   the header's channel_id.
# - module_id and data_id are always 0. channel_id carries no meaning here
#   and this client always sends 0 for determinism.
#
# Layout confirmed by readback and on/off polarity on hardware (see
# codecs.mix_route_u8, hardware_test/test_80_mix_route_polarity.py). There
# is no known continuous-level mix control (no known write uses module_id
# 51), so per-crosspoint on/off via this one path is the only known mixing
# capability.
#
# Input/output index ceilings are model-dependent (the crosspoint grid is
# flattened with a multiplier of 8 on 4-in/8-out models but 6 on 2-in/6-out
# models), so only the u8 0-255 bound is enforced in codecs.mix_route_u8 -
# verify on hardware which addresses are valid on a given unit.
MIX_ROUTE = _register(
    ParamSpec(
        name="mix_route",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.MIX,
        module_id=0,
        data_id=0,
        codec=mix_route_u8,
    )
)


# --- Secondary controls: Input Source, FIR, Frequency Shifter --------------
#
# All three share Mix matrix's shape: whichever field changes, each write
# sends every field of the control's struct as one 0xA4 payload, never a
# per-field frame. data_id is not a meaningful field on these frames; this
# client pins it to 0 for determinism, like the EQ all-bypass/all-reset
# data_id and Mix matrix's channel_id above.
#
# Input Source (module_id=43; source/freq/gain/enable, 6 bytes) is
# input-only (hardware_test/test_a2_input_source.py).
INPUT_SOURCE_IN = _register(
    ParamSpec(
        name="input_source_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=43,
        data_id=0,
        codec=input_source_struct,
    )
)

# FIR (module_id=49; enable/filter type/window type/low freq/high freq/
# attenuation/order, 9 bytes) exists on both IN and OUT with an identical
# module_id=49, differing only in channel_type - every channel's settings
# block carries one, unlike Input Source/Frequency Shifter, which are
# input-only. Confirmed on hardware on outputs (hardware_test/test_a3_fir.py);
# unverified on inputs.
FIR_IN = _register(
    ParamSpec(
        name="fir_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=49,
        data_id=0,
        codec=fir_struct,
    )
)
FIR_OUT = _register(
    ParamSpec(
        name="fir_out",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.OUT,
        module_id=49,
        data_id=0,
        codec=fir_struct,
    )
)

# Frequency Shifter (module_id=48; level/direction/enable, 3 bytes) is
# input-only (hardware_test/test_a4_freq_shifter.py). See
# codecs.freq_shifter_struct for what each field does.
FREQ_SHIFTER_IN = _register(
    ParamSpec(
        name="freq_shifter_in",
        opcode=Opcode.WRITE_MODULE_ID,
        channel_type=ChannelType.IN,
        module_id=48,
        data_id=0,
        codec=freq_shifter_struct,
    )
)


# --- Whole-device operations: program save/load, channel copy --------------
#
# module_id/data_id are not meaningful for these three and are pinned to 0
# for determinism, the same convention as the EQ all-bypass/all-reset
# data_id and Mix matrix's channel_id above. Program save/load also send
# channel_type/channel_id as 0; channel copy uses both (see below).
#
# Program save (Opcode.DEVICE_PROGRAM_SAVE, 0xAB): user_id = target slot
# index (0-based program list position), empty payload. Client.save_program
# waits up to 5 s for the ack. Confirmed on hardware
# (hardware_test/test_a6_program_slots.py).
DEVICE_PROGRAM_SAVE = _register(
    ParamSpec(
        name="device_program_save",
        opcode=Opcode.DEVICE_PROGRAM_SAVE,
        channel_type=ChannelType.IN,
        module_id=0,
        data_id=0,
        codec=empty,
    )
)

# Program load (Opcode.DEVICE_PROGRAM_READ, 0xAC): user_id = source slot
# index, empty payload. Client.load_program waits up to 9 s for the ack, and
# does not follow the load with a per-channel FIR coefficient read-back
# (this client doesn't know a unit's channel counts) - call `read_fir_coefs`
# per channel afterwards if you need them (an empty-payload
# Opcode.READ_FIR_COEFS (0xC8) request, channel_type=0/1, channel_id =
# channel, user_id=0, up to 1.8 s apiece).
DEVICE_PROGRAM_READ = _register(
    ParamSpec(
        name="device_program_read",
        opcode=Opcode.DEVICE_PROGRAM_READ,
        channel_type=ChannelType.IN,
        module_id=0,
        data_id=0,
        codec=empty,
    )
)

# Channel copy (Opcode.CHANNEL_COPY, 0xB5): channel_type=0 (in) / 1 (out),
# channel_id = the *source* channel (0-based, confirmed - NOT the target),
# payload = the ASCII bitmap from `codecs.channel_copy_bitmap`.
# Client.channel_copy waits up to 1 s for the ack. Confirmed on hardware in
# both directions (hardware_test/test_a5_channel_copy.py).
CHANNEL_COPY_IN = _register(
    ParamSpec(
        name="channel_copy_in",
        opcode=Opcode.CHANNEL_COPY,
        channel_type=ChannelType.IN,
        module_id=0,
        data_id=0,
        codec=channel_copy_bitmap,
    )
)
CHANNEL_COPY_OUT = _register(
    ParamSpec(
        name="channel_copy_out",
        opcode=Opcode.CHANNEL_COPY,
        channel_type=ChannelType.OUT,
        module_id=0,
        data_id=0,
        codec=channel_copy_bitmap,
    )
)
