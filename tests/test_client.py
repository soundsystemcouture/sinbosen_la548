import struct
from pathlib import Path

import pytest
from conftest import client_with_fake_transport

from sinbosen_la548 import codecs
from sinbosen_la548.client import Client
from sinbosen_la548.protocol import Frame, Opcode
from sinbosen_la548.registry import BLOCKED, REGISTRY, ChannelType
from sinbosen_la548.transport import Transport


def test_set_mute_defaults_to_input_and_encodes_inverted():
    client, fake = client_with_fake_transport()
    client.set_mute(2, True)

    opcode, payload, kwargs = fake.calls[0]
    assert payload == b"\x00"  # muted -> 0x00, confirmed inverted encoding
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["channel_id"] == 2


def test_read_device_id_fails_locally_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(NotImplementedError, match="no packet was sent"):
        client.read_device_id(timeout=0.01)
    assert fake.calls == []


def _program_name_payload(*names: str) -> bytes:
    values = list(names) + [f"User{i + 1}" for i in range(len(names), 30)]
    return b"".join(value.encode("utf-8").ljust(25, b"\x00") for value in values)


def test_get_program_names_decodes_all_30_utf8_slots():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.GET_USER_NAME] = _program_name_payload("User1", "Café")

    names = client.get_program_names()

    assert len(names) == 30
    assert names[:2] == ("User1", "Café")
    assert fake.calls == [(Opcode.GET_USER_NAME, b"", {})]


def test_get_user_name_is_a_compatible_slot_zero_alias():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.GET_USER_NAME] = _program_name_payload("First", "Second")
    assert client.get_user_name() == "First"


@pytest.mark.parametrize("payload", [b"short", b"\x00" * 751])
def test_get_program_names_rejects_wrong_payload_size(payload):
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.GET_USER_NAME] = payload
    with pytest.raises(ValueError, match="expected 750 bytes"):
        client.get_program_names()


def test_get_program_names_rejects_malformed_utf8():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.GET_USER_NAME] = b"\xff" + b"\x00" * 749
    with pytest.raises(ValueError, match="slot 0"):
        client.get_program_names()


def test_set_program_name_fails_locally_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(NotImplementedError, match="corrupt.*no packet was sent"):
        client.set_program_name(29, "Café")
    assert fake.calls == []


@pytest.mark.parametrize("method", ["save_program", "load_program"])
@pytest.mark.parametrize("slot", [-1, 30, 1.5, True])
def test_program_operations_reject_nonexistent_slots_without_sending(method, slot):
    client, fake = client_with_fake_transport()
    with pytest.raises((TypeError, ValueError), match="program slot"):
        getattr(client, method)(slot)
    assert fake.calls == []


@pytest.mark.parametrize(
    "channel_id,output",
    [(4, False), (-1, False), (8, True), (-1, True), (1.5, False), (True, False)],
)
def test_typed_setters_reject_channels_absent_from_da0408x_without_sending(channel_id, output):
    client, fake = client_with_fake_transport()
    with pytest.raises((TypeError, ValueError), match="channel_id"):
        client.set_mute(channel_id, True, output=output)
    assert fake.calls == []


@pytest.mark.parametrize("band_index,output", [(7, False), (-1, False), (15, True), (-1, True)])
def test_set_eq_rejects_bands_absent_from_da0408x_without_sending(band_index, output):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="EQ band_index"):
        client.set_eq_band(
            0,
            band_index,
            freq_hz=1000,
            gain_db=0,
            q=1,
            type=0,
            slope=0,
            enabled=True,
            output=output,
        )
    assert fake.calls == []


def test_set_mute_output_targets_output_channel_type():
    client, fake = client_with_fake_transport()
    client.set_mute(2, False, output=True)

    _, payload, kwargs = fake.calls[0]
    assert payload == b"\x01"
    assert kwargs["channel_type"] == ChannelType.OUT


def test_set_volume_encodes_db_formula():
    client, fake = client_with_fake_transport()
    client.set_volume(0, -6.0)

    _, payload, kwargs = fake.calls[0]
    assert payload == round((-6.0 + 80.0) * 10).to_bytes(2, "little")
    assert kwargs["channel_type"] == ChannelType.IN


