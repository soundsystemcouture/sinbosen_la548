from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from conftest import client_with_fake_transport

from sinbosen_la548 import codecs
from sinbosen_la548.protocol import Opcode
from sinbosen_la548.settings import DA0408X, FIRSnapshot, decode_settings


@pytest.fixture
def snapshot():
    return (Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes()


def test_fixture_profile_and_names(snapshot):
    settings = decode_settings(snapshot)
    assert settings.profile == DA0408X
    assert settings.profile.sample_rate_hz == 96000
    assert settings.profile.sample_rate_verified is True
    assert settings.profile.supports_input_compressor is False
    assert len(settings.inputs) == 4
    assert len(settings.outputs) == 8
    assert [ch.name for ch in settings.inputs] == ["InA", "InB", "InC", "InD"]
    assert [ch.name for ch in settings.outputs] == [f"Out{i}" for i in range(1, 9)]
    assert [ch.channel_id for ch in settings.inputs] == list(range(4))
    assert [ch.channel_id for ch in settings.outputs] == list(range(8))
    assert all(not ch.output and len(ch.eq_bands) == 7 for ch in settings.inputs)
    assert all(ch.output and len(ch.eq_bands) == 15 for ch in settings.outputs)


def test_fixture_core_values_and_codec_q(snapshot):
    settings = decode_settings(snapshot)
    for ch in (*settings.inputs, *settings.outputs):
        assert ch.base.muted is False
        assert ch.base.inverted is (ch.channel_id < 2)
        assert ch.base.gain_db == 0
        assert ch.base.delay_ms == 0
        assert all(not eq.enabled and eq.gain_db == 0 for eq in ch.eq_bands)
        assert ch.fir.enabled is False
    eq = settings.inputs[0].eq_bands[0]
    assert eq.freq_hz == 1000
    assert eq.q == pytest.approx(0.9988262647420053)
    assert settings.inputs[3].eq_bands[-1].freq_hz == 8418
    assert settings.outputs[7].eq_bands[-1].freq_hz == 12975
    for ch in settings.outputs:
        assert (ch.hp.freq_hz, ch.hp.filter_type, ch.hp.slope_db_per_oct, ch.hp.enabled) == (20, 0, 6, False)
        assert (ch.lp.freq_hz, ch.lp.filter_type, ch.lp.slope_db_per_oct, ch.lp.enabled) == (20000, 0, 6, False)


def test_fixture_advanced_blocks_and_capabilities(snapshot):
    settings = decode_settings(snapshot)
    ch = settings.inputs[0]
    assert (ch.input_source.source_select, ch.input_source.freq_hz, ch.input_source.gain_db) == (0, 0, 0)
    assert ch.input_source.enabled is True
    assert ch.expander.threshold_db == -100
    assert ch.expander.start_time_ms == pytest.approx(0.3)
    assert ch.expander.release_time_ms == 50
    assert ch.expander.ratio == 1
    assert ch.expander.enabled is False
    assert len(ch.dynamic_eq_bands) == 3
    for eq in ch.dynamic_eq_bands:
        assert eq.freq_hz == 20
        assert eq.threshold_db == 20
        assert eq.start_time_ms == pytest.approx(0.3)
        assert eq.release_time_ms == 50
        assert eq.ratio == 2
        assert eq.enabled is False
    assert (ch.frequency_shifter.level, ch.frequency_shifter.direction, ch.frequency_shifter.enabled) == (0, 0, False)
    assert settings.inputs[2].frequency_shifter.level == 5
    assert all(
        ch.compressor is None and ch.limiter is None and ch.hp is None and ch.lp is None for ch in settings.inputs
    )
    assert all(
        ch.input_source is None and ch.expander is None and ch.frequency_shifter is None and not ch.dynamic_eq_bands
        for ch in settings.outputs
    )
    assert settings.outputs[0].compressor.threshold_db == 0
    assert settings.outputs[0].compressor.ratio == 1
    assert settings.outputs[0].limiter.detection_type == 0
    # Disabled firmware defaults are read faithfully even if unsuitable for a new write.
    assert settings.outputs[2].compressor.threshold_db == 340
    assert settings.outputs[2].compressor.ratio == 0
    assert settings.outputs[2].compressor.enabled is False


def test_channel_strides_and_last_band_do_not_overlap(snapshot):
    blob = bytearray(snapshot)
    expected = []
    for output, count, stride, start, eq_offset, bands in [(False, 4, 153, 0, 78, 7), (True, 8, 182, 612, 38, 15)]:
        for i in range(count):
            offset = start + i * stride
            gain = -i - (10 if output else 0)
            blob[offset : offset + 1] = codecs.mute_u8.encode(i % 2 == 0)
            blob[offset + 1 : offset + 2] = codecs.phase_u8.encode(i % 2 == 1)
            blob[offset + 4 : offset + 6] = codecs.volume_u16.encode(gain)
            blob[offset + 6 : offset + 8] = codecs.delay_u16.encode(50 + i)
            last = offset + eq_offset + (bands - 1) * 9
            freq = 100 + i + (1000 if output else 0)
            blob[last : last + 9] = codecs.eq_struct.encode((freq, -3, 2, 0, 4, True))
            expected.append((output, i, gain, freq))
    settings = decode_settings(blob)
    for output, i, gain, freq in expected:
        ch = (settings.outputs if output else settings.inputs)[i]
        assert ch.base.muted is (i % 2 == 0)
        assert ch.base.inverted is (i % 2 == 1)
        assert ch.base.gain_db == gain
        assert ch.base.delay_ms == 50 + i
        assert ch.eq_bands[-1].freq_hz == freq
        assert ch.eq_bands[-1].type == 4
        assert ch.eq_bands[-1].enabled is True
        assert not ch.eq_bands[-2].enabled
    assert settings.mix_matrix == decode_settings(snapshot).mix_matrix


def test_distinct_advanced_fields_and_hp_lp_order(snapshot):
    blob = bytearray(snapshot)
    blob[8:14] = codecs.input_source_struct.encode((3, 1234, -7.5, True))
    blob[14:24] = codecs.expander_struct.encode((-45, 2, 125, 3.5, True))
    blob[60:78] = codecs.dyneq_struct.encode((250, -6, 1.5, 1, 4, 1, -30, 3, 120, 4, True))
    blob[141:144] = codecs.freq_shifter_struct.encode((4, 1, True))
    blob[620:630] = codecs.compressor_struct.encode((-24, 2, 125, 4, True))
    blob[630:640] = codecs.limiter_struct.encode((-6, 1, 90, True, 1))
    blob[640:645] = codecs.xover_struct.encode((80, 1, 24, True))
    blob[645:650] = codecs.xover_struct.encode((12000, 2, 48, True))
    settings = decode_settings(blob)
    ch = settings.inputs[0]
    assert (ch.input_source.source_select, ch.input_source.freq_hz, ch.input_source.gain_db) == (3, 1234, -7.5)
    assert (ch.expander.threshold_db, ch.expander.start_time_ms, ch.expander.release_time_ms, ch.expander.ratio) == (
        -45, 2, 125, 3.5
    )
    eq = ch.dynamic_eq_bands[2]
    assert (eq.freq_hz, eq.gain_db, eq.slope, eq.type, eq.startup_mode) == (250, -6, 1, 4, 1)
    assert (eq.threshold_db, eq.start_time_ms, eq.release_time_ms, eq.ratio, eq.enabled) == (-30, 3, 120, 4, True)
    assert eq.q == pytest.approx(codecs.eq_q_u16.decode(codecs.eq_q_u16.encode(1.5)))
    assert ch.dynamic_eq_bands[0].enabled is False
    assert (ch.frequency_shifter.level, ch.frequency_shifter.direction, ch.frequency_shifter.enabled) == (4, 1, True)
    ch = settings.outputs[0]
    assert (ch.compressor.threshold_db, ch.compressor.ratio, ch.compressor.enabled) == (-24, 4, True)
    assert (ch.limiter.threshold_db, ch.limiter.release_time_ms, ch.limiter.detection_type, ch.limiter.enabled) == (
        -6, 90, 1, True
    )
    assert (ch.hp.freq_hz, ch.hp.filter_type, ch.hp.slope_db_per_oct, ch.hp.enabled) == (80, 1, 24, True)
    assert (ch.lp.freq_hz, ch.lp.filter_type, ch.lp.slope_db_per_oct, ch.lp.enabled) == (12000, 2, 48, True)


def test_snapshot_mixer_matches_existing_live_confirmed_parser(snapshot):
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_GROUP] = snapshot
    existing = client.read_mix_matrix()
    settings = decode_settings(snapshot)
    assert settings.mix_matrix == tuple(tuple(existing[i][o] for o in range(8)) for i in range(4))


