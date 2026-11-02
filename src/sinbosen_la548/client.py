"""High-level client for one LA548 unit.

Wraps Transport with the specific request/reply shapes for the opcodes we
support in v1 (see docs/PROTOCOL.md for what's deferred and why).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable
from typing import Any

from .protocol import Frame, Opcode
from .registry import (
    BLOCKED,
    CHANNEL_COPY_IN,
    CHANNEL_COPY_OUT,
    DELAY_IN,
    DELAY_OUT,
    DEVICE_PROGRAM_READ,
    DEVICE_PROGRAM_SAVE,
    FIR_IN,
    FIR_OUT,
    FREQ_SHIFTER_IN,
    INPUT_SOURCE_IN,
    MIX_ROUTE,
    MUTE_IN,
    MUTE_OUT,
    PHASE_IN,
    PHASE_OUT,
    VOLUME_IN,
    VOLUME_OUT,
    ChannelType,
    ParamSpec,
    XoverBand,
    compressor_struct_spec,
    dyneq_struct_spec,
    eq_band_struct_spec,
    expander_struct_spec,
    limiter_struct_spec,
    xover_struct_spec,
)
from .settings import DA0408X, DeviceProfile, DeviceSettings, decode_settings
from .transport import DEFAULT_PORT, Transport

# read_mix_matrix()'s confirmed read_group() layout for the 4-in/8-out
# (DA0408X) model variant only - see read_mix_matrix's docstring for how
# this was confirmed (a read_group() diff against known factory-default
# routes). A different variant's blob has a different shape entirely.
_MIX_MATRIX_INPUTS = 4
_MIX_MATRIX_OUTPUTS = 8
_MIX_MATRIX_NAME_FIELD_LEN = 25
_MIX_MATRIX_NUM_NAME_FIELDS = _MIX_MATRIX_INPUTS + _MIX_MATRIX_OUTPUTS
_MIX_MATRIX_TABLE_LEN = _MIX_MATRIX_INPUTS * _MIX_MATRIX_OUTPUTS * 3
_DA0408X_FIR_COEFFICIENT_COUNT = 512
_DA0408X_FIR_COEFFICIENT_BYTES = _DA0408X_FIR_COEFFICIENT_COUNT * 4


def _require_index(value: int, label: str, count: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} must be an int, got {type(value).__name__}: {value!r}")
    if not 0 <= value < count:
        raise ValueError(f"{label} out of range: {value} (0-{count - 1})")


def _validate_da0408x_fir_block(payload: bytes, label: str) -> None:
    if len(payload) != _DA0408X_FIR_COEFFICIENT_BYTES:
        raise ValueError(
            f"{label} must be exactly {_DA0408X_FIR_COEFFICIENT_BYTES} bytes "
            f"({_DA0408X_FIR_COEFFICIENT_COUNT} float32 values), got {len(payload)}"
        )
    if not all(math.isfinite(value) for (value,) in struct.iter_unpack("<f", payload)):
        raise ValueError(f"{label} contains a non-finite float32 value")


def _parse_mix_matrix(blob: bytes) -> dict[int, dict[int, bool]]:
    names_len = _MIX_MATRIX_NUM_NAME_FIELDS * _MIX_MATRIX_NAME_FIELD_LEN
    table_end = len(blob) - names_len
    table_start = table_end - _MIX_MATRIX_TABLE_LEN
    if table_start < 0:
        raise ValueError(
            "read_group() blob doesn't match the confirmed 4-in/8-out mixer table "
            "layout (blob too short) - this unit may be a different model variant "
            "than read_mix_matrix() assumes; see docs/PROTOCOL.md, 'Mix matrix'."
        )
    table = blob[table_start:table_end]
    matrix: dict[int, dict[int, bool]] = {}
    for in_index in range(_MIX_MATRIX_INPUTS):
        row: dict[int, bool] = {}
        for out_index in range(_MIX_MATRIX_OUTPUTS):
            offset = (in_index * _MIX_MATRIX_OUTPUTS + out_index) * 3
            # (out, in, value) order at each entry - live-confirmed, not just
            # assumed: this exact parse, against a real unit, matched the
            # unit's documented factory-default routes exactly (see
            # read_mix_matrix's docstring).
            got_out, got_in, value = table[offset], table[offset + 1], table[offset + 2]
            if (got_out, got_in) != (out_index, in_index):
                raise ValueError(
                    f"mixer table entry at offset {offset} has (out={got_out}, in={got_in}), "
                    f"expected (out={out_index}, in={in_index}) - blob layout doesn't match "
                    "read_mix_matrix()'s assumptions"
                )
            row[out_index] = bool(value)
        matrix[in_index] = row
    return matrix


class DeviceId:
    def __init__(self, ip: str, name: str, raw: bytes):
        self.ip = ip
        self.name = name
        self.raw = raw

    def __repr__(self) -> str:
        return f"DeviceId(ip={self.ip!r}, name={self.name!r})"


class LockState:
    def __init__(self, locked: bool, pin: str):
        self.locked = locked
        self.pin = pin

    def __repr__(self) -> str:
        return f"LockState(locked={self.locked}, pin={'*' * len(self.pin)})"


class Client:
    """Talks to one LA548 unit over the network."""

    # (spec, channel_id, payload) of the last acknowledged _write_param call,
    # so a caller can verify readback against exactly what was sent.
    last_write: tuple[ParamSpec[Any], int, bytes] | None = None

    def __init__(self, ip: str, port: int = DEFAULT_PORT):
        self._transport = Transport(ip, port)

    def connect(self, *, timeout: float = 2.0) -> None:
        self._transport.start()
        # Connecting needs only the read-only ReadLockState request. DO NOT
        # send opcode 0xA4 ("WriteModuleId") here as a handshake: 0xA4 is
        # not a handshake opcode at all, it's the generic parameter-write
        # opcode shared by every control (volume, EQ, mix, mute, ...), and
        # an all-zero header (module_id=0, data_id=0) is exactly channel 1's
        # Mute control - such a "handshake" silently mutes channel 1 on every
        # connect. See docs/PROTOCOL.md, "Unsafe opcodes".
        try:
            self._transport.request(Opcode.READ_LOCK_STATE, timeout=timeout)
        except Exception:
            self._transport.stop()
            raise
        self._transport.start_keepalive()

    def close(self) -> None:
        self._transport.stop()

    def __enter__(self) -> Client:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def link_up(self) -> bool:
        return self._transport.link_up

    def read_device_id(self, *, timeout: float = 2.0) -> DeviceId:
        """Unavailable: opcode 0xD9 disrupts request sequencing on this firmware.

        One controlled hardware capture produced a delayed, model-specific
        reply only after disrupting normal request sequencing. See
        ``docs/PROTOCOL.md``. The operation is therefore disabled locally.
        """
        raise NotImplementedError(
            "read_device_id (opcode 0xD9) disrupts request sequencing on LA548 firmware; no packet was sent"
        )

    def get_program_names(self, *, timeout: float = 2.0) -> tuple[str, ...]:
        """Read all 30 program-slot names with GetUserName (opcode 0xCA).

        Hardware returns 30 fixed 25-byte UTF-8 slots (750 bytes); each name
        ends at its slot's first NUL byte.
        """
        frame = self._transport.request(Opcode.GET_USER_NAME, timeout=timeout)
        if len(frame.payload) != 30 * 25:
            raise ValueError(f"malformed program-name reply: expected 750 bytes, got {len(frame.payload)}")
        names = []
        for slot in range(30):
            raw = frame.payload[slot * 25 : (slot + 1) * 25].split(b"\x00", 1)[0]
            try:
                names.append(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError(f"malformed UTF-8 in program-name slot {slot}") from exc
        return tuple(names)

    def get_user_name(self, *, timeout: float = 2.0) -> str:
        """Compatibility alias returning program slot 0's name.

        Use :meth:`get_program_names` when the slot identity matters. The
        protocol has no singular device-user-name field.
        """
        return self.get_program_names(timeout=timeout)[0]

    def set_program_name(self, slot: int, name: str, *, timeout: float = 2.0) -> Frame:
        """Unavailable: the 0xCB rename write corrupts hidden slot bytes.

        The 0xCB request carries a 16-byte name, while this firmware's
        0xCA readback uses 25-byte slots. A controlled hardware trial restored
        the visible name but changed bytes 16-23 of that slot. No verified
        25-byte write format exists, so exact restoration is unavailable.
        """
        raise NotImplementedError(
            "program-name writes corrupt bytes 16-23 of the firmware's 25-byte slot; no packet was sent"
        )

    def read_group(self, *, timeout: float = 2.0) -> bytes:
        """Read the opaque settings blob for the currently active group.

        Opcode 0xA9, user_id=0, everything else default, empty payload;
        read-only. Returns the raw blob; use read_settings() for the
        supported DA0408X layout. Other layouts remain opaque. There is no
        matching write_group(): WriteGroup (0xA8) is a single whole-blob
        frame, not chunked, but it overwrites every setting at once, its
        handling of readback-only and name bytes is unverified, and whether
        it is safe without the accompanying per-channel FIR-coefficient
        restore (0xC9, see write_fir_coefs) is unverified (docs/PROTOCOL.md).
        """
        frame = self._transport.request(Opcode.READ_GROUP, user_id=0, timeout=timeout)
        return frame.payload

    def read_settings(
        self, *, profile: DeviceProfile = DA0408X, timeout: float = 2.0
    ) -> DeviceSettings:
        """Read typed settings using the selected snapshot layout, without writes.

        Only the fixture-matched DA0408X profile is supported. This does
        not discover firmware identity or sample rate. Invalid/incompatible
        snapshots raise ValueError; network failures propagate normally.
        See docs/SETTINGS_READBACK.md for verification evidence.
        """
        return decode_settings(self.read_group(timeout=timeout), profile=profile)

    def read_lock_state(self, *, timeout: float = 2.0) -> LockState:
        """Read the device lock state and PIN (opcode 0xB6, all-default
        header fields, empty payload). Read-only - this is the request
        connect() itself sends."""
        frame = self._transport.request(Opcode.READ_LOCK_STATE, timeout=timeout)
        if len(frame.payload) != 5:
            raise ValueError(f"malformed ReadLockState reply: expected 5 bytes, got {len(frame.payload)}")
        locked = bool(frame.payload[0])
        pin = frame.payload[1:5].decode("ascii", errors="replace")
        return LockState(locked=locked, pin=pin)

    def set_mute(self, channel_id: int, muted: bool, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Mute/unmute one channel (opcode 0xA4, module_id=0, data_id=0).

        This is the control behind the accidental-mute incident
        (docs/PROTOCOL.md, "Unsafe opcodes") - the inverted encoding (0x00=muted)
        is handled by `codecs.mute_u8`, not here, so there's no
        boolean-polarity guesswork left at this call site. Confirmed on
        hardware (`hardware_test/test_00_rig_and_mute.py`).
        """
        spec = MUTE_OUT if output else MUTE_IN
        return self._write_param(spec, channel_id, muted, timeout=timeout)

    def set_volume(self, channel_id: int, db: float, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Set one channel's gain in dB (opcode 0xA4, module_id=1, data_id=1).

        Bounded to -80..+12 dB, the range the manufacturer's app offers
        (docs/PROTOCOL.md, "Value bounds"); values outside it raise
        `ValueError` before anything is sent. The device's own ceiling is
        unverified. Confirmed on hardware (`hardware_test/test_10_volume.py`).
        """
        spec = VOLUME_OUT if output else VOLUME_IN
        return self._write_param(spec, channel_id, db, timeout=timeout)

    def set_phase(self, channel_id: int, inverted: bool, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Set one channel's polarity (opcode 0xA4, module_id=2, data_id=2).

        The raw byte is a plain identity mapping (`inverted=True` -> raw 1),
        **not** inverted like Mute. Two clean-boot audio sweeps
        (`hardware_test/test_20_phase.py`; docs/PROTOCOL.md,
        "Mute, volume and polarity") measured a consistent raw-value-to-signed-output
        mapping, but that evidence can't distinguish the two possible
        directions - swapping which raw value means "inverted" for *both*
        the input and output field at once cancels out in the measured sign,
        so both mappings fit the same data equally well. Polarity has two
        states, 0 and 180 degrees; mapping raw 1 to 180 degrees follows the
        plain boolean convention and is unverified by measurement.
        `codecs.phase_u8` is a plain alias for `codecs.bool_u8`.

        **Caveat**: this mapping has been observed to flip mid-session (no
        reboot, no config change) and revert after a power cycle -
        provisionally the same class of firmware fragility documented
        elsewhere for this unit (network dropouts, output 2 going
        stuck-silent for ~2 hours). Treat a wrong-sounding polarity
        mid-session as a signal to power-cycle the unit, not as evidence
        this encoding is wrong. See docs/PROTOCOL.md, "Mute, volume and polarity" for
        the full data.
        """
        spec = PHASE_OUT if output else PHASE_IN
        return self._write_param(spec, channel_id, inverted, timeout=timeout)

    def set_delay(self, channel_id: int, ms: float, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Set one channel's delay in milliseconds (opcode 0xA4, module_id=3,
        data_id=3, range 0-1200 ms).

        `codecs.delay_u16` handles the piecewise ms<->raw formula (0.01ms
        steps below 50ms, 1ms steps above), truncating rather than rounding
        to match the device exactly. Confirmed on hardware
        (`hardware_test/test_30_delay.py`).
        """
        spec = DELAY_OUT if output else DELAY_IN
        return self._write_param(spec, channel_id, ms, timeout=timeout)

    def set_xover(
        self,
        channel_id: int,
        band: XoverBand,
        *,
        freq_hz: int,
        filter_type: int,
        slope_db_per_oct: int,
        enabled: bool,
        timeout: float = 2.0,
    ) -> Frame:
        """Set one crossover block ("hp" or "lp") on an output channel.
        Output-only (see `registry.xover_struct_spec`) - crossover is not
        implemented for input channels on this hardware.

        The block is written as the *whole* crossover struct in one 0xA4
        frame, `data_id=0`. Independent per-field 0xA4 frames (data_id
        38-41) are ACK'd but have **no effect at all** on the signal
        (confirmed on hardware). All four fields are therefore always
        required, matching Mix matrix/Input Source/FIR/Frequency Shifter's
        "whole struct, no partial update" precedent - there is no meaningful
        "send only what changed" option once the wire shape is a single
        struct. `filter_type`: 0=Butterworth, 1=Linkwitz-Riley, 2=Bessel.

        Confirmed on hardware (`hardware_test/test_40_xover.py`): sweeping
        `freq_hz`/`slope_db_per_oct` measurably shapes a swept-sine
        magnitude response exactly as configured, both HP and LP.
        """
        value = (freq_hz, filter_type, slope_db_per_oct, enabled)
        return self._write_param(xover_struct_spec(band), channel_id, value, timeout=timeout)

    def set_eq_band(
        self,
        channel_id: int,
        band_index: int,
        *,
        freq_hz: int,
        gain_db: float | None = None,
        q: float,
        type: int,
        slope: int,
        enabled: bool,
        output: bool = False,
        timeout: float = 2.0,
    ) -> Frame:
        """Set one EQ band, 0-based (module_id = 4 + band_index).

        The block is written as the *whole* EQ-band struct in one 0xA4
        frame, `data_id=0`. As with `set_xover` above, independent per-field
        0xA4 frames (data_id 49-54) are ACK'd but have no measurable effect
        (confirmed on hardware). Unlike Crossover, EQ is confirmed to work
        on **both** input and output.

        **`type`**, matching the six-entry EQ type list the manufacturer's
        app offers (`EQ, LowShelf, HighShelf, AllPass1, AllPass2, Notch`)
        and confirmed on hardware by single-tone FFT magnitude/phase at each
        type's configured center:

        - `type=0`: EQ (peaking) - boost/cut at center, ~0 deg phase there.
        - `type=1`: **LowShelf** - +12 dB gain measured +11.9 dB at 100 Hz,
          +0.9 dB at 8000 Hz.
        - `type=2`: **HighShelf** - +12 dB gain measured +0.6 dB at 100 Hz,
          +11.9 dB at 8000 Hz.
        - `type=4`: **confirmed working AllPass** - flat magnitude
          (+0.02 dB, i.e. no gain effect at all) and almost exactly +180
          deg phase shift precisely at the configured center frequency,
          scaling with `q` as a real 2nd-order allpass should (a Q sweep
          0.5/1.0/2.0 showed a progressively steeper transition).

        A multi-frequency hardware sweep with a simultaneous phase
        reference confirms the remaining two:

        - `type=3`: a working **first-order allpass** - phase
          -53.13/-90.01/-126.93 deg at 500/1000/2000 Hz for a 1 kHz setting,
          flat magnitude, and (unlike `type=4`) no dependence on Q or gain
          (0-12 dB and Q 0.5/1/2 all gave the same response).
        - `type=5`: a working finite-depth **notch**, but only for
          **negative** gain - zero/positive gain is flat, so a test at
          zero or positive gain shows nothing. Cuts shallower than -6 dB
          follow `type=0`; at -6 dB and below, the bandwidth switches to
          the -3 dB definition. With L=10**(gain_db/20), the corresponding
          RBJ Q is Q_wire*sqrt(1/L - 2*L) for deep cuts, Q_wire*sqrt(L) for
          shallow cuts. The branch and formula were confirmed by hardware
          sweeps; see `docs/FILTER_MODELS.md`.

        `slope` selects first-order (0) or second-order (1) shelves for
        types 1/2; their stored Q has no measured effect. Shelf frequency
        specifies the prototype corner, not the half-gain midpoint.
        First-order shelves rotate phase even for tiny nonzero gains;
        exactly zero gain bypasses them. See `docs/FILTER_MODELS.md`.
        Slope 0/1 gave identical responses for types 0/3/5; type 4 was
        additionally tested at raw values 2/3 with no change. New writes are
        restricted to 0/1, the two slope values the manufacturer's app
        offers.

        AllPass (the primary motivation for this whole write path -
        subwoofer time/phase alignment): the struct always carries a gain
        field, so if `type` is an allpass (3 or 4) and `gain_db` isn't
        given, this method sends `gain_db=0.0` itself; for every other type,
        `gain_db` is required.

        Confirmed on hardware (`hardware_test/test_50_eq_band.py`,
        `hardware_test/test_60_allpass.py`): a +12/-12 dB boost/cut
        measured within 0.1 dB of the configured gain exactly at the
        configured frequency, and `type=4`'s allpass phase shift measured
        within a few degrees of the textbook +/-180 deg at center.
        """
        channel_type = ChannelType.OUT if output else ChannelType.IN
        _require_index(
            band_index,
            "EQ band_index",
            DA0408X.output_eq_bands if output else DA0408X.input_eq_bands,
        )
        if gain_db is None:
            if type not in (3, 4):
                raise ValueError("gain_db is required unless type is AllPass (3 or 4)")
            gain_db = 0.0
        if slope not in (0, 1):
            raise ValueError("EQ slope must be 0 or 1")
        spec = eq_band_struct_spec(channel_type, band_index)
        value = (freq_hz, gain_db, q, slope, type, enabled)
        return self._write_param(spec, channel_id, value, timeout=timeout)

    def eq_all_bypass(self, channel_id: int, enabled: bool, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Bypass (or restore) every EQ band on one channel at once (opcode
        0xAF, module_id=4, 1-byte payload).

        Confirmed on hardware to be **non-functional on this unit**, so this
        method raises `NotImplementedError` without sending anything. An
        exhaustive sweep (both `channel_type`s, `channel_id` 0-3, both
        `module_id` 0 and 4, both an empty and a 1-byte payload) got zero
        replies to opcode 0xAF in every case, while the structurally
        identical sibling opcode 0xB0 (`eq_all_reset`) ACKs normally on the
        same link - ruling out a session or wire-shape problem. Firing the
        frame anyway (ignoring the missing ACK) also produced zero
        measurable change to a live EQ boost, confirming the DSP doesn't
        silently apply it either. This is a genuine firmware gap on this
        unit, not a wire-format bug - see
        `hardware_test/test_70_eq_bypass_polarity.py` and docs/PROTOCOL.md.
        """
        raise NotImplementedError(
            "EQ-all bypass is confirmed non-functional on this LA548 firmware; "
            "no packet was sent. Disable or restore individual EQ bands instead."
        )

    def eq_all_reset(self, channel_id: int, *, output: bool = False, timeout: float = 2.0) -> Frame:
        """Reset every EQ band on one channel at once (opcode 0xB0,
        module_id=4, empty payload).

        Confirmed on hardware to be **non-functional on this unit**, the
        same finding as `eq_all_bypass`, just with a normal ACK instead of a
        timeout, so this method raises `NotImplementedError` without sending
        anything. The opcode/module_id/empty-payload request is the whole
        operation - there is no companion whole-struct write that could be
        missing here. A `read_group()` byte diff confirmed it has no effect:
        a +15 dB boosted band's struct in device storage was **completely
        unchanged** (0 of 2464 bytes differed anywhere in the whole blob)
        after sending it and getting a normal ACK back - see
        `hardware_test/test_72_eq_all_reset.py`. Use `set_eq_band(...,
        enabled=False)` per band instead (confirmed working -
        `hardware_test/test_71_eq_bypass_fallback.py`) if you need a real
        working reset/bypass.
        """
        raise NotImplementedError(
            "EQ-all reset is confirmed to ACK without changing state on this LA548 firmware; "
            "no packet was sent. Reset individual EQ bands instead."
        )

    def set_dynamic_eq_band(
        self,
        channel_id: int,
        band_index: int,
        *,
        freq_hz: int,
        gain_db: float,
        qvalue: float,
        slope: int,
        type: int,
        startup_mode: int,
        threshold_db: float,
        start_time_ms: float,
        release_time_ms: float,
        ratio: float,
        enabled: bool,
        timeout: float = 2.0,
    ) -> Frame:
        """Set one Dynamic EQ band, 0-based (module_id = 35 + band_index), as
        a single whole-struct write. Input-only - there is no output-channel
        Dynamic EQ on this hardware.

        Every field must be given on every call: the write is the whole
        Dynamic EQ band struct as one 0xA4 frame, `data_id=0`. As with
        `set_xover`/`set_eq_band`, independent per-field 0xA4 frames
        (data_id 55-65) are not the write path - confirmed on hardware
        (`hardware_test/test_90_dyneq_wire_format.py`): none of the 11
        per-field frames produced any measurable effect at all, while this
        whole-struct write does. See docs/PROTOCOL.md, "Dynamic EQ".

        Audible effect confirmed on hardware
        (`hardware_test/test_91_dyneq_audible_effect.py`): a
        fully-configured band (e.g. +15 dB at 1 kHz, threshold well below
        the test tone, ratio 10) measured a clean +15 dB level change,
        repeatably, on all three bands. If a write lands correctly in device
        storage (confirmed via a `read_group()` byte diff) but has zero
        audible effect, suspect a wedged DSP processing block - this
        hardware is sensitive to malformed/off-protocol packets - and
        power-cycle the unit; this symptom has been observed and cleared by
        a power cycle, and is not a code or protocol bug. See
        docs/PROTOCOL.md.

        Each field is bounded by the manufacturer's app's control limits
        (threshold -60..+20 dB, start 0.3..200 ms, release 50..5000 ms,
        ratio 1..20; docs/PROTOCOL.md, "Value bounds"); values outside them
        raise `ValueError` before anything is sent.
        """
        spec = dyneq_struct_spec(band_index)
        value = (
            freq_hz,
            gain_db,
            qvalue,
            slope,
            type,
            startup_mode,
            threshold_db,
            start_time_ms,
            release_time_ms,
            ratio,
            enabled,
        )
        return self._write_param(spec, channel_id, value, timeout=timeout)

    def set_compressor(
        self,
        channel_id: int,
        *,
        threshold_db: float,
        start_time_ms: float,
        release_time_ms: float,
        ratio: float,
        enabled: bool,
        output: bool = False,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the Compressor block (module_id=40).

        The block is written as the *whole* dynamics struct in one 0xA4
        frame (module_id=40, `data_id=0`) - see
        `registry.compressor_struct_spec`/`codecs.compressor_struct` - not
        as independent per-field frames (data_id 14-18). All fields are
        always required, matching `set_xover`/`set_eq_band`/
        `set_dynamic_eq_band`'s "whole struct, no partial update"
        precedent. `ratio` is a plain "N:1" value (e.g. `4.0` for 4:1) -
        a different formula than Limiter/Expander's ratio wire slot.
        Each field is bounded by the manufacturer's app's control limits
        (threshold -60..+20 dB, start 0.3..200 ms, release 50..5000 ms,
        ratio 1..128; docs/PROTOCOL.md, "Value bounds"); values outside them
        raise `ValueError` before anything is sent.

        Confirmed on hardware (`hardware_test/test_a0_dynamics_wire_format.py`,
        `hardware_test/test_a1_dynamics_audible_effect.py`): on **output**
        channels, the whole-struct write reaches device storage (a 5-byte
        `read_group()` delta for a fully-changed struct, matching exactly
        what each field's formula predicts) and produces a large, real
        compression effect (~28 dB level reduction with `threshold_db=-40`/
        `ratio=20` on a tone well above threshold).

        The DA0408X settings layout has no input-compressor block (an input
        compressor write leaves the readback unchanged on hardware), so
        input requests raise locally without sending; pass ``output=True``
        for the active output compressor. Limiter and Expander are unaffected.
        """
        if not output:
            raise NotImplementedError(
                "input compressor is unavailable on the DA0408X profile; no packet was sent. "
                "Use output=True for the output compressor."
            )
        channel_type = ChannelType.OUT if output else ChannelType.IN
        value = (threshold_db, start_time_ms, release_time_ms, ratio, enabled)
        return self._write_param(compressor_struct_spec(channel_type), channel_id, value, timeout=timeout)

    def set_limiter(
        self,
        channel_id: int,
        *,
        threshold_db: float,
        start_time_ms: float,
        release_time_ms: float,
        enabled: bool,
        detection_type: int,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the Limiter block (module_id=41). Output-only - there is no
        input-channel limiter on this hardware.

        Written the same way as `set_compressor` above: the whole dynamics
        struct (module_id=41, `data_id=0`), all fields required each call.
        The limiter has no ratio control - the struct's shared `ratio`
        wire slot is sent as 0 and ignored by this method's callers.
        `detection_type` accepts the two values the manufacturer's app
        offers: 0 is the average-sensitive mode and 1 is the peak-sensitive
        mode. The names are based on repeated equal-peak sine/burst
        measurements on hardware.

        Confirmed on hardware (`hardware_test/test_a0_dynamics_wire_format.py`,
        `hardware_test/test_a1_dynamics_audible_effect.py`): the whole-struct
        write reaches device storage (a 5-byte `read_group()` delta for a
        fully-changed struct) and produces a large, real limiting effect
        (~28 dB level reduction with `threshold_db=-40` on a tone well above
        threshold).
        """
        if detection_type not in (0, 1):
            raise ValueError("limiter detection type must be 0 or 1")
        value = (threshold_db, start_time_ms, release_time_ms, enabled, detection_type)
        return self._write_param(limiter_struct_spec(), channel_id, value, timeout=timeout)

    def set_expander(
        self,
        channel_id: int,
        *,
        threshold_db: float,
        start_time_ms: float,
        release_time_ms: float,
        ratio: float,
        enabled: bool,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the Expander block (module_id=42). Input-only - there is no
        output-channel expander on this hardware.

        Written the same way as `set_compressor` above: the whole dynamics
        struct (module_id=42, `data_id=0`), all fields required each call.
        `ratio` here uses the same formula as Dynamic EQ's ratio field (a
        "compression ratio", `1.0`-floored) - a different formula than
        Compressor's `ratio` above. `threshold_db` has a -100 dB floor (vs.
        Compressor/Limiter's -60 dB) and its own raw formula. The struct's
        shared `detection_type` wire slot is sent as 0 (Expander has no
        such control).

        Confirmed on hardware (`hardware_test/test_a0_dynamics_wire_format.py`,
        `hardware_test/test_a1_dynamics_audible_effect.py`): the whole-struct
        write reaches device storage (a 6-byte `read_group()` delta for a
        fully-changed struct - one field, `ratio`, needs 2 bytes here since
        its raw value crosses a byte boundary at this formula's scale) and
        produces a very large, real expansion effect (~79 dB level reduction
        with `threshold_db=-3`/`ratio=20` on a tone below threshold).

        The apparent ~1.8 s closing "hold" is the expander detector's slow
        release, not the configured gain release time (confirmed by envelope
        measurements). With a 50 ms release setting, closure onset was 1.10,
        1.94, and 2.79 s after signals 10, 15, and 20 dB above threshold.
        That linear 168.75 ms/dB slope implies an approximately 1.47 s
        exponential detector time constant. Once detection crossed threshold,
        the configured 50 ms case took another ~377 ms to reach 63% closure.
        See `docs/PROTOCOL.md` for the measured distinction.
        """
        value = (threshold_db, start_time_ms, release_time_ms, ratio, enabled)
        return self._write_param(expander_struct_spec(), channel_id, value, timeout=timeout)

    def set_mix_route(self, in_index: int, out_index: int, enabled: bool, *, timeout: float = 2.0) -> Frame:
        """Toggle one crosspoint of the mix matrix.

        Structurally different from every other control here - it addresses
        `channel_type=ChannelType.MIX` rather than IN/OUT, and the
        input/output crosspoint address lives entirely in the 3-byte
        payload (`in_index`, `out_index`, both 0-based), not in `channel_id`
        (which this method always sends as `0` - see `registry.MIX_ROUTE`
        for why). There is no continuous-level mix control on this
        hardware, only on/off per crosspoint.

        The field layout was confirmed by readback: `read_mix_matrix()`'s
        decode of a `read_group()` diff matched this unit's untouched
        factory-default routes (`InC`->{Out5,Out6}, `InD`->{Out7,Out8}, both
        never written by this client) exactly. `enabled`'s polarity is a
        separate claim, confirmed on hardware by a dedicated dBFS
        measurement (`hardware_test/test_80_mix_route_polarity.py`): toggling
        an otherwise-unused crosspoint jumped a 1 kHz tone from the noise
        floor to a clean connected level exactly when set to `True`, and
        back when set to `False` - `True`/`1` is crosspoint **connected**,
        not inverted. See `codecs.mix_route_u8` and `read_mix_matrix` for
        the layout evidence.
        """
        _require_index(in_index, "mix in_index", _MIX_MATRIX_INPUTS)
        _require_index(out_index, "mix out_index", _MIX_MATRIX_OUTPUTS)
        if not isinstance(enabled, bool):
            raise TypeError(f"mix route value must be a bool, got {type(enabled).__name__}: {enabled!r}")
        return self._write_param(MIX_ROUTE, 0, (in_index, out_index, enabled), timeout=timeout)

    def read_mix_matrix(self, *, timeout: float = 2.0) -> dict[int, dict[int, bool]]:
        """Read the full input->output routing table, decoded.

        The field layout was confirmed by diffing `read_group()`'s raw blob
        against this unit's untouched factory-default routes (see
        `set_mix_route`) and finding an exact match against the documented
        factory defaults. `codecs.mix_route_u8`'s polarity was confirmed
        separately by a dedicated dBFS measurement - see `set_mix_route`'s
        docstring. The blob holds the input-channel params, then the
        output-channel params, then the mixer table, then one fixed-width
        name slot per channel; the full layout is decoded and checked by
        `read_settings()`.

        This decode is specific to the confirmed 4-input/8-output model
        profile (DA0408X, matching `registry.MIX_ROUTE`'s 4-by-8 bus
        count). A different variant's blob has a different shape (fewer
        channels, fewer name slots), so this raises rather than silently
        misreading it - either the blob is too short for this shape at all,
        or the per-entry `(out_index, in_index)` check fails because the
        table landed somewhere else than expected.

        Returns `{in_index: {out_index: connected}}`, both 0-based, for all
        4 x 8 = 32 crosspoints that exist on this variant.
        """
        return _parse_mix_matrix(self.read_group(timeout=timeout))

    def write_mix_matrix(self, routes: dict[int, dict[int, bool]], *, timeout: float = 2.0) -> list[Frame]:
        """Apply a routing table via repeated `set_mix_route` calls.

        `routes` uses `read_mix_matrix`'s own shape
        (`{in_index: {out_index: connected}}`), so the common pattern is
        `read_mix_matrix()`, edit a few entries, `write_mix_matrix()` back.
        Only the crosspoints actually present in `routes` are written.

        Every `(in_index, out_index)` in `routes` is validated against this
        unit's confirmed 4x8 bus count *before* anything is sent - a bad
        entry partway through a naive loop would otherwise leave earlier
        crosspoints already changed on live audio. The current matrix is
        then read via `read_mix_matrix()`, and only entries that actually
        differ from current state are sent - repeated rapid `set_mix_route`
        writes are a suspected trigger for real hardware fragility incidents
        documented elsewhere in this project (docs/PROTOCOL.md, "Known
        issues"), so a read-edit-write round trip that only touches real
        diffs is worth doing even for a small edit to a large table.
        """
        for in_index, outs in routes.items():
            if not isinstance(in_index, int) or isinstance(in_index, bool):
                raise TypeError(f"mix in_index must be an int, got {type(in_index).__name__}: {in_index!r}")
            if not 0 <= in_index < _MIX_MATRIX_INPUTS:
                raise ValueError(f"mix in_index out of range for this unit: {in_index} (0-{_MIX_MATRIX_INPUTS - 1})")
            for out_index, enabled in outs.items():
                if not isinstance(out_index, int) or isinstance(out_index, bool):
                    raise TypeError(f"mix out_index must be an int, got {type(out_index).__name__}: {out_index!r}")
                if not 0 <= out_index < _MIX_MATRIX_OUTPUTS:
                    raise ValueError(
                        f"mix out_index out of range for this unit: {out_index} (0-{_MIX_MATRIX_OUTPUTS - 1})"
                    )
                if not isinstance(enabled, bool):
                    raise TypeError(f"mix route value must be a bool, got {type(enabled).__name__}: {enabled!r}")
        current = self.read_mix_matrix(timeout=timeout)
        return [
            self.set_mix_route(in_index, out_index, enabled, timeout=timeout)
            for in_index, outs in routes.items()
            for out_index, enabled in outs.items()
            if current[in_index][out_index] != enabled
        ]

    def set_input_source(
        self,
        channel_id: int,
        *,
        source_select: int,
        freq_hz: int,
        gain_db: float,
        enabled: bool,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the Input Source block (module_id=43). Input-only - there is
        no output-channel variant on this hardware.

        Like Mix matrix, this sends the *whole* input-source struct as one
        0xA4 payload - all four fields are always required, there is no
        "send only the fields given" partial-write option (see
        `registry.INPUT_SOURCE_IN`). `gain_db` uses the same `(dB+80)*10`
        formula as `set_volume`.

        **`source_select`, confirmed on hardware
        (`hardware_test/test_a2_input_source.py`):**

        - `0`: external analog input.
        - `1`: internal sine generator - `freq_hz` sets its frequency
          (measured peaks at 999.02/2000.98 Hz for freq_hz=1000/2000).
        - `2`: internal pink noise generator (PSD slope -3.09 dB/octave).
        - `3`: internal white noise generator (PSD slope -0.18 dB/octave).

        Selecting 1/2/3 replaces the external program material with an
        internally generated tone/noise; it is not an input-trim or gating
        control. `freq_hz` only has an audible effect on source 1 (the sine
        generator) - testing it against noise sources cannot show it doing
        anything.
        """
        value = (source_select, freq_hz, gain_db, enabled)
        return self._write_param(INPUT_SOURCE_IN, channel_id, value, timeout=timeout)

    def set_fir(
        self,
        channel_id: int,
        *,
        enabled: bool,
        fir_type: int,
        win_type: int,
        freq_low_hz: int,
        freq_high_hz: int,
        attenuation_db: float,
        order_index: int,
        output: bool = False,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the FIR block (module_id=49). Confirmed real on both input
        and output channels (unlike Input Source/Frequency Shifter below).

        Like Mix matrix/Input Source, this sends the *whole* FIR struct as
        one 0xA4 payload - all seven fields are always required (see
        `registry.FIR_IN`/`FIR_OUT`). `fir_type`: 0=low-pass, 1=high-pass,
        2=band-pass, 3=band-stop, 4=all-pass. `attenuation_db` has a
        confirmed `raw = display - 21.0` formula and the manufacturer's
        app's 21-120 dB range (docs/PROTOCOL.md, "Value bounds") - see
        `win_type` below for why that exact floor is itself a real clue,
        not just a range fact.

        **`win_type`/`order_index` both do something real on this unit,
        confirmed on hardware (`hardware_test/test_a7_fir_win_type.py`,
        `hardware_test/test_a8_fir_order.py`; see docs/PROTOCOL.md's FIR
        section for the full write-up):** `win_type` has a 12-item candidate
        mapping (`0=KAISER, 1=HANNING, 2=HAMMING, 3=BLACKMAN, 4=FLATTOP,
        5=BLACKMAN_HARRIS, ...`) following the display order of the
        manufacturer's app's window list, corroborated by Kaiser being the
        documented default window and by the 21.0 dB attenuation floor
        above matching the standard Kaiser design formula's own break
        point. Hardware testing confirmed `win_type` selects genuinely
        different filter shapes (a main-lobe-width/sidelobe-depth tradeoff
        right at the transition - measuring the *deep* stopband instead
        found no difference at all, a measurement-technique pitfall, not a
        real null result) and that `win_type=0` responds strongly to
        `attenuation_db` while a fixed-shape candidate barely moves,
        corroborating Kaiser specifically. `order_index` selects the tap
        count: 0=256 and 1=512 taps on both inputs and outputs, the two
        lengths the manufacturer's app offers for this model, so the public
        API accepts only indices 0 and 1. Hardware testing (on an output
        channel) confirmed `order_index=0` reproducibly measures a wider
        transition and shallower stopband than `1`; values 2 and 3 alias to
        the latter on this firmware, but the manufacturer's app never
        offers them and they are rejected locally.

        **Which frequency field is the actual corner, confirmed on hardware
        (**`hardware_test/test_a3_fir.py`**):** for
        `fir_type=0` (low-pass), `freq_low_hz` is the real corner and
        `freq_high_hz` has no measurable effect at all; for `fir_type=1`
        (high-pass), it's the other way around - `freq_high_hz` is the real
        corner and `freq_low_hz` has no effect. Neither field's name is a
        reliable guide by itself (plausibly this is one generic band-pass/
        band-stop primitive under the hood, with LP/HP as the two special
        cases of pushing one edge out past the band of interest, but that's
        inference - only the LP/HP cases above are actually measured, not
        band-pass/band-stop/all-pass). Both fields are raw Hz with no
        confirmed range beyond the u16 wire width.
        """
        spec = FIR_OUT if output else FIR_IN
        _require_index(order_index, "FIR order_index", 2)
        value = (enabled, fir_type, win_type, freq_low_hz, freq_high_hz, attenuation_db, order_index)
        return self._write_param(spec, channel_id, value, timeout=timeout)

    def set_freq_shifter(
        self,
        channel_id: int,
        *,
        freq_level: int,
        direction: int,
        enabled: bool,
        timeout: float = 2.0,
    ) -> Frame:
        """Set the Frequency Shifter block (module_id=48). Input-only -
        there is no output-channel variant on this hardware.

        Like Mix matrix/Input Source/FIR, this sends the *whole*
        frequency-shifter struct as one 0xA4 payload - all three fields are
        always required (see `registry.FREQ_SHIFTER_IN`).

        **Confirmed on hardware (**`hardware_test/test_a4_freq_shifter.py`**):**
        `direction` selects the shift's sign (`0`=down, `1`=up) and
        `freq_level` (0-5, matching the UI's 6 preset buttons 'lev1'..
        'lev6') scales the shift's magnitude - measured ~1 Hz at level 0 up
        to ~9 Hz at level 5 on a 1 kHz tone. Both fields are real and
        working, not guesses.

        `direction` is honored in both directions by the device itself:
        `direction=1` really does shift upward. Do not assume 0 is "safe"
        or "default" without a live `read_group()` check first if this
        matters for your use case.
        """
        value = (freq_level, direction, enabled)
        return self._write_param(FREQ_SHIFTER_IN, channel_id, value, timeout=timeout)

    def save_program(self, slot: int, *, timeout: float = 5.0) -> Frame:
        """Save the device's current live state to program slot `slot`
        (opcode 0xAB, empty payload, `user_id`=slot).

        `slot` is a 0-based index (0-29) into the device's 30 program
        slots; `timeout` defaults to 5 s. This does not touch live audio
        state - it's a snapshot, the lowest-risk of the whole-device
        operations.

        Confirmed on hardware (`hardware_test/test_a6_program_slots.py`)
        together with
        `load_program` below - see that method's docstring for the full
        round-trip result.
        """
        _require_index(slot, "program slot", 30)
        return self._write_param(DEVICE_PROGRAM_SAVE, 0, None, user_id=slot, timeout=timeout)

    def load_program(self, slot: int, *, timeout: float = 9.0) -> Frame:
        """Load program slot `slot`, **overwriting all of the device's
        current live settings** with whatever was last saved there (opcode
        0xAC, empty payload, `user_id`=slot).

        `slot` is a 0-based index, same convention as `save_program`;
        `timeout` defaults to 9 s.

        This is the highest-blast-radius operation implemented so far short
        of a raw `write_group` (deliberately not implemented - see
        `read_group`): it replaces every channel's Mute/Volume/EQ/Xover/etc.
        state at once, not one control on one channel. Confirm the target
        slot's contents (e.g. via a prior `save_program` to it, or by
        recalling what it holds) before calling this against a live setup.

        This method does **not** re-read FIR coefficient data (0xC8)
        afterward. Call `read_fir_coefs` per channel yourself if you need
        the loaded program's coefficients.

        Confirmed on hardware (`hardware_test/test_a6_program_slots.py`,
        against slot 0 on a unit confirmed to have no slots in use): saved
        a distinctive EQ boost
        (+15 dB @ 1 kHz, measured -23.07 dBFS), changed live state to a
        clearly different value (-12 dB, measured -49.99 dBFS), then loaded
        the saved slot back and measured -22.97 dBFS - within 0.1 dB of the
        originally-saved state. The full save/change/load round trip works
        correctly, not just "both calls ACK".
        """
        _require_index(slot, "program slot", 30)
        return self._write_param(DEVICE_PROGRAM_READ, 0, None, user_id=slot, timeout=timeout)

    def read_fir_coefs(self, channel_id: int, *, output: bool = False, timeout: float = 1.8) -> bytes:
        """Read one channel's raw FIR coefficient data (opcode 0xC8, empty
        payload, `channel_type`=in/out, `channel_id`=channel, `user_id`=0).

        DA0408X returns one 512-float32/2048-byte block per channel
        (confirmed on hardware); any other size or a non-finite value is
        rejected. The public boundary remains opaque bytes so backup and
        restore preserve every bit without depending on coefficient
        interpretation. `timeout` defaults to 1.8 s.
        """
        channel_type = ChannelType.OUT if output else ChannelType.IN
        _require_index(channel_id, "output channel_id" if output else "input channel_id", 8 if output else 4)
        frame = self._transport.request(
            Opcode.READ_FIR_COEFS,
            timeout=timeout,
            channel_type=channel_type,
            user_id=0,
            channel_id=channel_id,
        )
        _validate_da0408x_fir_block(frame.payload, "DA0408X FIR reply")
        return frame.payload

    def write_fir_coefs(
        self,
        channel_id: int,
        coefficients: bytes,
        *,
        output: bool = False,
        timeout: float = 1.8,
    ) -> Frame:
        """Restore one DA0408X channel's opaque FIR coefficient block.

        The DA0408X layout contains exactly 512 packed ``float32`` values
        (2048 bytes) per input and output channel. The restore sends one
        complete block with opcode ``0xC9``, ``user_id=0``, and the same
        channel addressing used by :meth:`read_fir_coefs`. Exact
        changed-byte readback and restoration are confirmed on hardware.

        The bytes are deliberately kept opaque so backups round-trip exactly;
        this method does not reinterpret, resize, or synthesize coefficients.
        It rejects every size other than the pinned DA0408X layout before any
        datagram is sent.
        """
        channel_type = ChannelType.OUT if output else ChannelType.IN
        _require_index(channel_id, "output channel_id" if output else "input channel_id", 8 if output else 4)
        payload = bytes(coefficients)
        _validate_da0408x_fir_block(payload, "DA0408X FIR coefficient block")
        return self._transport.request(
            Opcode.WRITE_FIR_COEFS,
            payload,
            timeout=timeout,
            channel_type=channel_type,
            user_id=0,
            channel_id=channel_id,
        )

    def channel_copy(
        self,
        source: int,
        targets: int | Iterable[int],
        total_channels: int,
        *,
        output: bool = False,
        timeout: float = 1.0,
    ) -> Frame:
        """Copy channel `source`'s full settings onto one `target`
        (opcode 0xB5, `channel_id`=source, payload=ASCII 'N'/'T' bitmap).

        `channel_id` in the header is the *source* channel, not the
        target(s) - the target(s) are marked in the payload instead
        (`codecs.channel_copy_bitmap`). `total_channels` is the bitmap
        width and must match the DA0408X channel count for `output`'s
        direction (4 inputs, 8 outputs); the protocol has no way to query
        it at runtime, so any other value raises before sending. The public
        method requires exactly one target, matching the manufacturer's app,
        which copies to one selected channel at a time. `timeout` defaults
        to 1 s.

        Confirmed on hardware (`hardware_test/test_a5_channel_copy.py`),
        both directions, on this unit's confirmed 4-input/8-output channel
        count (`total_channels=4`/`8` respectively): boosted a source
        channel's EQ band, left the target flat, confirmed the two channels
        measured clearly differently, called this method, then confirmed the
        target channel's measured output moved to within 0.1-0.6 dB of the
        source's - the copy genuinely propagates a real DSP parameter, not
        just an ACK.
        """
        expected_channels = _MIX_MATRIX_OUTPUTS if output else _MIX_MATRIX_INPUTS
        if total_channels != expected_channels:
            raise ValueError(
                f"channel copy total_channels must be {expected_channels} for "
                f"{'output' if output else 'input'} channels on DA0408X, got {total_channels}"
            )
        _require_index(source, "channel copy source", expected_channels)
        spec = CHANNEL_COPY_OUT if output else CHANNEL_COPY_IN
        try:
            target_set = frozenset((targets,) if isinstance(targets, int) else targets)
        except TypeError as exc:
            raise TypeError("channel copy targets must be an int or iterable of ints") from exc
        if not target_set:
            raise ValueError("channel_copy requires at least one target")
        if len(target_set) != 1:
            raise ValueError("channel_copy requires exactly one target, matching the manufacturer's app")
        for target in target_set:
            _require_index(target, "channel copy target", expected_channels)
        return self._write_param(spec, source, (total_channels, target_set), timeout=timeout)

    def _write_param[T](
        self,
        spec: ParamSpec[T],
        channel_id: int,
        value: T,
        *,
        user_id: int = 0,
        timeout: float = 2.0,
    ) -> Frame:
        """The only place a `ParamSpec` gets turned into an 0xA4 write.

        Always sends `linkage=0` (see docs/PROTOCOL.md - a nonzero linkage
        mirrors the write to other channels in the same link group, which we
        never want from this client). Every real control method on `Client`
        goes through this rather than building header fields itself - see
        docs/PROTOCOL.md, "Unsafe opcodes", for why that matters.

        Input and output channel IDs are checked against the enabled DA0408X
        profile (4 inputs / 8 outputs) before encoding. MIX/DEFAULT operations
        use their fixed header value (channel_id 0). Public operations
        with a distinct index, such as program slots, validate that index at
        their own call site.
        """
        if spec.name in BLOCKED or spec.opcode in (Opcode.EQ_ALL_BYPASS, Opcode.EQ_ALL_RESET):
            raise NotImplementedError(
                f"{spec.name} is disabled on this firmware (docs/PROTOCOL.md); no packet was sent"
            )
        if spec.channel_type == ChannelType.IN:
            _require_index(channel_id, "input channel_id", DA0408X.input_count)
        elif spec.channel_type == ChannelType.OUT:
            _require_index(channel_id, "output channel_id", DA0408X.output_count)
        else:
            _require_index(channel_id, "channel_id", 1)
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise TypeError(f"user_id must be an int, got {type(user_id).__name__}: {user_id!r}")
        if not 0 <= user_id <= 0xFF:
            raise ValueError(f"user_id out of range: {user_id} (0-255)")
        payload = spec.codec.encode(value)
        reply = self._transport.request(
            spec.opcode,
            payload,
            timeout=timeout,
            linkage=0,
            channel_type=spec.channel_type,
            user_id=user_id,
            channel_id=channel_id,
            module_id=spec.module_id,
            data_id=spec.data_id,
        )
        self.last_write = (spec, channel_id, payload)
        return reply