def test_set_phase_encodes_identity():
    client, fake = client_with_fake_transport()
    client.set_phase(1, True)
    assert fake.calls[0][1] == b"\x01"  # inverted -> 0x01: plain identity, unlike mute

    client.set_phase(1, False)
    assert fake.calls[1][1] == b"\x00"


def test_all_writes_go_through_write_param_with_linkage_zero():
    client, fake = client_with_fake_transport()
    client.set_mute(0, True)
    client.set_volume(0, 0.0)
    client.set_phase(0, False)

    assert len(fake.calls) == 3
    assert all(kwargs["linkage"] == 0 for _, _, kwargs in fake.calls)


def test_set_delay_sends_raw_u16():
    client, fake = client_with_fake_transport()
    client.set_delay(0, 100.0)

    _, payload, kwargs = fake.calls[0]
    assert payload == (5050).to_bytes(2, "little")  # 100ms -> raw 4950+100
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["module_id"] == 3
    assert kwargs["data_id"] == 3


def test_set_delay_output_targets_output_channel_type():
    client, fake = client_with_fake_transport()
    client.set_delay(0, 100.0, output=True)
    assert fake.calls[0][2]["channel_type"] == ChannelType.OUT


def test_set_xover_sends_one_struct_frame_with_data_id_zero():
    client, fake = client_with_fake_transport()
    client.set_xover(2, "hp", freq_hz=80, filter_type=0, slope_db_per_oct=12, enabled=True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload == (80).to_bytes(2, "little") + bytes((0, 1, 1))  # freq, type, slope_raw, enable
    assert kwargs["module_id"] == 38  # hp block
    assert kwargs["data_id"] == 0
    assert kwargs["channel_id"] == 2
    assert kwargs["channel_type"] == ChannelType.OUT  # output-only


def test_set_xover_lp_uses_lp_module_id():
    client, fake = client_with_fake_transport()
    client.set_xover(0, "lp", freq_hz=120, filter_type=1, slope_db_per_oct=24, enabled=True)

    assert fake.calls[0][2]["module_id"] == 39


def test_set_eq_band_sends_one_struct_frame_with_data_id_zero():
    client, fake = client_with_fake_transport()
    client.set_eq_band(0, 3, freq_hz=1000, gain_db=0.0, q=1.0, type=0, slope=0, enabled=True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert len(payload) == 9
    assert kwargs["module_id"] == 7  # 4 + band_index 3
    assert kwargs["data_id"] == 0


def test_set_eq_band_allpass_auto_zeroes_gain_when_not_given():
    client, fake = client_with_fake_transport()
    client.set_eq_band(0, 0, type=4, freq_hz=100, q=1.0, slope=0, enabled=True)  # confirmed AllPass raw value

    _, payload, _ = fake.calls[0]
    assert payload[2:4] == (200).to_bytes(2, "little")  # gain 0dB -> raw 200


def test_set_eq_band_allpass_respects_explicit_gain():
    client, fake = client_with_fake_transport()
    client.set_eq_band(0, 0, type=4, gain_db=-3.0, freq_hz=100, q=1.0, slope=0, enabled=True)

    _, payload, _ = fake.calls[0]
    assert payload[2:4] == (170).to_bytes(2, "little")  # -3dB -> raw (17.0)*10


def test_set_eq_band_non_allpass_requires_gain():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="gain_db is required"):
        client.set_eq_band(0, 0, type=0, freq_hz=1000, q=1.0, slope=0, enabled=True)  # peaking EQ, no gain given
    assert fake.calls == []


@pytest.mark.parametrize("eq_type", [0, 4])
def test_set_eq_band_rejects_unsupported_slope(eq_type):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="slope must be 0 or 1"):
        client.set_eq_band(0, 0, type=eq_type, gain_db=0, freq_hz=1000, q=1.0, slope=2, enabled=True)
    assert fake.calls == []


def test_eq_all_bypass_fails_locally_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(NotImplementedError, match="no packet was sent"):
        client.eq_all_bypass(2, True)
    assert fake.calls == []