def test_all_mixer_coordinates_and_binary_states(snapshot):
    blob = bytearray(snapshot)
    for i in range(4):
        for o in range(8):
            # Independent snapshot construction in the confirmed (out, in) order.
            offset = 2068 + (i * 8 + o) * 3
            blob[offset : offset + 3] = bytes((o, i, (i + o) % 2))
    settings = decode_settings(blob)
    assert settings.mix_matrix == tuple(tuple(bool((i + o) % 2) for o in range(8)) for i in range(4))


def test_raw_bytes_preserved_including_unknown_fields_and_name_encoding(snapshot):
    blob = bytearray(snapshot)
    blob[2:4] = b"\xa5\xff"  # Base source/fbx have unresolved semantics.
    blob[85] = 5  # Unresolved EQ type, preserved rather than guessed.
    blob[84] = 255  # Unresolved EQ slope.
    blob[23] = 77  # Expander's unused detectionType byte.
    blob[629] = 88  # Compressor's unused detectionType byte.
    blob[636:638] = b"\xef\xbe"  # Limiter's unused ratio field.
    blob[144:153] = b"\x01\xff\xfe\x00\x80\x01\x02\xfd\xfc"  # Opaque FIR adapter.
    blob[2164:2189] = b"\xffName\0" + b"\x91" * 19
    settings = decode_settings(blob)
    assert settings.raw == bytes(blob)
    ch = settings.inputs[0]
    assert (ch.base.source_raw, ch.base.fbx_raw) == (165, 255)
    assert (ch.eq_bands[0].type, ch.eq_bands[0].slope) == (5, 255)
    assert ch.expander.raw[-1] == 77
    assert settings.outputs[0].compressor.raw[-1] == 88
    assert settings.outputs[0].limiter.raw[6:8] == b"\xef\xbe"
    assert ch.fir.enabled is True
    assert ch.fir.raw == bytes(blob[144:153])
    assert ch.name == "\ufffdName"
    assert ch.name_raw == bytes(blob[2164:2189])
    assert b"".join(c.raw for c in (*settings.inputs, *settings.outputs)) == bytes(blob[:2068])
    assert b"".join(c.name_raw for c in (*settings.inputs, *settings.outputs)) == bytes(blob[2164:])
    assert ch.base.raw + ch.input_source.raw + ch.expander.raw + b"".join(
        eq.raw for eq in ch.dynamic_eq_bands
    ) + b"".join(eq.raw for eq in ch.eq_bands) + ch.frequency_shifter.raw + ch.fir.raw == ch.raw
    out = settings.outputs[0]
    assert out.base.raw + out.compressor.raw + out.limiter.raw + out.hp.raw + out.lp.raw + b"".join(
        eq.raw for eq in out.eq_bands
    ) + out.fir.raw == out.raw


