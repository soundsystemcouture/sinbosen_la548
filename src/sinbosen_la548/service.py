"""Serialized device ownership and per-browser drafts, independent of Gradio."""

from __future__ import annotations

import math
import struct
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any

from . import codecs
from .client import Client
from .fir_adapter import FIRParameters
from .settings import (
    CompressorSettings,
    CrossoverSettings,
    DeviceSettings,
    DynamicEQSettings,
    EQBandSettings,
    ExpanderSettings,
    FIRSnapshot,
    FrequencyShifterSettings,
    InputSourceSettings,
    LimiterSettings,
)


@dataclass(frozen=True, order=True)
class ControlKey:
    output: bool
    channel: int
    block: str
    index: int = -1

    @property
    def label(self) -> str:
        direction = "Output" if self.output else "Input"
        suffix = f" {self.index + 1}" if self.index >= 0 else ""
        return f"{direction} {self.channel + 1}: {self.block}{suffix}"


@dataclass(frozen=True)
class Change:
    key: ControlKey
    before: object
    after: object


@dataclass(frozen=True)
class ApplyResult:
    applied: tuple[ControlKey, ...] = ()
    pending: tuple[ControlKey, ...] = ()
    conflicts: tuple[ControlKey, ...] = ()
    errors: tuple[str, ...] = ()
    verified: bool = True
    demo: bool = False

    @property
    def summary(self) -> str:
        if self.conflicts:
            return "External changes conflict with your draft: " + "; ".join(k.label for k in self.conflicts)
        prefix = "Demo: applied locally" if self.demo else "Applied and verified"
        if self.errors:
            return f"{len(self.applied)} verified, {len(self.pending)} pending. " + " ".join(self.errors)
        return f"{prefix}: {len(self.applied)} controls; {len(self.pending)} pending."


@dataclass
class DraftState:
    baseline: DeviceSettings
    draft: DeviceSettings
    demo: bool = True
    generation: int = -1
    live: bool = False
    due_at: float | None = None
    revision: int = 0
    message: str = "Offline demo — changes do not reach hardware."
    host: str | None = None


@dataclass(frozen=True)
class ServiceView:
    baseline: DeviceSettings
    draft: DeviceSettings
    pending: tuple[Change, ...]
    demo: bool
    connected: bool
    live: bool
    revision: int
    message: str


def _semantic(value: object) -> object:
    if isinstance(value, FIRSnapshot):
        return value.raw
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(getattr(value, f.name) for f in fields(value) if f.name != "raw")
    return value


def validated_fir_coefficients(data: bytes) -> bytes:
    """Validate one complete DA0408X FIR coefficient block before any write."""
    if not isinstance(data, bytes) or len(data) != 2048:
        raise ValueError("FIR coefficient backup must contain exactly 2048 bytes (512 float32 values)")
    if any(not math.isfinite(value) for value in struct.unpack("<512f", data)):
        raise ValueError("FIR coefficients must all be finite float32 values")
    return data


class FIRRestoreError(RuntimeError):
    """A FIR restore wrote but did not verify. `error` says what failed;
    `restored` holds the settings readback after a verified rollback, or
    None when the rollback failed too."""

    def __init__(self, error: str, *, restored: DeviceSettings | None, rollback_error: Exception | None = None):
        super().__init__(f"{error}; rollback failed: {rollback_error}" if restored is None
                         else f"{error}; original coefficients restored.")
        self.error = error
        self.restored = restored