def test_connect_stops_transport_on_bootstrap_request_failure():
    # A real Client/Transport here (not client_with_fake_transport, which
    # bypasses Transport via Client.__new__) since this exercises Transport's
    # own start()/stop() lifecycle. Constructing one only opens an
    # unconnected UDP socket - no real network traffic.
    client = Client("127.0.0.1", 8087)

    def fail_request(*args: object, **kwargs: object) -> None:
        raise TimeoutError("no reply")

    client._transport.request = fail_request  # type: ignore[method-assign]

    with pytest.raises(TimeoutError):
        client.connect(timeout=0.05)

    assert client._transport._stop.is_set()
    assert client._transport._recv_thread is not None
    assert not client._transport._recv_thread.is_alive()


def test_eq_all_reset_fails_locally_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(NotImplementedError, match="no packet was sent"):
        client.eq_all_reset(2)
    assert fake.calls == []


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_generic_write_param_refuses_blocked_registry_specs(name):
    client, fake = client_with_fake_transport()
    spec = REGISTRY[name]
    value = None if spec.opcode == Opcode.EQ_ALL_RESET else True
    with pytest.raises(NotImplementedError, match="no packet was sent"):
        client._write_param(spec, 0, value)
    assert fake.calls == []


def test_set_dynamic_eq_band_sends_one_struct_frame_with_data_id_zero():
    client, fake = client_with_fake_transport()
    client.set_dynamic_eq_band(
        0,
        2,
        freq_hz=1000,
        gain_db=0.0,
        qvalue=1.0,
        slope=0,
        type=0,
        startup_mode=0,
        threshold_db=-20.0,
        start_time_ms=1.0,
        release_time_ms=100.0,
        ratio=2.0,
        enabled=True,
    )

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert len(payload) == 18
    assert kwargs["module_id"] == 37  # 35 + band_index 2
    assert kwargs["data_id"] == 0
    assert kwargs["channel_type"] == ChannelType.IN


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"slope": 2}, "slope out of range"),
        ({"type": 5}, "type out of range"),
        ({"startup_mode": 2}, "startup mode out of range"),
    ],
)
def test_set_dynamic_eq_rejects_unsupported_enum_values(updates, message):
    client, fake = client_with_fake_transport()
    values = dict(
        freq_hz=1000, gain_db=0.0, qvalue=1.0, slope=0, type=0, startup_mode=0,
        threshold_db=-20.0, start_time_ms=1.0, release_time_ms=100.0, ratio=2.0, enabled=True,
    )
    values.update(updates)
    with pytest.raises(ValueError, match=message):
        client.set_dynamic_eq_band(0, 0, **values)
    assert fake.calls == []


def test_set_compressor_sends_one_struct_frame_with_data_id_zero():
    client, fake = client_with_fake_transport()
    client.set_compressor(
        0,
        threshold_db=-20.0,
        start_time_ms=1.0,
        release_time_ms=100.0,
        ratio=4.0,
        enabled=True,
        output=True,
    )

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert len(payload) == 10
    assert payload[0:2] == (80).to_bytes(2, "little")  # -20dB -> (40.0)*2
    assert kwargs["module_id"] == 40
    assert kwargs["data_id"] == 0
    assert kwargs["channel_type"] == ChannelType.OUT


def test_set_compressor_ratio_uses_compressor_specific_formula():
    # Regression test: compressor_ratio_u16 (raw = ratio*10, no offset) is a
    # different formula from the one Dynamic EQ/Expander's ratio field uses
    # (dyneq_ratio_u16, raw = (ratio-1.0)*100) - swapping the two specs in
    # registry.py (a plausible copy-paste mistake) would otherwise pass every
    # other test, since the registry tests only check data_id/module_id/
    # channel_type. See test_set_expander_ratio_uses_dyneq_shared_formula.
    client, fake = client_with_fake_transport()
    client.set_compressor(
        0,
        threshold_db=0.0,
        start_time_ms=1.0,
        release_time_ms=100.0,
        ratio=4.0,
        enabled=True,
        output=True,
    )
    assert fake.calls[0][1][6:8] == (40).to_bytes(2, "little")