def test_decoded_readback_is_immutable_and_owns_raw_bytes(snapshot):
    blob = bytearray(snapshot)
    settings = decode_settings(blob)
    blob[0] = 0
    assert settings.raw == snapshot
    with pytest.raises(FrozenInstanceError):
        settings.inputs[0].base.gain_db = 12
    with pytest.raises(TypeError):
        settings.mix_matrix[0][0] = False


@pytest.mark.parametrize("size", [0, 100, 2463, 2465, 2704, 2824])
def test_rejects_truncated_extended_and_other_model_snapshots(snapshot, size):
    blob = (snapshot + bytes(size))[:size]
    with pytest.raises(ValueError, match="must be 2464 bytes"):
        decode_settings(blob)


def test_rejects_unsupported_profile(snapshot):
    with pytest.raises(ValueError, match="unsupported settings profile"):
        decode_settings(snapshot, profile=replace(DA0408X, name="DA0408-other", input_eq_bands=16))


@pytest.mark.parametrize("index", range(32))
def test_rejects_wrong_mixer_coordinates_at_every_crosspoint(snapshot, index):
    blob = bytearray(snapshot)
    blob[2068 + index * 3] = 255
    with pytest.raises(ValueError, match="mixer entry.*expected"):
        decode_settings(blob)