def restore_fir_coefficients(client: Client, channel: int, coefficients: bytes, *, output: bool,
                             before_write: Callable[[], None] | None = None) -> DeviceSettings:
    """Write one FIR coefficient block with lock check, backup, verify and rollback.

    Shared by DSPService.restore_fir_backup and the CLI. Lock or preflight
    failures raise before anything is written; `before_write` runs just
    before the write, so a caller can tell a preflight failure from an
    unknown write outcome. A readback mismatch, or a change to the main
    settings snapshot, restores the backed-up block and raises
    FIRRestoreError. Returns the verified post-restore settings readback.
    """
    desired = validated_fir_coefficients(coefficients)
    if client.read_lock_state().locked:
        raise PermissionError("device is locked; writes are disabled")
    settings_before = client.read_settings()
    original = validated_fir_coefficients(client.read_fir_coefs(channel, output=output))
    if before_write is not None:
        before_write()
    client.write_fir_coefs(channel, desired, output=output)
    actual = client.read_fir_coefs(channel, output=output)
    settings_after = client.read_settings()
    if actual != desired:
        error = "FIR coefficient readback did not match the uploaded backup"
    elif settings_after.raw != settings_before.raw:
        error = "FIR coefficient restore changed the main settings snapshot"
    else:
        return settings_after
    try:
        client.write_fir_coefs(channel, original, output=output)
        restored = client.read_fir_coefs(channel, output=output)
        restored_settings = client.read_settings()
        if restored != original or restored_settings.raw != settings_before.raw:
            raise RuntimeError("rollback readback did not match the original state")
    except Exception as rollback_error:
        raise FIRRestoreError(error, restored=None, rollback_error=rollback_error) from rollback_error
    raise FIRRestoreError(error, restored=restored_settings)


def controls(settings: DeviceSettings) -> dict[ControlKey, object]:
    result: dict[ControlKey, object] = {}
    for ch in (*settings.inputs, *settings.outputs):
        for block, value in (("mute", ch.base.muted), ("polarity", ch.base.inverted),
                             ("gain", ch.base.gain_db), ("delay", ch.base.delay_ms)):
            result[ControlKey(ch.output, ch.channel_id, block)] = value
        for i, eq in enumerate(ch.eq_bands):
            result[ControlKey(ch.output, ch.channel_id, "eq", i)] = eq
        for i, dynamic in enumerate(ch.dynamic_eq_bands):
            result[ControlKey(ch.output, ch.channel_id, "dyneq", i)] = dynamic
        for block in ("hp", "lp", "compressor", "limiter", "expander", "input_source", "frequency_shifter", "fir"):
            value = getattr(ch, block)
            if value is not None:
                result[ControlKey(ch.output, ch.channel_id, block)] = value
    for i, row in enumerate(settings.mix_matrix):
        for o, value in enumerate(row):
            result[ControlKey(False, i, "route", o)] = value
    return result


def changes(before: DeviceSettings, after: DeviceSettings) -> tuple[Change, ...]:
    if before.profile != after.profile:
        raise ValueError("draft profile does not match the device")
    old, new = controls(before), controls(after)
    if old.keys() != new.keys():
        raise ValueError("draft channel/band structure does not match the device")
    return tuple(Change(key, old[key], value) for key, value in new.items() if _semantic(old[key]) != _semantic(value))


def replace_control(settings: DeviceSettings, key: ControlKey, value: object) -> DeviceSettings:
    if key not in controls(settings):
        raise ValueError(f"unsupported control: {key.label}")
    if key.block == "route":
        if not isinstance(value, bool):
            raise ValueError("route requires a boolean")
        rows = list(settings.mix_matrix)
        row = list(rows[key.channel])
        row[key.index] = value
        rows[key.channel] = tuple(row)
        return replace(settings, mix_matrix=tuple(rows))
    channels = list(settings.outputs if key.output else settings.inputs)
    ch = channels[key.channel]
    base_fields = {"mute": "muted", "polarity": "inverted", "gain": "gain_db", "delay": "delay_ms"}
    updates: dict[str, Any]
    if key.block in base_fields:
        updates = {base_fields[key.block]: value}
        ch = replace(ch, base=replace(ch.base, **updates))
    elif key.block in ("eq", "dyneq"):
        attr = "eq_bands" if key.block == "eq" else "dynamic_eq_bands"
        bands = list(getattr(ch, attr))
        bands[key.index] = value
        updates = {attr: tuple(bands)}
        ch = replace(ch, **updates)
    else:
        updates = {key.block: value}
        ch = replace(ch, **updates)
    channels[key.channel] = ch
    return replace(settings, outputs=tuple(channels)) if key.output else replace(settings, inputs=tuple(channels))