def test_set_compressor_output_targets_output_channel_type():
    client, fake = client_with_fake_transport()
    client.set_compressor(
        0, threshold_db=0.0, start_time_ms=1.0, release_time_ms=100.0, ratio=4.0, enabled=True, output=True
    )
    assert fake.calls[0][2]["channel_type"] == ChannelType.OUT


def test_set_compressor_input_fails_locally_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(NotImplementedError, match="input compressor.*no packet was sent"):
        client.set_compressor(0, threshold_db=0.0, start_time_ms=1.0, release_time_ms=100.0, ratio=4.0, enabled=True)
    assert fake.calls == []


def test_set_limiter_has_no_output_flag_and_uses_output_channel_type():
    client, fake = client_with_fake_transport()
    client.set_limiter(1, threshold_db=-10.0, start_time_ms=1.0, release_time_ms=100.0, enabled=True, detection_type=0)

    assert len(fake.calls) == 1
    _, _, kwargs = fake.calls[0]
    assert kwargs["module_id"] == 41
    assert kwargs["data_id"] == 0
    assert kwargs["channel_type"] == ChannelType.OUT
    assert kwargs["channel_id"] == 1


def test_set_limiter_has_no_output_channel_parameter():
    # Regression test: the limiter exists only on output channels - there is
    # no "output" flag on set_limiter (unlike set_compressor).
    import inspect

    from sinbosen_la548.client import Client

    assert "output" not in inspect.signature(Client.set_limiter).parameters


def test_set_limiter_ratio_slot_is_zeroed_and_ignored():
    client, fake = client_with_fake_transport()
    client.set_limiter(1, threshold_db=-10.0, start_time_ms=1.0, release_time_ms=100.0, enabled=True, detection_type=0)
    _, payload, _ = fake.calls[0]
    assert payload[6:8] == (0).to_bytes(2, "little")


def test_set_limiter_sends_detection_type_in_struct():
    client, fake = client_with_fake_transport()
    client.set_limiter(
        1, threshold_db=-10.0, start_time_ms=1.0, release_time_ms=100.0, enabled=True, detection_type=1
    )
    _, payload, _ = fake.calls[0]
    assert payload[9] == 1


def test_set_limiter_rejects_unknown_detection_type_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="detection type"):
        client.set_limiter(
            1, threshold_db=-10.0, start_time_ms=1.0, release_time_ms=100.0,
            enabled=True, detection_type=2,
        )
    assert fake.calls == []


