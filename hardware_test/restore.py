"""Put the device back the way a hardware test found it.

`rig.reset_both_channels_safe` only covers mute/volume/phase/delay/routes, so
a test that fails part-way would otherwise leave EQ, crossover, dynamics, FIR
or input source changes on the unit for every later test. The `rig` fixture
snapshots `read_settings()` before each test and calls `restore_settings`
afterwards, pass or fail.

Changed controls go back through the service's own normalize/write path.
Disabled factory blocks can read back values outside the ranges the encoders
enforce (e.g. a 340 dB compressor threshold); those are restored by
re-sending the exact bytes read back, through `Client._write_param` with an
identity codec, so header construction still happens in the one sanctioned
place. FIR *coefficient* blocks and program slots are not part of
`read_settings()` and are not covered.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sinbosen_la548.client import Client
from sinbosen_la548.codecs import Codec
from sinbosen_la548.registry import (
    DELAY_IN,
    DELAY_OUT,
    FIR_IN,
    FIR_OUT,
    FREQ_SHIFTER_IN,
    INPUT_SOURCE_IN,
    MUTE_IN,
    MUTE_OUT,
    PHASE_IN,
    PHASE_OUT,
    VOLUME_IN,
    VOLUME_OUT,
    ChannelType,
    ParamSpec,
    compressor_struct_spec,
    dyneq_struct_spec,
    eq_band_struct_spec,
    expander_struct_spec,
    limiter_struct_spec,
    xover_struct_spec,
)
from sinbosen_la548.service import ControlKey, DSPService, changes, normalize_control
from sinbosen_la548.settings import DeviceSettings

_RAW: Codec[bytes] = Codec(encode=bytes, decode=bytes)


def spec_for(key: ControlKey) -> ParamSpec[Any]:
    """The registry spec that writes `key`'s block (routes excluded)."""
    direction = ChannelType.OUT if key.output else ChannelType.IN
    fixed: dict[str, tuple[ParamSpec[Any], ParamSpec[Any]]] = {
        "mute": (MUTE_IN, MUTE_OUT), "polarity": (PHASE_IN, PHASE_OUT), "gain": (VOLUME_IN, VOLUME_OUT),
        "delay": (DELAY_IN, DELAY_OUT), "fir": (FIR_IN, FIR_OUT),
    }
    if key.block in fixed:
        return fixed[key.block][key.output]
    if key.block == "eq":
        return eq_band_struct_spec(direction, key.index)
    if key.block == "dyneq":
        return dyneq_struct_spec(key.index)
    if key.block in ("hp", "lp"):
        return xover_struct_spec("hp" if key.block == "hp" else "lp")
    if key.block == "compressor":
        return compressor_struct_spec(direction)
    if key.block == "limiter":
        return limiter_struct_spec()
    if key.block == "expander":
        return expander_struct_spec()
    if key.block == "input_source":
        return INPUT_SOURCE_IN
    if key.block == "frequency_shifter":
        return FREQ_SHIFTER_IN
    raise ValueError(f"no registry spec for {key.label}")


def write_control(client: Client, key: ControlKey, value: object) -> None:
    try:
        normalized = normalize_control(key, value)
    except ValueError:
        raw = getattr(value, "raw", b"")
        if not raw:
            raise
        client._write_param(replace(spec_for(key), codec=_RAW), key.channel, bytes(raw))
        return
    DSPService._write(client, key, normalized)


def restore_settings(client: Client, baseline: DeviceSettings) -> tuple[ControlKey, ...]:
    """Re-write every control that differs from `baseline`, then verify by
    readback. Returns the keys written; raises if any still differ."""
    pending = changes(client.read_settings(), baseline)
    for change in pending:
        write_control(client, change.key, change.after)
    remaining = changes(client.read_settings(), baseline) if pending else ()
    if remaining:
        labels = ", ".join(change.key.label for change in remaining)
        raise RuntimeError(f"restore left {len(remaining)} controls different from the pre-test snapshot: {labels}")
    return tuple(change.key for change in pending)