def test_rejects_write_order_coordinates_in_snapshot(snapshot):
    blob = bytearray(snapshot)
    blob[2071:2074] = b"\x00\x01\x01"  # (in=0,out=1) is correct for writes, wrong for reads.
    with pytest.raises(ValueError, match="expected.*out=1, in=0"):
        decode_settings(blob)


@pytest.mark.parametrize("offset", [0, 1, 13, 22, 41, 86, 143, 144, 612, 613, 628, 638, 644, 649, 658, 785, 2163])
def test_rejects_nonbinary_flags(snapshot, offset):
    blob = bytearray(snapshot)
    blob[offset] = 2
    with pytest.raises(ValueError, match="invalid boolean"):
        decode_settings(blob)


@pytest.mark.parametrize(
    "offset,value,message",
    [
        (78, 0, "frequency"),
        (78, 20001, "frequency"),
        (80, 401, "EQ gain"),
        (82, 296, "EQ Q"),
        (24, 0, "frequency"),
        (28, 296, "EQ Q"),
        (640, 0, "frequency"),
        (6, 6151, "delay"),
    ],
)
def test_rejects_invalid_known_parameter_values(snapshot, offset, value, message):
    blob = bytearray(snapshot)
    blob[offset : offset + 2] = value.to_bytes(2, "little")
    with pytest.raises(ValueError, match=message):
        decode_settings(blob)


def test_rejects_invalid_crossover_slope(snapshot):
    blob = bytearray(snapshot)
    blob[643] = 8
    with pytest.raises(ValueError, match="crossover slope"):
        decode_settings(blob)


@pytest.mark.parametrize("gain_raw,q_raw", [(0, 0), (400, 295)])
def test_eq_raw_endpoints_remain_readable(snapshot, gain_raw, q_raw):
    blob = bytearray(snapshot)
    blob[80:82] = gain_raw.to_bytes(2, "little")
    blob[82:84] = q_raw.to_bytes(2, "little")
    eq = decode_settings(blob).inputs[0].eq_bands[0]
    assert eq.gain_db == gain_raw / 10 - 20
    assert eq.q == codecs.eq_q_u16.decode(q_raw.to_bytes(2, "little"))


@pytest.mark.parametrize("raw,ms", [(4999, 49.99), (5000, 50), (6150, 1200)])
def test_piecewise_delay_boundary_and_maximum(snapshot, raw, ms):
    blob = bytearray(snapshot)
    blob[6:8] = raw.to_bytes(2, "little")
    assert decode_settings(blob).inputs[0].base.delay_ms == ms


@pytest.mark.parametrize("data", [b"", bytes(8), bytes(10), b"\x02" + bytes(8)])
def test_fir_adapter_rejects_invalid_boundary(data):
    with pytest.raises(ValueError):
        FIRSnapshot.from_bytes(data)


def test_read_settings_performs_exactly_one_read_and_no_writes(snapshot):
    client, fake = client_with_fake_transport()
    fake.responses[Opcode.READ_GROUP] = snapshot
    assert client.read_settings() == decode_settings(snapshot)
    assert fake.calls == [(Opcode.READ_GROUP, b"", {"user_id": 0})]


def test_read_settings_forwards_timeout(snapshot, monkeypatch):
    client, _ = client_with_fake_transport()
    timeouts = []

    def read_group(*, timeout):
        timeouts.append(timeout)
        return snapshot

    monkeypatch.setattr(client, "read_group", read_group)
    client.read_settings(timeout=0.7)
    assert timeouts == [0.7]


def test_read_settings_propagates_network_failure(monkeypatch):
    client, _ = client_with_fake_transport()

    def read_group(*, timeout):
        raise TimeoutError("device unavailable")

    monkeypatch.setattr(client, "read_group", read_group)
    with pytest.raises(TimeoutError, match="device unavailable"):
        client.read_settings()


def test_read_settings_rejects_empty_reply_without_any_writes():
    client, fake = client_with_fake_transport()
    with pytest.raises(ValueError, match="got 0"):
        client.read_settings()
    assert fake.calls == [(Opcode.READ_GROUP, b"", {"user_id": 0})]