def test_set_expander_has_no_output_flag_and_uses_input_channel_type():
    client, fake = client_with_fake_transport()
    client.set_expander(0, threshold_db=-40.0, start_time_ms=1.0, release_time_ms=100.0, ratio=2.0, enabled=True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload[0:2] == (120).to_bytes(2, "little")  # -40dB -> (60.0)*2
    assert kwargs["module_id"] == 42
    assert kwargs["data_id"] == 0
    assert kwargs["channel_type"] == ChannelType.IN


def test_set_expander_ratio_uses_dyneq_shared_formula():
    # See test_set_compressor_ratio_uses_compressor_specific_formula - the
    # discriminating counterpart, same ratio value through the other formula.
    client, fake = client_with_fake_transport()
    client.set_expander(0, threshold_db=0.0, start_time_ms=1.0, release_time_ms=100.0, ratio=4.0, enabled=True)
    assert fake.calls[0][1][6:8] == (300).to_bytes(2, "little")


def test_set_mix_route_uses_mix_channel_type_and_zeroed_channel_id():
    client, fake = client_with_fake_transport()
    client.set_mix_route(2, 5, True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload == bytes((2, 5, 1))  # in_index, out_index, value
    assert kwargs["channel_type"] == ChannelType.MIX
    assert kwargs["channel_id"] == 0
    assert kwargs["module_id"] == 0
    assert kwargs["data_id"] == 0


def test_set_mix_route_off_encodes_zero_value_byte():
    client, fake = client_with_fake_transport()
    client.set_mix_route(2, 5, False)
    assert fake.calls[0][1] == bytes((2, 5, 0))


def _read_group_sample() -> bytes:
    """A real read_group() blob captured live from the unit
    (tests/fixtures/read_group_sample.bin) - not synthesized from this
    parser's own assumptions, so it can actually catch a wrong layout
    assumption instead of just confirming the parser agrees with itself."""
    path = Path(__file__).parent / "fixtures" / "read_group_sample.bin"
    return path.read_bytes()


@pytest.mark.parametrize("current_program", [0, 29])
def test_read_group_accepts_a_reply_carrying_the_current_program(current_program):
    # The 0xA9 reply's user_id is the unit's current program slot, not an
    # echo of the request's 0: after a save to slot 29 it was 29, before and
    # after a power cycle. Matching on user_id would time out on such a unit.
    transport = Transport("127.0.0.1", 8087)
    transport._sock.close()
    reply = Frame(opcode=Opcode.READ_GROUP, version=0x13, user_id=current_program, payload=b"blob")
    transport.send = lambda *a, **k: transport._inbox.put(reply)  # type: ignore[method-assign]
    client = Client.__new__(Client)
    client._transport = transport
    try:
        assert client.read_group(timeout=1.0) == b"blob"
    finally:
        transport.stop()


def test_read_mix_matrix_decodes_confirmed_layout():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_GROUP] = _read_group_sample()

    matrix = client.read_mix_matrix()

    # The unit's routing at capture time: inputs A/B (0/1) routed to
    # outputs 1/2 (0/1) respectively, inputs C/D (2/3) still on their
    # untouched factory-default routes (InC->{Out5,Out6}, InD->{Out7,Out8}).
    assert matrix[0][0] is True
    assert matrix[0][1] is False
    assert matrix[1][1] is True
    assert matrix[2][4] is True
    assert matrix[2][5] is True
    assert matrix[3][6] is True
    assert matrix[3][7] is True
    assert matrix[3][0] is False
    assert set(matrix.keys()) == {0, 1, 2, 3}
    assert set(matrix[0].keys()) == set(range(8))


def test_read_mix_matrix_rejects_a_blob_that_does_not_match_the_layout():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_GROUP] = b"\x00" * 50  # too short/wrong shape entirely

    with pytest.raises(ValueError, match="doesn't match"):
        client.read_mix_matrix()


def test_read_lock_state_rejects_an_empty_reply():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_LOCK_STATE] = b""

    with pytest.raises(ValueError, match="malformed ReadLockState reply"):
        client.read_lock_state()


def test_write_mix_matrix_only_sends_entries_that_differ_from_current_state():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_GROUP] = _read_group_sample()

    # current state (see test above): in0->out0, in1->out1, in2->{4,5}, in3->{6,7}
    client.write_mix_matrix({0: {0: True, 2: True}, 1: {1: True}, 2: {4: True, 6: True}})

    # in0/out0, in1/out1, in2/out4 already match current state and must not
    # be resent - only the two genuinely-changed crosspoints are sent.
    set_mix_route_calls = [c for c in fake.calls if c[0] != Opcode.READ_GROUP]
    assert len(set_mix_route_calls) == 2
    payloads = {call_payload for _, call_payload, _ in set_mix_route_calls}
    assert payloads == {bytes((0, 2, 1)), bytes((2, 6, 1))}
    assert all(kwargs["channel_type"] == ChannelType.MIX for _, _, kwargs in set_mix_route_calls)


def test_write_mix_matrix_rejects_out_of_range_index_without_sending_anything():
    client, fake = client_with_fake_transport()

    with pytest.raises(ValueError, match="out of range"):
        client.write_mix_matrix({4: {0: True}})  # only 4 inputs (0-3) on this unit

    with pytest.raises(ValueError, match="out of range"):
        client.write_mix_matrix({0: {8: True}})  # only 8 outputs (0-7) on this unit

    assert fake.calls == []  # validation happens before anything is read or sent