def _finite_values(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("settings must be finite")
    if is_dataclass(value) and not isinstance(value, type):
        for f in fields(value):
            if f.name != "raw":
                _finite_values(getattr(value, f.name))


def _check_unused(value: object) -> None:
    if isinstance(value, (CompressorSettings, ExpanderSettings)) and value.raw and value.raw[9] != 0:
        raise ValueError("cannot preserve unused dynamics bytes through the current write API")
    if isinstance(value, LimiterSettings) and value.raw and value.raw[6:8] != b"\x00\x00":
        raise ValueError("cannot preserve unused limiter bytes through the current write API")


def normalize_control(key: ControlKey, value: object) -> object:
    """Validate edited blocks only; never encode unrelated disabled defaults."""
    _finite_values(value)
    _check_unused(value)
    if hasattr(value, "enabled") and not isinstance(value.enabled, bool):
        raise ValueError("enable flag requires a boolean")
    if key.block in ("mute", "polarity", "route"):
        if not isinstance(value, bool):
            raise ValueError(f"{key.block} requires a boolean")
        return value
    if key.block in ("gain", "delay"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key.block} requires a number")
        codec = codecs.volume_u16 if key.block == "gain" else codecs.delay_u16
        return codec.decode(codec.encode(float(value)))
    if key.block == "eq" and isinstance(value, EQBandSettings):
        if value.slope not in (0, 1):
            raise ValueError("EQ slope must be 0 or 1")
        raw = codecs.eq_struct.encode((value.freq_hz, value.gain_db, value.q, value.slope, value.type, value.enabled))
        return EQBandSettings(*codecs.eq_struct.decode(raw), raw=raw)
    if key.block in ("hp", "lp") and isinstance(value, CrossoverSettings):
        if value.filter_type not in (0, 1, 2):
            raise ValueError("unsupported crossover family")
        raw = codecs.xover_struct.encode((value.freq_hz, value.filter_type, value.slope_db_per_oct, value.enabled))
        return CrossoverSettings(*codecs.xover_struct.decode(raw), raw=raw)
    if key.block == "dyneq" and isinstance(value, DynamicEQSettings):
        if value.type not in range(5) or value.slope not in (0, 1) or value.startup_mode not in (0, 1):
            raise ValueError("unsupported dynamic EQ type, slope, or startup mode")
        raw = codecs.dyneq_struct.encode((value.freq_hz, value.gain_db, value.q, value.slope, value.type,
                                         value.startup_mode, value.threshold_db, value.start_time_ms,
                                         value.release_time_ms, value.ratio, value.enabled))
        return DynamicEQSettings(*codecs.dyneq_struct.decode(raw), raw=raw)
    if key.block == "compressor" and isinstance(value, CompressorSettings):
        raw = codecs.compressor_struct.encode((value.threshold_db, value.start_time_ms, value.release_time_ms,
                                              value.ratio, value.enabled))
        return CompressorSettings(*codecs.compressor_struct.decode(raw), raw=raw)
    if key.block == "limiter" and isinstance(value, LimiterSettings):
        raw = codecs.limiter_struct.encode((value.threshold_db, value.start_time_ms, value.release_time_ms,
                                           value.enabled, value.detection_type))
        return LimiterSettings(*codecs.limiter_struct.decode(raw), raw=raw)
    if key.block == "expander" and isinstance(value, ExpanderSettings):
        raw = codecs.expander_struct.encode((value.threshold_db, value.start_time_ms, value.release_time_ms,
                                            value.ratio, value.enabled))
        return ExpanderSettings(*codecs.expander_struct.decode(raw), raw=raw)
    if key.block == "input_source" and isinstance(value, InputSourceSettings):
        if value.source_select not in range(4):
            raise ValueError("source index must be 0-3 (0=external, 1=sine, 2=pink noise, 3=white noise)")
        raw = codecs.input_source_struct.encode((value.source_select, value.freq_hz, value.gain_db, value.enabled))
        return InputSourceSettings(*codecs.input_source_struct.decode(raw), raw=raw)
    if key.block == "frequency_shifter" and isinstance(value, FrequencyShifterSettings):
        if value.level not in range(6) or value.direction not in (0, 1):
            raise ValueError("frequency shifter level must be 0-5 and direction 0/1")
        raw = codecs.freq_shifter_struct.encode((value.level, value.direction, value.enabled))
        return FrequencyShifterSettings(*codecs.freq_shifter_struct.decode(raw), raw=raw)
    if key.block == "fir" and isinstance(value, FIRSnapshot):
        # Parameter semantics remain behind the adapter; coefficient backups use a separate operation.
        return FIRParameters.read(value).snapshot(output=key.output)
    raise ValueError(f"invalid value for {key.label}")


class DSPService:
    def __init__(self, demo_settings: DeviceSettings, *, client_factory: Callable[[str], Client] = Client):
        self._fixture = demo_settings
        self._factory = client_factory
        self._client: Client | None = None
        self._demo_programs: dict[tuple[str, int], DeviceSettings] = {}
        self._sessions: dict[str, DraftState] = {}
        self._generation = 0
        self._lock = threading.RLock()

    def new_session(self) -> str:
        with self._lock:
            token = uuid.uuid4().hex
            self._sessions[token] = DraftState(self._fixture, self._fixture)
            return token

    def drop_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            for key in list(self._demo_programs):
                if key[0] == token:
                    self._demo_programs.pop(key)

    def _state(self, token: str) -> DraftState:
        try:
            return self._sessions[token]
        except KeyError:
            raise ValueError("session expired; reload the page") from None

    def _online(self, state: DraftState) -> bool:
        return not state.demo and state.generation == self._generation and self._client is not None

    def view(self, token: str) -> ServiceView:
        with self._lock:
            state = self._state(token)
            connected = self._online(state) and self._client is not None and self._client.link_up
            if not state.demo and not connected:
                state.live = False
                state.due_at = None
            return ServiceView(state.baseline, state.draft, changes(state.baseline, state.draft), state.demo,
                               connected, state.live, state.revision, state.message)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._generation += 1
            for state in self._sessions.values():
                state.live = False
                state.due_at = None

    def connect(self, token: str, ip: str) -> None:
        with self._lock:
            state = self._state(token)
            pending = changes(state.baseline, state.draft) if not state.demo else ()
            if pending and state.host is not None and state.host != ip:
                raise ValueError("revert pending edits before connecting to a different device")
            self.close()
            client = self._factory(ip)
            try:
                client.connect()
                locked = client.read_lock_state().locked
                observed = client.read_settings()
            except Exception:
                client.close()
                raise
            self._client = client
            if not pending or not self._conflicts(pending, observed):
                draft = observed
                for change in pending:
                    draft = replace_control(draft, change.key, change.after)
                state.baseline, state.draft = observed, draft
            state.demo = False
            state.host = ip
            state.generation = self._generation
            state.live = False
            state.due_at = None
            state.revision += 1
            state.message = "Connected; settings loaded."
            if pending:
                state.message += " Draft retained; Apply will check current settings."
            if locked:
                state.message += " Device is locked; writes are disabled."

    def demo(self, token: str) -> None:
        with self._lock:
            self.close()
            self._sessions[token] = DraftState(self._fixture, self._fixture)

    def stage(self, token: str, key: ControlKey, value: object, *, immediate: bool = False,
              expected_revision: int | None = None) -> None:
        self.stage_many(token, {key: value}, immediate=immediate, expected_revision=expected_revision)

    def stage_many(self, token: str, values: dict[ControlKey, object], *, immediate: bool = False,
                   expected_revision: int | None = None) -> None:
        with self._lock:
            state = self._state(token)
            if expected_revision is not None and (state.revision != expected_revision or state.live):
                raise ValueError("draft changed during graph editing; try again")
            draft = state.draft
            # Build the entire update before committing; errors leave the draft intact.
            for key, value in values.items():
                draft = replace_control(draft, key, normalize_control(key, value))
            if not changes(state.draft, draft):
                # Duplicate submit/blur events must not erase a failed Live result
                # or restart its write deadline when the normalized draft is unchanged.
                return
            state.draft = draft
            state.revision += 1
            pending = changes(state.baseline, draft)
            state.message = f"{len(pending)} pending controls."
            state.due_at = time.monotonic() + (0 if immediate else 0.25) if state.live and pending else None

    def revert(self, token: str) -> None:
        with self._lock:
            state = self._state(token)
            state.draft = state.baseline
            state.due_at = None
            state.revision += 1
            state.message = "Draft reverted to last readback."

    def report_error(self, token: str, error: Exception) -> None:
        """Retain an action failure until a later operation supplies new state."""
        with self._lock:
            state = self._state(token)
            state.message = f"Action failed — draft retained. {error}"

    def set_live(self, token: str, enabled: bool) -> None:
        with self._lock:
            state = self._state(token)
            if enabled and not state.demo and not self.view(token).connected:
                raise ConnectionError("connect before enabling Live")
            if enabled and changes(state.baseline, state.draft):
                raise ValueError("Apply or revert pending edits before enabling Live")
            state.live = enabled
            # Enabling Live never sends an existing draft; explicitly Apply it.
            state.due_at = None

    @staticmethod
    def _conflicts(pending: tuple[Change, ...], observed: DeviceSettings) -> tuple[ControlKey, ...]:
        current = controls(observed)
        return tuple(c.key for c in pending
                     if _semantic(current[c.key]) not in (_semantic(c.before), _semantic(c.after)))

    def refresh(self, token: str) -> None:
        with self._lock:
            state = self._state(token)
            if state.demo:
                return
            if not self._online(state) or self._client is None:
                raise ConnectionError("reconnect to refresh settings")
            observed = self._client.read_settings()
            pending = changes(state.baseline, state.draft)
            conflicts = self._conflicts(pending, observed)
            if conflicts:
                state.live = False
                state.due_at = None
                raise ValueError("External edit conflict: " + "; ".join(k.label for k in conflicts))
            draft = observed
            for change in pending:
                draft = replace_control(draft, change.key, change.after)
            state.baseline, state.draft = observed, draft
            state.revision += 1
            state.message = "Readback refreshed; pending edits retained."

    def flush_live(self, token: str, *, now: float | None = None) -> ApplyResult | None:
        with self._lock:
            state = self._state(token)
            if not self.view(token).live or state.due_at is None:
                return None
            if (time.monotonic() if now is None else now) < state.due_at:
                return None
            state.due_at = None
            return self.apply(token)

    def operation(self, token: str, kind: str, *, slot: int = 0, output: bool = True,
                  source: int = 0, targets: tuple[int, ...] = ()) -> None:
        """Explicit reviewed program/copy operations, always followed by fresh readback."""
        with self._lock:
            state = self._state(token)
            if changes(state.baseline, state.draft):
                raise ValueError("Apply or revert pending edits before program/copy operations")
            if kind not in ("save", "load", "copy"):
                raise ValueError("unsupported device operation")
            if type(slot) is not int or not 0 <= slot < 30:
                raise ValueError("program slot must be in the DA0408X range 0-29")
            count = state.draft.profile.output_count if output else state.draft.profile.input_count
            if kind == "copy":
                # Exactly one destination: Client.channel_copy rejects any other
                # count (matching the manufacturer's app), so demo mode must too.
                if (type(source) is not int or not 0 <= source < count or len(targets) != 1
                        or any(type(t) is not int or not 0 <= t < count or t == source for t in targets)):
                    raise ValueError("copy requires exactly one supported destination other than the source")
            state.live = False
            state.due_at = None
            if state.demo:
                observed = state.baseline
                if kind == "save":
                    self._demo_programs[token, slot] = observed
                elif kind == "load":
                    if (token, slot) not in self._demo_programs:
                        raise ValueError("save this demo slot before loading it")
                    observed = self._demo_programs[token, slot]
                else:
                    channels = list(observed.outputs if output else observed.inputs)
                    for destination in targets:
                        old = channels[destination]
                        channels[destination] = replace(channels[source], channel_id=destination,
                                                        name=old.name, name_raw=old.name_raw)
                    observed = (replace(observed, outputs=tuple(channels)) if output
                                else replace(observed, inputs=tuple(channels)))
                state.baseline = state.draft = observed
            else:
                if not self._online(state) or self._client is None:
                    raise ConnectionError("reconnect before device operations")
                client = self._client
                if client.read_lock_state().locked:
                    raise PermissionError("device is locked; writes are disabled")
                state.baseline = state.draft = client.read_settings()
                try:
                    if kind == "save":
                        client.save_program(slot)
                    elif kind == "load":
                        client.load_program(slot)
                    else:
                        client.channel_copy(source, targets, count, output=output)
                finally:
                    # Even a missing ACK may have completed the operation on the device.
                    try:
                        state.baseline = state.draft = client.read_settings()
                    except Exception:
                        state.message = "Operation readback unavailable; reconnect and refresh before further work."
                        self.close()
                        raise
            state.revision += 1
            state.message = f"{'Demo: ' if state.demo else ''}{kind.capitalize()} completed; settings refreshed."

    def read_backup(self, token: str, *, fir_channel: int | None = None, output: bool = True) -> bytes:
        with self._lock:
            state = self._state(token)
            if state.demo:
                if fir_channel is not None:
                    raise ValueError("the demo fixture has no separate FIR coefficient payload")
                return self._fixture.raw
            if not self._online(state) or self._client is None:
                raise ConnectionError("connect before downloading device data")
            if fir_channel is not None:
                count = state.draft.profile.output_count if output else state.draft.profile.input_count
                if type(fir_channel) is not int or not 0 <= fir_channel < count:
                    raise ValueError("unsupported FIR channel")
                return self._client.read_fir_coefs(fir_channel, output=output)
            return self._client.read_settings().raw

    def restore_fir_backup(self, token: str, coefficients: bytes, *, channel: int, output: bool) -> None:
        """Write, verify, and if possible roll back one selected FIR coefficient block."""
        desired = validated_fir_coefficients(coefficients)
        with self._lock:
            state = self._state(token)
            if changes(state.baseline, state.draft):
                raise ValueError("Apply or revert pending edits before restoring FIR coefficients")
            if state.demo:
                raise ConnectionError("connect before restoring FIR coefficients")
            if not self._online(state) or self._client is None:
                raise ConnectionError("reconnect before restoring FIR coefficients")
            count = state.draft.profile.output_count if output else state.draft.profile.input_count
            if type(channel) is not int or not 0 <= channel < count:
                raise ValueError("unsupported FIR channel")

            written = False

            def before_write() -> None:
                nonlocal written
                written = True
                state.live = False
                state.due_at = None

            try:
                settings_after = restore_fir_coefficients(self._client, channel, desired, output=output,
                                                          before_write=before_write)
            except PermissionError:
                raise
            except FIRRestoreError as exc:
                if exc.restored is None:
                    state.message = f"{exc.error}; rollback failed. Reconnect and verify before further writes."
                    self.close()
                    raise
                state.baseline = state.draft = exc.restored
                state.revision += 1
                state.message = str(exc)
                raise
            except Exception:
                state.message = ("FIR restore outcome is unknown; reconnect and verify before further writes."
                                 if written else "FIR restore preflight failed; reconnect before further writes.")
                self.close()
                raise
            state.baseline = state.draft = settings_after
            state.revision += 1
            state.message = "FIR coefficient backup restored and verified."

    def apply(self, token: str) -> ApplyResult:
        with self._lock:
            state = self._state(token)
            pending = changes(state.baseline, state.draft)
            if not pending:
                return ApplyResult(demo=state.demo)
            # Revalidate all changed blocks before performing any device write.
            normalized = tuple(Change(c.key, c.before, normalize_control(c.key, c.after)) for c in pending)
            if state.demo:
                draft = state.draft
                for c in normalized:
                    draft = replace_control(draft, c.key, c.after)
                state.baseline = state.draft = draft
                state.due_at = None
                state.revision += 1
                result = ApplyResult(applied=tuple(c.key for c in normalized), demo=True)
                state.message = result.summary
                return result
            if not self._online(state) or self._client is None:
                state.live = False
                raise ConnectionError("device disconnected; drafts are retained")
            client = self._client
            try:
                if client.read_lock_state().locked:
                    raise PermissionError("device is locked; writes are disabled")
                observed = client.read_settings()
            except Exception:
                state.live = False
                state.due_at = None
                raise
            conflicts = self._conflicts(normalized, observed)
            if conflicts:
                state.live = False
                state.due_at = None
                result = ApplyResult(conflicts=conflicts, pending=tuple(c.key for c in normalized))
                state.message = result.summary
                return result
            desired = observed
            for c in normalized:
                # Check fresh unused bytes too, preventing accidental overwrites.
                fresh = controls(observed)[c.key]
                _check_unused(fresh)
                desired = replace_control(desired, c.key, c.after)
            state.baseline, state.draft = observed, desired
            errors: list[str] = []
            for c in normalized:
                if _semantic(controls(observed)[c.key]) == _semantic(c.after):
                    continue
                try:
                    self._write(client, c.key, c.after)
                except Exception as exc:
                    errors.append(f"{c.key.label}: {exc}")
                    state.live = False
                    break
            verified = True
            try:
                readback = client.read_settings()
                state.baseline = readback
                # Retain requested values, merge unrelated observed changes.
                draft = readback
                for c in normalized:
                    draft = replace_control(draft, c.key, c.after)
                state.draft = draft
            except Exception as exc:
                verified = False
                state.live = False
                errors.append(f"Readback unavailable; writes are unverified: {exc}")
            remaining = changes(state.baseline, state.draft)
            remaining_keys = {c.key for c in remaining}
            applied = tuple(c.key for c in normalized if c.key not in remaining_keys) if verified else ()
            if remaining and not errors:
                errors.append("Some settings did not match readback; edits remain pending.")
                state.live = False
            state.due_at = None
            state.revision += 1
            result = ApplyResult(applied=applied, pending=tuple(c.key for c in remaining),
                                 errors=tuple(errors), verified=verified)
            state.message = result.summary
            return result

    @staticmethod
    def _write(client: Client, key: ControlKey, value: object) -> None:
        ch, out = key.channel, key.output
        if key.block == "mute" and isinstance(value, bool):
            client.set_mute(ch, value, output=out)
        elif key.block == "polarity" and isinstance(value, bool):
            client.set_phase(ch, value, output=out)
        elif key.block == "gain" and isinstance(value, (float, int)):
            client.set_volume(ch, float(value), output=out)
        elif key.block == "delay" and isinstance(value, (float, int)):
            client.set_delay(ch, float(value), output=out)
        elif key.block == "route" and isinstance(value, bool):
            client.set_mix_route(ch, key.index, value)
        elif key.block == "eq" and isinstance(value, EQBandSettings):
            client.set_eq_band(ch, key.index, freq_hz=value.freq_hz, gain_db=value.gain_db, q=value.q,
                               type=value.type, slope=value.slope, enabled=value.enabled, output=out)
        elif key.block in ("hp", "lp") and isinstance(value, CrossoverSettings):
            client.set_xover(ch, "hp" if key.block == "hp" else "lp", freq_hz=value.freq_hz,
                             filter_type=value.filter_type, slope_db_per_oct=value.slope_db_per_oct,
                             enabled=value.enabled)
        elif key.block == "dyneq" and isinstance(value, DynamicEQSettings):
            client.set_dynamic_eq_band(ch, key.index, freq_hz=value.freq_hz, gain_db=value.gain_db, qvalue=value.q,
                                       slope=value.slope, type=value.type, startup_mode=value.startup_mode,
                                       threshold_db=value.threshold_db, start_time_ms=value.start_time_ms,
                                       release_time_ms=value.release_time_ms, ratio=value.ratio, enabled=value.enabled)
        elif key.block == "compressor" and isinstance(value, CompressorSettings):
            client.set_compressor(ch, threshold_db=value.threshold_db, start_time_ms=value.start_time_ms,
                                 release_time_ms=value.release_time_ms, ratio=value.ratio,
                                 enabled=value.enabled, output=out)
        elif key.block == "limiter" and isinstance(value, LimiterSettings):
            client.set_limiter(ch, threshold_db=value.threshold_db, start_time_ms=value.start_time_ms,
                              release_time_ms=value.release_time_ms, enabled=value.enabled,
                              detection_type=value.detection_type)
        elif key.block == "expander" and isinstance(value, ExpanderSettings):
            client.set_expander(ch, threshold_db=value.threshold_db, start_time_ms=value.start_time_ms,
                               release_time_ms=value.release_time_ms, ratio=value.ratio, enabled=value.enabled)
        elif key.block == "input_source" and isinstance(value, InputSourceSettings):
            client.set_input_source(ch, source_select=value.source_select, freq_hz=value.freq_hz,
                                   gain_db=value.gain_db, enabled=value.enabled)
        elif key.block == "frequency_shifter" and isinstance(value, FrequencyShifterSettings):
            client.set_freq_shifter(ch, freq_level=value.level, direction=value.direction, enabled=value.enabled)
        elif key.block == "fir" and isinstance(value, FIRSnapshot):
            enabled, kind, window, low, high, attenuation, order = codecs.fir_struct.decode(value.raw)
            client.set_fir(ch, enabled=enabled, fir_type=kind, win_type=window, freq_low_hz=low,
                           freq_high_hz=high, attenuation_db=attenuation, order_index=order, output=out)
        else:
            raise ValueError(f"unsupported write: {key.label}")
