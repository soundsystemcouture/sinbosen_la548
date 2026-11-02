import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from sinbosen_la548.client import Client, LockState
from sinbosen_la548.protocol import Opcode
from sinbosen_la548.service import ControlKey, DSPService, controls, normalize_control, replace_control
from sinbosen_la548.settings import decode_settings


class Device:
    def __init__(self, settings):
        self.settings = settings
        self.client = Mock(spec=Client)
        self.client.link_up = True
        self.client.read_settings.side_effect = lambda: self.settings
        self.client.read_lock_state.return_value = LockState(False, "")
        self.writes = []
        for name, block in [("set_delay", "delay"), ("set_volume", "gain"), ("set_mute", "mute"),
                            ("set_phase", "polarity")]:
            getattr(self.client, name).side_effect = self.setter(block)
        self.client.set_mix_route.side_effect = self.route

    def setter(self, block):
        def set_value(channel, value, *, output=False, **kwargs):
            key = ControlKey(output, channel, block)
            self.writes.append((key, value))
            self.settings = replace_control(self.settings, key, value)
        return set_value

    def route(self, i, o, value, **kwargs):
        key = ControlKey(False, i, "route", o)
        self.writes.append((key, value))
        self.settings = replace_control(self.settings, key, value)


@pytest.fixture
def rig():
    snapshot = decode_settings((Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes())
    device = Device(snapshot)
    service = DSPService(snapshot, client_factory=lambda ip: device.client)
    token = service.new_session()
    return service, token, device


def test_connect_and_preview_do_not_write(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    assert device.writes == []
    assert device.client.read_settings.call_count == 1
    service.stage(token, ControlKey(True, 0, "delay"), 12.345)
    assert device.writes == []
    assert service.view(token).draft.outputs[0].base.delay_ms == 12.34
    assert service.view(token).baseline.outputs[0].base.delay_ms == 0


def test_redundant_normalized_stage_preserves_failure_and_revision(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 4)
    service.report_error(token, TimeoutError("injected live write failure"))
    before = service.view(token)
    service.stage(token, key, 4.001, immediate=True)
    assert service.view(token) == before
    assert device.writes == []


def test_program_operations_refresh_and_refuse_dirty_drafts(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 1)
    with pytest.raises(ValueError, match="pending"):
        service.operation(token, "load", slot=0)
    device.client.load_program.assert_not_called()
    service.revert(token)
    changed = replace_control(device.settings, key, 9)
    device.client.load_program.side_effect = lambda slot: setattr(device, "settings", changed)
    service.operation(token, "load", slot=0)
    device.client.load_program.assert_called_once_with(0)
    assert service.view(token).baseline.outputs[0].base.delay_ms == 9
    assert service.view(token).pending == ()
    assert device.client.read_settings.call_count == 3


def test_program_slot_range_is_the_confirmed_da0408x_range(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    for slot in (-1, 30, 255):
        with pytest.raises(ValueError, match="0-29"):
            service.operation(token, "save", slot=slot)
    device.client.save_program.assert_not_called()


def test_copy_destinations_and_post_failure_readback(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    for targets in ((0,), (), (1, 2), (1, 1)):
        with pytest.raises(ValueError, match="exactly one"):
            service.operation(token, "copy", source=0, targets=targets)
    device.client.channel_copy.assert_not_called()
    device.client.channel_copy.side_effect = TimeoutError("missing ACK")
    with pytest.raises(TimeoutError):
        service.operation(token, "copy", source=0, targets=(1,))
    device.client.channel_copy.assert_called_once_with(0, (1,), 8, output=True)
    assert device.client.read_settings.call_count == 3


def test_operation_readback_loss_disconnects_and_disables_live(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.set_live(token, True)
    device.client.read_settings.side_effect = [device.settings, TimeoutError("readback unavailable")]
    with pytest.raises(TimeoutError):
        service.operation(token, "save", slot=1)
    assert not service.view(token).connected
    assert not service.view(token).live


def test_demo_program_copy_and_backups(rig):
    service, token, device = rig
    service.operation(token, "save", slot=1)
    service.stage(token, ControlKey(True, 0, "delay"), 8)
    service.apply(token)
    service.operation(token, "copy", source=0, targets=(1,))
    assert service.view(token).draft.outputs[1].base.delay_ms == 8
    service.operation(token, "load", slot=1)
    assert service.view(token).draft.outputs[0].base.delay_ms == 0
    assert len(service.read_backup(token)) == 2464
    with pytest.raises(ValueError, match="no separate FIR"):
        service.read_backup(token, fir_channel=0)
    device.client.connect.assert_not_called()


def test_fir_backup_uses_selected_channel_only(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    device.client.read_fir_coefs.return_value = b"opaque"
    assert service.read_backup(token, fir_channel=7) == b"opaque"
    device.client.read_fir_coefs.assert_called_once_with(7, output=True)


def _fir_payload(last: float = 0.0) -> bytes:
    values = [1.0] + [0.0] * 510 + [last]
    return struct.pack("<512f", *values)


def test_fir_restore_validates_writes_reads_back_and_preserves_settings(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    original, desired = _fir_payload(), _fir_payload(0.0625)
    device.client.read_fir_coefs.side_effect = [original, desired]

    service.restore_fir_backup(token, desired, channel=1, output=False)

    device.client.write_fir_coefs.assert_called_once_with(1, desired, output=False)
    assert device.client.read_fir_coefs.call_args_list[0].args == (1,)
    assert device.client.read_fir_coefs.call_args_list[0].kwargs == {"output": False}
    assert service.view(token).message == "FIR coefficient backup restored and verified."
    assert service.view(token).pending == ()


@pytest.mark.parametrize("payload", [b"short", struct.pack("<512f", float("nan"), *([0.0] * 511))])
def test_fir_restore_rejects_invalid_payload_before_any_device_call(rig, payload):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    calls = list(device.client.mock_calls)
    with pytest.raises(ValueError):
        service.restore_fir_backup(token, payload, channel=0, output=True)
    assert device.client.mock_calls == calls


def test_fir_restore_mismatch_rolls_back_and_reports_failure(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    original, desired, wrong = _fir_payload(), _fir_payload(0.0625), _fir_payload(0.125)
    device.client.read_fir_coefs.side_effect = [original, wrong, original]

    with pytest.raises(RuntimeError, match="original coefficients restored"):
        service.restore_fir_backup(token, desired, channel=0, output=True)

    assert device.client.write_fir_coefs.call_args_list[0].args == (0, desired)
    assert device.client.write_fir_coefs.call_args_list[1].args == (0, original)
    assert service.view(token).connected


def test_fir_restore_transport_failure_disconnects_without_retry(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    device.client.read_fir_coefs.return_value = _fir_payload()
    device.client.write_fir_coefs.side_effect = TimeoutError("missing ACK")

    with pytest.raises(TimeoutError, match="missing ACK"):
        service.restore_fir_backup(token, _fir_payload(0.0625), channel=0, output=True)

    device.client.write_fir_coefs.assert_called_once()
    assert not service.view(token).connected


def test_fir_restore_preflight_failure_disconnects_without_writing(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    device.client.read_fir_coefs.side_effect = TimeoutError("preflight timeout")

    with pytest.raises(TimeoutError, match="preflight timeout"):
        service.restore_fir_backup(token, _fir_payload(0.0625), channel=0, output=True)

    device.client.write_fir_coefs.assert_not_called()
    assert not service.view(token).connected


def test_fir_restore_refuses_dirty_or_locked_state_without_writing(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.stage(token, ControlKey(True, 0, "delay"), 1)
    with pytest.raises(ValueError, match="pending edits"):
        service.restore_fir_backup(token, _fir_payload(), channel=0, output=True)
    service.revert(token)
    device.client.locked = True
    device.client.read_lock_state.return_value = LockState(True, "locked")
    with pytest.raises(PermissionError, match="locked"):
        service.restore_fir_backup(token, _fir_payload(), channel=0, output=True)
    device.client.write_fir_coefs.assert_not_called()


def test_apply_writes_only_differences_and_verifies(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 2.5)
    result = service.apply(token)
    assert result.applied == (key,)
    assert result.verified and not result.pending
    assert device.writes == [(key, 2.5)]
    service.apply(token)
    assert len(device.writes) == 1


def test_demo_apply_never_connects_or_writes(rig):
    service, token, device = rig
    service.stage(token, ControlKey(True, 0, "gain"), -6)
    result = service.apply(token)
    assert result.demo and result.verified
    assert not device.client.mock_calls
    assert service.view(token).baseline.outputs[0].base.gain_db == -6


def test_external_conflict_blocks_entire_apply(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage_many(token, {key: 2, ControlKey(True, 0, "gain"): -6})
    device.settings = replace_control(device.settings, key, 9)
    result = service.apply(token)
    assert result.conflicts == (key,)
    assert not device.writes
    assert len(service.view(token).pending) == 2


def test_unrelated_external_changes_merge_into_readback_and_draft(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.stage(token, ControlKey(True, 0, "delay"), 2)
    device.settings = replace_control(device.settings, ControlKey(True, 1, "gain"), -8)
    service.apply(token)
    assert service.view(token).draft.outputs[1].base.gain_db == -8
    assert service.view(token).baseline.outputs[1].base.gain_db == -8


def test_already_applied_external_value_is_not_written_again(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 2)
    device.settings = replace_control(device.settings, key, 2)
    assert service.apply(token).applied == (key,)
    assert not device.writes


def test_invalid_batch_is_atomic(rig):
    service, token, _ = rig
    initial = service.view(token).draft
    with pytest.raises(ValueError):
        service.stage_many(token, {ControlKey(True, 0, "gain"): -6, ControlKey(True, 0, "delay"): 1201})
    assert service.view(token).draft == initial
    with pytest.raises(ValueError):
        service.stage(token, ControlKey(True, 0, "gain"), float("nan"))


def test_partial_failure_keeps_unapplied_edits_and_disables_live(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.set_live(token, True)
    gain, delay = ControlKey(True, 0, "gain"), ControlKey(True, 0, "delay")
    service.stage_many(token, {gain: -6, delay: 3})
    device.client.set_delay.side_effect = TimeoutError("lost ACK")
    result = service.apply(token)
    assert result.applied == (gain,)
    assert result.pending == (delay,)
    assert result.errors and result.verified
    assert not service.view(token).live
    assert service.view(token).draft.outputs[0].base.delay_ms == 3


def test_ack_without_effect_is_not_reported_as_applied(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 3)
    device.client.set_delay.side_effect = None
    result = service.apply(token)
    assert not result.applied and result.pending == (key,)
    assert result.errors


def test_unavailable_final_readback_retains_unverified_draft(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 3)
    device.client.read_settings.side_effect = [device.settings, TimeoutError("no readback")]
    result = service.apply(token)
    assert not result.verified and not result.applied
    assert result.pending == (key,)


def test_live_coalesces_edits_and_waits_for_debounce(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.set_live(token, True)
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 2)
    service.stage(token, key, 3)
    service.stage(token, key, 4)
    assert service.flush_live(token, now=0) is None
    service.flush_live(token, now=float("inf"))
    assert device.writes == [(key, 4)]
    assert service.flush_live(token, now=float("inf")) is None


def test_enabling_live_does_not_send_existing_draft(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.stage(token, ControlKey(True, 0, "delay"), 4)
    with pytest.raises(ValueError, match="pending edits"):
        service.set_live(token, True)
    assert service.flush_live(token, now=float("inf")) is None
    assert not device.writes


def test_link_loss_and_reconnect_retain_drafts_without_replay(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.set_live(token, True)
    service.stage(token, ControlKey(True, 0, "delay"), 4)
    device.client.link_up = False
    assert not service.view(token).live
    assert service.flush_live(token, now=float("inf")) is None
    service.close()
    device.client.link_up = True
    service.connect(token, "127.0.0.1")
    assert service.view(token).draft.outputs[0].base.delay_ms == 4
    assert not service.view(token).live
    assert not device.writes


def test_two_browsers_have_independent_drafts_and_conflict_detection(rig):
    service, a, device = rig
    service.connect(a, "127.0.0.1")
    b = service.new_session()
    # Joining the active device uses connect; generation invalidates the earlier session.
    service.connect(b, "127.0.0.1")
    service.stage(a, ControlKey(True, 0, "delay"), 7)
    service.stage(b, ControlKey(True, 0, "delay"), 9)
    assert service.view(a).draft.outputs[0].base.delay_ms == 7
    assert service.view(b).draft.outputs[0].base.delay_ms == 9
    with pytest.raises(ConnectionError):
        service.apply(a)
    service.apply(b)
    assert device.settings.outputs[0].base.delay_ms == 9


def test_refresh_preserves_draft_and_rejects_conflicts(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "delay")
    service.stage(token, key, 3)
    service.refresh(token)
    assert service.view(token).draft.outputs[0].base.delay_ms == 3
    device.settings = replace_control(device.settings, key, 5)
    with pytest.raises(ValueError, match="conflict"):
        service.refresh(token)
    assert service.view(token).draft.outputs[0].base.delay_ms == 3


def test_device_lock_blocks_writes(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.stage(token, ControlKey(True, 0, "delay"), 3)
    device.client.read_lock_state.return_value = LockState(True, "")
    with pytest.raises(PermissionError):
        service.apply(token)
    assert not device.writes


def test_unused_advanced_fields_cannot_be_overwritten(rig):
    service, token, _ = rig
    comp = service.view(token).draft.outputs[0].compressor
    comp = replace(comp, raw=comp.raw[:9] + b"\x05")
    with pytest.raises(ValueError, match="unused"):
        normalize_control(ControlKey(True, 0, "compressor"), comp)


def test_reads_and_writes_are_serialized(rig):
    service, token, device = rig
    service.connect(token, "127.0.0.1")
    service.stage(token, ControlKey(True, 0, "delay"), 3)
    started = threading.Event()
    events = []

    def write(channel, value, **kwargs):
        events.append("write start")
        started.set()
        time.sleep(0.02)
        device.setter("delay")(channel, value, **kwargs)
        events.append("write end")

    def refresh():
        started.wait(1)
        service.refresh(token)
        events.append("refresh end")

    device.client.set_delay.side_effect = write
    a = threading.Thread(target=service.apply, args=(token,))
    b = threading.Thread(target=refresh)
    a.start()
    b.start()
    a.join(2)
    b.join(2)
    assert events == ["write start", "write end", "refresh end"]


def test_new_session_is_independent_and_can_expire(rig):
    service, token, _ = rig
    other = service.new_session()
    service.stage(token, ControlKey(True, 0, "delay"), 3)
    assert not service.view(other).pending
    service.drop_session(other)
    with pytest.raises(ValueError, match="expired"):
        service.view(other)
    assert len(controls(service.view(token).draft)) > 200


# --- Real Client over FakeTransport: exact frames for every writable block ---

def _real_client_rig(responses=None):
    from conftest import FakeTransport

    snapshot = decode_settings((Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes())
    transport = FakeTransport()
    transport.responses[Opcode.READ_GROUP] = snapshot.raw
    transport.responses[Opcode.READ_LOCK_STATE] = b"\x000000"
    transport.responses.update(responses or {})
    transport.link_up = True
    transport.start = transport.start_keepalive = transport.stop = lambda: None
    client = Client.__new__(Client)
    client._transport = transport
    return snapshot, client, transport


def _block_cases(snapshot):
    from sinbosen_la548 import codecs
    from sinbosen_la548.settings import FIRSnapshot

    out2, in1 = snapshot.outputs[2], snapshot.inputs[1]
    fir = FIRSnapshot.from_bytes(codecs.fir_struct.encode((True, 0, 1, 100, 1000, 40.0, 1)))
    return [
        (ControlKey(False, 1, "mute"), True, 0, 0),
        (ControlKey(True, 2, "polarity"), True, 2, 2),
        (ControlKey(True, 2, "gain"), -6.5, 1, 1),
        (ControlKey(False, 1, "delay"), 12.5, 3, 3),
        (ControlKey(False, 2, "route", 5), True, 0, 0),
        (ControlKey(True, 2, "eq", 3), replace(out2.eq_bands[3], freq_hz=800, gain_db=-4.0, q=2.0,
                                                enabled=True), 7, 0),
        (ControlKey(True, 2, "hp"), replace(out2.hp, freq_hz=120, filter_type=1, slope_db_per_oct=24,
                                             enabled=True), 38, 0),
        (ControlKey(True, 2, "lp"), replace(out2.lp, freq_hz=5000, enabled=True), 39, 0),
        (ControlKey(False, 1, "dyneq", 2), replace(in1.dynamic_eq_bands[2], freq_hz=500, gain_db=3.0,
                                                   threshold_db=-30.0, ratio=2.5, enabled=True), 37, 0),
        (ControlKey(True, 2, "compressor"), replace(out2.compressor, threshold_db=-20.0, ratio=4.0,
                                                     enabled=True), 40, 0),
        (ControlKey(True, 2, "limiter"), replace(out2.limiter, threshold_db=-3.0, detection_type=1,
                                                  enabled=True, raw=b""), 41, 0),
        (ControlKey(False, 1, "expander"), replace(in1.expander, threshold_db=-70.0, ratio=3.0,
                                                    enabled=True), 42, 0),
        (ControlKey(False, 1, "input_source"), replace(in1.input_source, source_select=1, freq_hz=1000,
                                                        gain_db=-10.0), 43, 0),
        (ControlKey(False, 1, "frequency_shifter"), replace(in1.frequency_shifter, level=3, enabled=True), 48, 0),
        (ControlKey(True, 2, "fir"), fir, 49, 0),
        (ControlKey(False, 1, "fir"), fir, 49, 0),
    ]


def test_every_block_write_sends_the_exact_frame_through_the_real_client():
    from sinbosen_la548 import codecs
    from sinbosen_la548.registry import ChannelType

    snapshot, client, transport = _real_client_rig()
    scalar = {"mute": codecs.mute_u8, "polarity": codecs.phase_u8, "gain": codecs.volume_u16,
              "delay": codecs.delay_u16}
    cases = _block_cases(snapshot)
    assert {key.block for key, *_ in cases} == {key.block for key in controls(snapshot)}
    for key, value, module_id, data_id in cases:
        normalized = normalize_control(key, value)
        transport.calls.clear()
        DSPService._write(client, key, normalized)
        ((opcode, payload, header),) = transport.calls
        assert opcode == Opcode.WRITE_MODULE_ID, key
        if key.block == "route":
            assert payload == bytes((key.channel, key.index, 1))
            expected = (ChannelType.MIX, 0, 0, 0)
        else:
            assert payload == (scalar[key.block].encode(normalized) if key.block in scalar else normalized.raw), key
            expected = (ChannelType.OUT if key.output else ChannelType.IN, key.channel, module_id, data_id)
        assert (header["channel_type"], header["channel_id"], header["module_id"], header["data_id"]) == expected, key
        assert header["linkage"] == 0


def test_fir_restore_addresses_the_requested_direction_through_the_real_client():
    from sinbosen_la548.registry import ChannelType

    desired = _fir_payload(0.0625)
    snapshot, client, transport = _real_client_rig({Opcode.READ_FIR_COEFS: desired})
    service = DSPService(snapshot, client_factory=lambda ip: client)
    token = service.new_session()
    service.connect(token, "127.0.0.1")

    service.restore_fir_backup(token, desired, channel=3, output=True)

    fir_calls = [c for c in transport.calls if c[0] in (Opcode.READ_FIR_COEFS, Opcode.WRITE_FIR_COEFS)]
    assert [c[0] for c in fir_calls] == [Opcode.READ_FIR_COEFS, Opcode.WRITE_FIR_COEFS, Opcode.READ_FIR_COEFS]
    assert all(c[2]["channel_type"] == ChannelType.OUT and c[2]["channel_id"] == 3 for c in fir_calls)
    assert fir_calls[1][1] == desired


def test_apply_with_an_invalid_limiter_detection_type_sends_no_packets():
    snapshot, client, transport = _real_client_rig()
    service = DSPService(snapshot, client_factory=lambda ip: client)
    token = service.new_session()
    service.connect(token, "127.0.0.1")
    key = ControlKey(True, 0, "limiter")
    bad = replace(snapshot.outputs[0].limiter, threshold_db=-3.0, detection_type=7, enabled=True, raw=b"")
    with pytest.raises(ValueError, match="detection_type"):
        service.stage(token, key, bad)
    # Even a draft that bypassed staging is revalidated before any write.
    service.stage(token, ControlKey(True, 1, "delay"), 2.0)
    state = service._sessions[token]
    state.draft = replace_control(state.draft, key, bad)
    transport.calls.clear()
    with pytest.raises(ValueError, match="detection_type"):
        service.apply(token)
    assert transport.calls == []