def test_write_mix_matrix_rejects_non_integer_index_or_value_without_sending_anything():
    # Regression test: a float index (or non-bool value) numerically passes
    # the range check (0 <= 1.5 < 8) but isn't a valid key in the decoded
    # mix-matrix dict, and validation must reject it before any real write -
    # not after partially applying earlier, valid-looking entries.
    client, fake = client_with_fake_transport()

    with pytest.raises(TypeError, match="in_index must be an int"):
        client.write_mix_matrix({1.5: {0: True}})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="out_index must be an int"):
        client.write_mix_matrix({0: {0: False, 1.5: True}})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="route value must be a bool"):
        client.write_mix_matrix({0: {0: 1}})  # type: ignore[dict-item]

    assert fake.calls == []  # validation happens before anything is read or sent


@pytest.mark.parametrize("in_index,out_index", [(4, 0), (-1, 0), (0, 8), (0, -1)])
def test_set_mix_route_rejects_nonexistent_crosspoints_without_sending(in_index, out_index):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="mix .*_index out of range"):
        client.set_mix_route(in_index, out_index, True)
    assert fake.calls == []


def test_set_input_source_sends_whole_struct_input_only():
    client, fake = client_with_fake_transport()
    client.set_input_source(0, source_select=1, freq_hz=100, gain_db=-6.0, enabled=True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload == bytes((1,)) + (100).to_bytes(2, "little") + (740).to_bytes(2, "little") + bytes((1,))
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["module_id"] == 43
    assert kwargs["data_id"] == 0


def test_set_input_source_rejects_unsupported_source_select():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="select out of range"):
        client.set_input_source(0, source_select=4, freq_hz=100, gain_db=0, enabled=True)
    assert fake.calls == []


def test_set_fir_sends_whole_struct_and_targets_output_channel_type():
    client, fake = client_with_fake_transport()
    client.set_fir(
        1,
        enabled=True,
        fir_type=1,
        win_type=2,
        freq_low_hz=100,
        freq_high_hz=200,
        attenuation_db=30.0,
        order_index=1,
        output=True,
    )

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    expected = bytes((1, 1, 2)) + (100).to_bytes(2, "little") + (200).to_bytes(2, "little") + bytes((9, 1))
    assert payload == expected
    assert kwargs["channel_type"] == ChannelType.OUT
    assert kwargs["module_id"] == 49
    assert kwargs["data_id"] == 0


def test_set_fir_defaults_to_input_channel_type():
    client, fake = client_with_fake_transport()
    client.set_fir(
        0,
        enabled=False,
        fir_type=0,
        win_type=0,
        freq_low_hz=100,
        freq_high_hz=200,
        attenuation_db=21.0,
        order_index=0,
    )
    assert fake.calls[0][2]["channel_type"] == ChannelType.IN


@pytest.mark.parametrize("order_index", [-1, 2, 255, 1.5, True])
def test_set_fir_rejects_order_absent_from_da0408x_ui_without_sending(order_index):
    client, fake = client_with_fake_transport()
    with pytest.raises((TypeError, ValueError), match="FIR order_index"):
        client.set_fir(
            0,
            enabled=False,
            fir_type=0,
            win_type=0,
            freq_low_hz=100,
            freq_high_hz=200,
            attenuation_db=21.0,
            order_index=order_index,
        )
    assert fake.calls == []


def test_set_freq_shifter_sends_whole_struct_input_only():
    client, fake = client_with_fake_transport()
    client.set_freq_shifter(0, freq_level=3, direction=1, enabled=True)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload == bytes((3, 1, 1))
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["module_id"] == 48
    assert kwargs["data_id"] == 0


def test_save_program_sends_empty_payload_with_slot_as_user_id():
    client, fake = client_with_fake_transport()
    client.save_program(2)

    assert len(fake.calls) == 1
    opcode, payload, kwargs = fake.calls[0]
    assert opcode == Opcode.DEVICE_PROGRAM_SAVE
    assert payload == b""
    assert kwargs["user_id"] == 2
    assert kwargs["linkage"] == 0


def test_load_program_sends_empty_payload_with_slot_as_user_id():
    client, fake = client_with_fake_transport()
    client.load_program(3)

    assert len(fake.calls) == 1
    opcode, payload, kwargs = fake.calls[0]
    assert opcode == Opcode.DEVICE_PROGRAM_READ
    assert payload == b""
    assert kwargs["user_id"] == 3


def test_read_fir_coefs_sends_empty_payload_addressed_by_channel():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_FIR_COEFS] = bytes(2048)
    client.read_fir_coefs(4, output=True)

    assert len(fake.calls) == 1
    opcode, payload, kwargs = fake.calls[0]
    assert opcode == Opcode.READ_FIR_COEFS
    assert payload == b""
    assert kwargs["channel_type"] == ChannelType.OUT
    assert kwargs["channel_id"] == 4
    assert kwargs["user_id"] == 0


def test_read_fir_coefs_defaults_to_input_channel_type():
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_FIR_COEFS] = bytes(2048)
    client.read_fir_coefs(0)
    assert fake.calls[0][2]["channel_type"] == ChannelType.IN


@pytest.mark.parametrize("channel_id,output", [(4, False), (8, True)])
def test_read_fir_coefs_rejects_nonexistent_channels_without_sending(channel_id, output):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="channel_id out of range"):
        client.read_fir_coefs(channel_id, output=output)
    assert fake.calls == []


@pytest.mark.parametrize("size", [0, 2047, 2049, 4096])
def test_read_fir_coefs_rejects_non_da0408x_payload_size(size):
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_FIR_COEFS] = bytes(size)
    with pytest.raises(ValueError, match="exactly 2048 bytes"):
        client.read_fir_coefs(0)


def test_read_fir_coefs_rejects_nonfinite_payload():
    client, fake = client_with_fake_transport()
    payload = bytearray(2048)
    struct.pack_into("<f", payload, 0, float("nan"))
    fake.responses[Opcode.READ_FIR_COEFS] = bytes(payload)
    with pytest.raises(ValueError, match="non-finite"):
        client.read_fir_coefs(0)


def test_write_fir_coefs_sends_exact_da0408x_block_and_address():
    client, fake = client_with_fake_transport()
    payload = struct.pack("<512f", 1.0, *([0.0] * 511))

    client.write_fir_coefs(6, payload, output=True)

    assert fake.calls == [
        (
            Opcode.WRITE_FIR_COEFS,
            payload,
            {"channel_type": ChannelType.OUT, "user_id": 0, "channel_id": 6},
        )
    ]


@pytest.mark.parametrize("size", [0, 2047, 2049, 4096])
def test_write_fir_coefs_rejects_wrong_size_without_sending(size):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="exactly 2048 bytes"):
        client.write_fir_coefs(0, bytes(size))
    assert fake.calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_write_fir_coefs_rejects_nonfinite_value_without_sending(value):
    client, fake = client_with_fake_transport()
    payload = bytearray(2048)
    struct.pack_into("<f", payload, 0, value)
    with pytest.raises(ValueError, match="non-finite"):
        client.write_fir_coefs(0, payload)
    assert fake.calls == []


@pytest.mark.parametrize("channel_id,output", [(4, False), (8, True), (-1, False), (-1, True)])
def test_write_fir_coefs_rejects_nonexistent_channel_without_sending(channel_id, output):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="channel_id"):
        client.write_fir_coefs(channel_id, bytes(2048), output=output)
    assert fake.calls == []


def test_channel_copy_addresses_source_channel_and_marks_target_in_payload():
    client, fake = client_with_fake_transport()
    client.channel_copy(1, 3, 4)

    assert len(fake.calls) == 1
    _, payload, kwargs = fake.calls[0]
    assert payload == b"NNNT"
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["channel_id"] == 1  # source, not target
    assert kwargs["module_id"] == 0
    assert kwargs["data_id"] == 0


def test_channel_copy_output_targets_output_channel_type():
    client, fake = client_with_fake_transport()
    client.channel_copy(0, 1, 8, output=True)
    assert fake.calls[0][2]["channel_type"] == ChannelType.OUT


def test_channel_copy_rejects_multiple_targets_without_sending():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="exactly one target"):
        client.channel_copy(0, [1, 2], 4)
    assert fake.calls == []


@pytest.mark.parametrize("target", [-1, 4, 1.5, True])
def test_channel_copy_rejects_invalid_target_without_sending(target):
    client, fake = client_with_fake_transport()
    with pytest.raises((TypeError, ValueError), match="channel copy target"):
        client.channel_copy(0, target, 4)
    assert fake.calls == []


def test_channel_copy_rejects_source_outside_total_channels():
    # Regression test: source=4 on a 4-channel unit (a plausible 1-based
    # mental-model mistake) used to be accepted and sent as channel_id=4,
    # wrapping onto whatever the device's byte-4 channel_id maps to.
    client, _ = client_with_fake_transport()
    with pytest.raises(ValueError, match="source out of range"):
        client.channel_copy(4, 0, 4)
    with pytest.raises(ValueError, match="source out of range"):
        client.channel_copy(-1, 0, 4)


def test_channel_copy_rejects_empty_target_set():
    client, _ = client_with_fake_transport()
    with pytest.raises(ValueError, match="at least one target"):
        client.channel_copy(0, [], 4)


@pytest.mark.parametrize("total_channels,output", [(2, False), (8, False), (2, True), (4, True)])
def test_channel_copy_rejects_wrong_da0408x_bitmap_width_without_sending(total_channels, output):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="total_channels must be"):
        client.channel_copy(0, 1, total_channels, output=output)
    assert fake.calls == []


def test_set_compressor_validates_all_fields_before_sending_any():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="threshold out of range"):
        client.set_compressor(
            0,
            enabled=True,
            threshold_db=-70.0,
            start_time_ms=1.0,
            release_time_ms=100.0,
            ratio=4.0,
            output=True,
        )
    assert fake.calls == []


def test_set_xover_validates_all_fields_before_sending_any():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="multiple of 6"):
        client.set_xover(0, "hp", freq_hz=100, filter_type=0, slope_db_per_oct=45, enabled=True)
    assert fake.calls == []


def test_set_xover_rejects_unsupported_filter_type():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="filter_type out of range"):
        client.set_xover(0, "hp", freq_hz=80, filter_type=255, slope_db_per_oct=24, enabled=True)
    assert fake.calls == []


@pytest.mark.parametrize("fir_type", [3, 4, 7, 255])
def test_set_fir_rejects_unsupported_fir_type(fir_type):
    """The manufacturer's app sends only 0, 1, 2, 5 and 6."""
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="FIR filter type out of range"):
        client.set_fir(
            0,
            enabled=True,
            fir_type=fir_type,
            win_type=0,
            freq_low_hz=80,
            freq_high_hz=8000,
            attenuation_db=30.0,
            order_index=0,
        )
    assert fake.calls == []


@pytest.mark.parametrize("fir_type", [5, 6])
def test_set_fir_accepts_coefficient_block_type(fir_type):
    """5 selects the coefficient block; the device stores it as 6."""
    client, fake = client_with_fake_transport()
    client.set_fir(
        0,
        enabled=True,
        fir_type=fir_type,
        win_type=0,
        freq_low_hz=80,
        freq_high_hz=8000,
        attenuation_db=30.0,
        order_index=1,
        output=True,
    )
    assert fake.calls[0][1][1] == fir_type


@pytest.mark.parametrize(
    ("freq_level", "direction", "message"),
    [(6, 0, "level out of range"), (0, 2, "direction out of range")],
)
def test_set_freq_shifter_rejects_unsupported_level_and_direction(freq_level, direction, message):
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match=message):
        client.set_freq_shifter(0, freq_level=freq_level, direction=direction, enabled=True)
    assert fake.calls == []


@pytest.mark.parametrize("allpass_type", [3, 4])
def test_set_eq_band_defaults_gain_to_zero_for_both_allpass_types(allpass_type):
    client, fake = client_with_fake_transport()
    client.set_eq_band(0, 0, type=allpass_type, freq_hz=100, q=1.0, slope=0, enabled=True)
    assert fake.calls[0][1] == codecs.eq_struct.encode((100, 0.0, 1.0, 0, allpass_type, True))
