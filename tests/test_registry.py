import pytest
from conftest import client_with_fake_transport

from sinbosen_la548 import codecs
from sinbosen_la548.protocol import Opcode
from sinbosen_la548.registry import (
    CHANNEL_COPY_IN,
    CHANNEL_COPY_OUT,
    DEVICE_PROGRAM_READ,
    DEVICE_PROGRAM_SAVE,
    FIR_IN,
    FIR_OUT,
    FREQ_SHIFTER_IN,
    INPUT_SOURCE_IN,
    MIX_ROUTE,
    REGISTRY,
    ChannelType,
    compressor_struct_spec,
    dyneq_struct_spec,
    eq_band_struct_spec,
    expander_struct_spec,
    limiter_struct_spec,
    xover_struct_spec,
)


def test_u8_round_trip():
    assert codecs.u8.decode(codecs.u8.encode(200)) == 200


def test_u8_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        codecs.u8.encode(256)


def test_u16_round_trip_little_endian():
    data = codecs.u16.encode(0x1234)
    assert data == b"\x34\x12"
    assert codecs.u16.decode(data) == 0x1234


def test_mute_u8_inverted_encoding():
    # Regression test for the incident: 0x00 is MUTED, not a harmless
    # default. See docs/PROTOCOL.md, "Unsafe opcodes".
    assert codecs.mute_u8.encode(True) == b"\x00"
    assert codecs.mute_u8.encode(False) == b"\x01"
    assert codecs.mute_u8.decode(b"\x00") is True
    assert codecs.mute_u8.decode(b"\x01") is False


def test_volume_u16_formula_round_trip():
    for db in (-80.0, -20.0, 0.0, 6.0):
        raw = codecs.volume_u16.encode(db)
        assert codecs.volume_u16.decode(raw) == pytest.approx(db)
    assert codecs.volume_u16.encode(0.0) == (800).to_bytes(2, "little")
    assert codecs.volume_u16.encode(-80.0) == (0).to_bytes(2, "little")


def test_volume_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.volume_u16.encode(-81.0)


def test_volume_u16_truncates_not_rounds():
    # Regression test: the volume conversion truncates, not round().
    # (-6.05+80)*10 = 739.5 exactly - round() would go to 740 (banker's
    # rounding), truncation must give 739.
    assert codecs.volume_u16.encode(-6.05) == (739).to_bytes(2, "little")


def test_bool_u8_not_inverted():
    assert codecs.bool_u8.encode(True) == b"\x01"
    assert codecs.bool_u8.encode(False) == b"\x00"
    assert codecs.bool_u8.decode(b"\x01") is True
    assert codecs.bool_u8.decode(b"\x00") is False


def test_freq_u16_round_trip_is_identity():
    assert codecs.freq_u16.decode(codecs.freq_u16.encode(1000)) == 1000
    assert codecs.freq_u16.encode(1000) == (1000).to_bytes(2, "little")


def test_freq_u16_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        codecs.freq_u16.encode(5)
    with pytest.raises(ValueError, match="out of range"):
        codecs.freq_u16.encode(20001)


def test_xover_slope_u8_formula_round_trip():
    for db_per_oct, raw in ((6, 0), (12, 1), (24, 3), (48, 7)):
        assert codecs.xover_slope_u8.encode(db_per_oct) == bytes((raw,))
        assert codecs.xover_slope_u8.decode(bytes((raw,))) == db_per_oct


def test_xover_slope_u8_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        codecs.xover_slope_u8.encode(54)


def test_xover_slope_u8_rejects_non_multiple_of_6():
    # Regression test: this used to silently coerce e.g. 45 -> raw 6 (48
    # dB/oct) via round(45/6)-1 instead of rejecting a value the
    # manufacturer's app never offers.
    with pytest.raises(ValueError, match="multiple of 6"):
        codecs.xover_slope_u8.encode(45)
    with pytest.raises(ValueError, match="multiple of 6"):
        codecs.xover_slope_u8.encode(10)


def test_eq_gain_u16_formula_round_trip():
    for db in (-20.0, -3.0, 0.0, 20.0):
        raw = codecs.eq_gain_u16.encode(db)
        assert codecs.eq_gain_u16.decode(raw) == pytest.approx(db)
    assert codecs.eq_gain_u16.encode(0.0) == (200).to_bytes(2, "little")


def test_eq_gain_u16_rejects_outside_plusminus_20db():
    with pytest.raises(ValueError, match="out of range"):
        codecs.eq_gain_u16.encode(20.1)
    with pytest.raises(ValueError, match="out of range"):
        codecs.eq_gain_u16.encode(-20.1)


def test_eq_q_u16_round_trip():
    # Raw only quantizes octave-bandwidth in 0.01 steps, and Q is highly
    # nonlinear in that near the low-octave/high-Q end of the range - real
    # round-trip error grows there, so the tolerance is looser than a typical
    # codec round-trip test.
    for q in (0.404, 0.5, 0.707, 1.0, 5.0, 20.0, 28.8):
        raw = codecs.eq_q_u16.encode(q)
        assert codecs.eq_q_u16.decode(raw) == pytest.approx(q, rel=0.05)


def test_eq_q_u16_rejects_non_positive():
    with pytest.raises(ValueError, match="must be positive"):
        codecs.eq_q_u16.encode(0.0)


def test_eq_q_u16_rejects_q_above_encodable_maximum():
    # Regression test: int() truncates a small negative pre-range-check value
    # *toward* zero (int(-0.38) == 0), so a Q just above the true ~28.85
    # maximum used to silently encode as raw 0 (Q=28.85) instead of being
    # rejected. floor() goes the correct direction.
    with pytest.raises(ValueError, match="out of encodable range"):
        codecs.eq_q_u16.encode(35.0)


def test_delay_u16_piecewise_boundary():
    # raw 4999/5000/5001 <-> ms 49.99/50.00/50.01, the exact off-by-one spot
    # that is easy to get wrong.
    assert codecs.delay_u16.encode(49.99) == (4999).to_bytes(2, "little")
    assert codecs.delay_u16.encode(50.0) == (5000).to_bytes(2, "little")
    assert codecs.delay_u16.decode((4999).to_bytes(2, "little")) == pytest.approx(49.99)
    assert codecs.delay_u16.decode((5000).to_bytes(2, "little")) == 50.0
    assert codecs.delay_u16.decode((5001).to_bytes(2, "little")) == 51.0


def test_delay_u16_truncates_not_rounds():
    # 0.499 ms * 100 = 49.9 -> truncated raw is 49, not round()'s 50.
    assert codecs.delay_u16.encode(0.499) == (49).to_bytes(2, "little")


def test_delay_u16_not_degraded_by_float_representation_error():
    # Regression test: 1.15 is not exactly representable in binary float, so
    # 1.15 * 100 == 114.99999999999999 - a naive int() truncation drops a
    # whole raw step (114, i.e. 1.14ms) instead of the intended 115 (1.15ms).
    assert codecs.delay_u16.encode(1.15) == (115).to_bytes(2, "little")


def test_delay_u16_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        codecs.delay_u16.encode(1200.1)
    with pytest.raises(ValueError, match="out of range"):
        codecs.delay_u16.encode(-0.1)


def test_empty_codec_ignores_value():
    assert codecs.empty.encode(None) == b""
    assert codecs.empty.decode(b"") is None


def test_dyneq_starttime_u16_piecewise_boundary():
    # raw 6/7 <-> ms 0.9/1.0, the piecewise breakpoint.
    assert codecs.dyneq_starttime_u16.encode(0.9) == (6).to_bytes(2, "little")
    assert codecs.dyneq_starttime_u16.encode(1.0) == (7).to_bytes(2, "little")
    assert codecs.dyneq_starttime_u16.decode((6).to_bytes(2, "little")) == pytest.approx(0.9)
    assert codecs.dyneq_starttime_u16.decode((7).to_bytes(2, "little")) == 1.0


def test_dyneq_starttime_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.dyneq_starttime_u16.encode(0.2)


def test_dyneq_release_time_u16_formula_round_trip():
    assert codecs.dyneq_release_time_u16.encode(50.0) == (0).to_bytes(2, "little")
    assert codecs.dyneq_release_time_u16.encode(150.0) == (100).to_bytes(2, "little")
    assert codecs.dyneq_release_time_u16.decode((100).to_bytes(2, "little")) == 150.0


def test_dyneq_release_time_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.dyneq_release_time_u16.encode(49.0)


def test_dyneq_ratio_u16_formula_round_trip():
    assert codecs.dyneq_ratio_u16.encode(1.0) == (0).to_bytes(2, "little")
    assert codecs.dyneq_ratio_u16.encode(4.0) == (300).to_bytes(2, "little")
    assert codecs.dyneq_ratio_u16.decode((300).to_bytes(2, "little")) == 4.0


def test_dyneq_ratio_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.dyneq_ratio_u16.encode(0.5)


def test_dyneq_ratio_u16_not_degraded_by_float_representation_error():
    # Regression test: (1.2-1.0)*100 == 19.999999999999996 and
    # (8.2-1.0)*100 == 719.9999999999999 - naive int() truncation drops a
    # whole raw step on both.
    assert codecs.dyneq_ratio_u16.encode(1.2) == (20).to_bytes(2, "little")
    assert codecs.dyneq_ratio_u16.encode(8.2) == (720).to_bytes(2, "little")


def test_dyneq_starttime_u16_not_degraded_by_float_representation_error():
    # Regression test: 7.0 - (1.0-0.7)*10.0 == 3.9999999999999996, so naive
    # int() truncation gave raw 3 (0.6ms) instead of the intended raw 4
    # (0.7ms) - which also made raw 4 unreachable from any input.
    assert codecs.dyneq_starttime_u16.encode(0.7) == (4).to_bytes(2, "little")


def test_dyneq_threshold_u16_formula_round_trip():
    assert codecs.dyneq_threshold_u16.encode(-60.0) == (0).to_bytes(2, "little")
    assert codecs.dyneq_threshold_u16.encode(20.0) == (160).to_bytes(2, "little")
    assert codecs.dyneq_threshold_u16.decode((160).to_bytes(2, "little")) == 20.0


def test_dyneq_threshold_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.dyneq_threshold_u16.encode(-60.1)


def test_expander_threshold_u16_formula_round_trip():
    assert codecs.expander_threshold_u16.encode(-100.0) == (0).to_bytes(2, "little")
    assert codecs.expander_threshold_u16.encode(0.0) == (200).to_bytes(2, "little")
    assert codecs.expander_threshold_u16.decode((200).to_bytes(2, "little")) == 0.0


def test_expander_threshold_u16_rejects_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.expander_threshold_u16.encode(-100.1)


def test_compressor_ratio_u16_formula_round_trip():
    assert codecs.compressor_ratio_u16.encode(1.0) == (10).to_bytes(2, "little")
    assert codecs.compressor_ratio_u16.encode(4.0) == (40).to_bytes(2, "little")
    assert codecs.compressor_ratio_u16.encode(128.0) == (1280).to_bytes(2, "little")
    assert codecs.compressor_ratio_u16.decode((40).to_bytes(2, "little")) == 4.0
    # Factory readback of a disabled compressor stores raw 0; it still decodes.
    assert codecs.compressor_ratio_u16.decode((0).to_bytes(2, "little")) == 0.0


@pytest.mark.parametrize("ratio", [-0.1, 0.0, 0.99, 128.1])
def test_compressor_ratio_u16_rejects_outside_supported_range(ratio):
    with pytest.raises(ValueError, match="out of range"):
        codecs.compressor_ratio_u16.encode(ratio)


def test_mix_route_u8_packs_fields_in_confirmed_wire_order():
    # Wire order is input index, output index, value (confirmed by readback).
    assert codecs.mix_route_u8.encode((2, 5, True)) == bytes((2, 5, 1))
    assert codecs.mix_route_u8.encode((2, 5, False)) == bytes((2, 5, 0))
    assert codecs.mix_route_u8.decode(bytes((2, 5, 1))) == (2, 5, True)


def test_mix_route_u8_rejects_out_of_range_indices():
    with pytest.raises(ValueError, match="out of range"):
        codecs.mix_route_u8.encode((256, 0, True))
    with pytest.raises(ValueError, match="out of range"):
        codecs.mix_route_u8.encode((0, 256, True))


def test_fir_struct_packs_fields_in_confirmed_wire_order():
    # Wire order is enable, filter type, window type, low freq, high freq,
    # attenuation, order; attenuation's offset formula is raw = dB - 21.0.
    encoded = codecs.fir_struct.encode((True, 1, 2, 100, 200, 30.0, 5))
    assert encoded == bytes((1, 1, 2)) + (100).to_bytes(2, "little") + (200).to_bytes(2, "little") + bytes((9, 5))
    assert codecs.fir_struct.decode(encoded) == (True, 1, 2, 100, 200, 30.0, 5)


def test_fir_struct_rejects_attenuation_below_floor():
    with pytest.raises(ValueError, match="out of range"):
        codecs.fir_struct.encode((True, 0, 0, 100, 200, 20.0, 0))


def test_fir_struct_truncates_attenuation_not_rounds():
    # Regression test: this used to use round(), which would give byte 0x0A
    # (raw 10) for 30.7 dB; the device expects the truncated 0x09 (raw 9),
    # matching the truncating-cast convention used by every other formula in
    # this file (see codecs._encode_volume).
    encoded = codecs.fir_struct.encode((True, 0, 0, 100, 200, 30.7, 0))
    assert encoded[7] == 9


def test_fir_struct_rejects_win_type_outside_the_supported_range():
    # The manufacturer's app offers exactly 12 window types (0=Kaiser..
    # 11=sine, see docs/PROTOCOL.md's FIR section) - same reasoning as EQ
    # type's 0-5 check.
    with pytest.raises(ValueError, match="out of range"):
        codecs.fir_struct.encode((True, 0, 12, 100, 200, 30.0, 0))
    with pytest.raises(ValueError, match="out of range"):
        codecs.fir_struct.encode((True, 0, -1, 100, 200, 30.0, 0))


def test_fir_struct_does_not_restrict_order_because_its_valid_range_is_tier_dependent():
    # Deliberately NOT range-checked like win_type: order's valid tap-count
    # list depends on the device model, which can't be read from the
    # device, so the generic 0-0xFF wire bound is the only one that's safe
    # to enforce in the codec. This documents that as a deliberate choice,
    # not an oversight.
    encoded = codecs.fir_struct.encode((True, 0, 0, 100, 200, 30.0, 0xFF))
    assert encoded[8] == 0xFF
    with pytest.raises(ValueError, match="out of range"):
        codecs.fir_struct.encode((True, 0, 0, 100, 200, 30.0, 0x100))


def test_input_source_struct_packs_fields_in_confirmed_wire_order():
    # Wire order is source, freq, gain, enable; gain uses the same
    # (dB+80)*10 formula as codecs.volume_u16.
    encoded = codecs.input_source_struct.encode((1, 100, -6.0, True))
    assert encoded == bytes((1,)) + (100).to_bytes(2, "little") + (740).to_bytes(2, "little") + bytes((1,))
    decoded = codecs.input_source_struct.decode(encoded)
    assert decoded[0] == 1
    assert decoded[1] == 100
    assert decoded[2] == pytest.approx(-6.0)
    assert decoded[3] is True


def test_freq_shifter_struct_packs_fields_in_confirmed_wire_order():
    # Wire order is level, direction, enable - all raw u8 passthroughs.
    encoded = codecs.freq_shifter_struct.encode((3, 1, True))
    assert encoded == bytes((3, 1, 1))
    assert codecs.freq_shifter_struct.decode(encoded) == (3, 1, True)


def test_channel_copy_bitmap_marks_target_and_leaves_others_untouched():
    # 'N'=0x4E not-a-target, 'T'=0x54 target, one byte per channel.
    encoded = codecs.channel_copy_bitmap.encode((4, frozenset({2})))
    assert encoded == b"NNTN"
    assert codecs.channel_copy_bitmap.decode(encoded) == (4, frozenset({2}))


def test_channel_copy_bitmap_supports_multiple_targets():
    encoded = codecs.channel_copy_bitmap.encode((4, frozenset({0, 3})))
    assert encoded == b"TNNT"


def test_channel_copy_bitmap_rejects_target_outside_total_channels():
    with pytest.raises(ValueError, match="out of range"):
        codecs.channel_copy_bitmap.encode((4, frozenset({4})))


def test_dyneq_struct_spec_maps_confirmed_module_ids_and_data_id():
    assert dyneq_struct_spec(0).data_id == 0
    assert dyneq_struct_spec(0).module_id == 35
    assert dyneq_struct_spec(2).module_id == 37
    assert dyneq_struct_spec(0).channel_type == ChannelType.IN


def test_dyneq_struct_spec_rejects_band_index_outside_dyneq1_3():
    # Regression test: band_index=5 used to silently land on module_id=40
    # (the Compressor) instead of being rejected - the same "well-formed
    # frame aimed at a different control" hazard as the original incident.
    with pytest.raises(ValueError, match="out of range"):
        dyneq_struct_spec(5)
    with pytest.raises(ValueError, match="out of range"):
        dyneq_struct_spec(-1)


def test_dyneq_struct_spec_round_trips_all_fields():
    spec = dyneq_struct_spec(0)
    payload = spec.codec.encode((1234, 7.5, 2.0, 1, 3, 1, -20.0, 1.0, 100.0, 4.0, True))
    freq_hz, gain_db, qvalue, slope, dtype, mode, threshold_db, start_ms, release_ms, ratio, enabled = (
        spec.codec.decode(payload)
    )
    assert freq_hz == 1234
    assert gain_db == pytest.approx(7.5)
    assert qvalue == pytest.approx(2.0, abs=0.05)
    assert (slope, dtype, mode) == (1, 3, 1)
    assert threshold_db == pytest.approx(-20.0)
    assert start_ms == pytest.approx(1.0)
    assert release_ms == pytest.approx(100.0)
    assert ratio == pytest.approx(4.0)
    assert enabled is True


def test_eq_band_struct_spec_rejects_band_index_outside_eq1_31():
    with pytest.raises(ValueError, match="out of range"):
        eq_band_struct_spec(ChannelType.IN, 31)
    with pytest.raises(ValueError, match="out of range"):
        eq_band_struct_spec(ChannelType.IN, -1)


def test_xover_struct_spec_maps_confirmed_module_ids_and_data_id():
    assert xover_struct_spec("hp").module_id == 38
    assert xover_struct_spec("lp").module_id == 39
    assert xover_struct_spec("hp").data_id == 0
    assert xover_struct_spec("hp").channel_type == ChannelType.OUT


def test_xover_struct_spec_round_trips_all_fields():
    spec = xover_struct_spec("hp")
    payload = spec.codec.encode((1234, 1, 18, True))
    assert spec.codec.decode(payload) == (1234, 1, 18, True)


def test_eq_band_struct_spec_maps_confirmed_data_id_and_band_module_id():
    assert eq_band_struct_spec(ChannelType.IN, 0).data_id == 0
    assert eq_band_struct_spec(ChannelType.IN, 0).module_id == 4
    assert eq_band_struct_spec(ChannelType.IN, 5).module_id == 9
    assert eq_band_struct_spec(ChannelType.OUT, 0).channel_type == ChannelType.OUT


def test_eq_band_struct_spec_round_trips_all_fields():
    spec = eq_band_struct_spec(ChannelType.OUT, 0)
    payload = spec.codec.encode((1234, 7.5, 2.0, 1, 3, True))
    freq_hz, gain_db, q, slope, eq_type, enabled = spec.codec.decode(payload)
    assert freq_hz == 1234
    assert gain_db == pytest.approx(7.5)
    assert q == pytest.approx(2.0, abs=0.05)
    assert (slope, eq_type, enabled) == (1, 3, True)


def test_eq_band_struct_spec_rejects_type_outside_the_supported_range():
    spec = eq_band_struct_spec(ChannelType.OUT, 0)
    with pytest.raises(ValueError, match="out of range"):
        spec.codec.encode((1234, 0.0, 1.0, 0, 6, True))
    with pytest.raises(ValueError, match="out of range"):
        spec.codec.encode((1234, 0.0, 1.0, 0, -1, True))


def test_compressor_struct_spec_maps_confirmed_module_id_and_data_id():
    assert compressor_struct_spec(ChannelType.IN).data_id == 0
    assert compressor_struct_spec(ChannelType.IN).module_id == 40
    assert compressor_struct_spec(ChannelType.OUT).module_id == 40
    assert compressor_struct_spec(ChannelType.OUT).channel_type == ChannelType.OUT


def test_compressor_struct_spec_round_trips_all_fields():
    spec = compressor_struct_spec(ChannelType.IN)
    payload = spec.codec.encode((-20.0, 1.0, 100.0, 4.0, True))
    threshold_db, start_ms, release_ms, ratio, enabled = spec.codec.decode(payload)
    assert threshold_db == pytest.approx(-20.0)
    assert start_ms == pytest.approx(1.0)
    assert release_ms == pytest.approx(100.0)
    assert ratio == pytest.approx(4.0)
    assert enabled is True


def test_compressor_ratio_uses_its_own_formula_not_dyneq_expander_shared_one():
    # Regression test: compressor's ratio field uses compressor_ratio_u16
    # (raw = ratio*10), a different formula from the dyneq_ratio_u16 that
    # Dynamic EQ/Expander's ratio field shares (raw = (ratio-1.0)*100) -
    # round-tripping the same nominal ratio through each struct's codec and
    # checking the two decode to the same value would NOT catch a copy-paste
    # formula swap (both formulas are invertible), so compare raw encoded
    # bytes against each formula computed independently instead.
    comp_payload = compressor_struct_spec(ChannelType.IN).codec.encode((-20.0, 1.0, 100.0, 4.0, True))
    exp_payload = expander_struct_spec().codec.encode((-20.0, 1.0, 100.0, 4.0, True))
    # compressor_ratio_u16(4.0) = raw 40 = 0x28; dyneq_ratio_u16(4.0) = raw 300 = 0x012C.
    assert comp_payload[6:8] == (40).to_bytes(2, "little")
    assert exp_payload[6:8] == (300).to_bytes(2, "little")


def test_limiter_struct_spec_maps_confirmed_module_id_and_data_id():
    assert limiter_struct_spec().data_id == 0
    assert limiter_struct_spec().module_id == 41
    assert limiter_struct_spec().channel_type == ChannelType.OUT


def test_limiter_struct_spec_round_trips_all_fields():
    spec = limiter_struct_spec()
    payload = spec.codec.encode((-20.0, 1.0, 100.0, True, 1))
    threshold_db, start_ms, release_ms, enabled, detection_type = spec.codec.decode(payload)
    assert threshold_db == pytest.approx(-20.0)
    assert start_ms == pytest.approx(1.0)
    assert release_ms == pytest.approx(100.0)
    assert enabled is True
    assert detection_type == 1
    # Stored snapshots keep any raw u8 on decode; only encoding is restricted.
    assert spec.codec.decode(payload[:9] + b"\x07")[4] == 7
    for bad in (2, 7, True):
        with pytest.raises(ValueError, match="detection_type out of range"):
            spec.codec.encode((-20.0, 1.0, 100.0, True, bad))


def test_expander_struct_spec_maps_confirmed_module_id_and_data_id():
    assert expander_struct_spec().data_id == 0
    assert expander_struct_spec().module_id == 42
    assert expander_struct_spec().channel_type == ChannelType.IN


def test_expander_struct_spec_round_trips_all_fields():
    spec = expander_struct_spec()
    payload = spec.codec.encode((-50.0, 1.0, 100.0, 4.0, True))
    threshold_db, start_ms, release_ms, ratio, enabled = spec.codec.decode(payload)
    assert threshold_db == pytest.approx(-50.0)
    assert start_ms == pytest.approx(1.0)
    assert release_ms == pytest.approx(100.0)
    assert ratio == pytest.approx(4.0)
    assert enabled is True


def test_expander_threshold_uses_its_own_formula_not_compressor_limiter_shared_one():
    # Regression test: Expander's threshold has a -100 dB floor (expander_
    # threshold_u16) vs. Compressor/Limiter's -60 dB floor (dyneq_threshold_
    # u16) - both formulas accept -50.0 dB, so compare raw encoded bytes
    # rather than a round trip (which can't distinguish two invertible
    # formulas that happen to agree at one value).
    comp_payload = compressor_struct_spec(ChannelType.IN).codec.encode((-50.0, 1.0, 100.0, 4.0, True))
    exp_payload = expander_struct_spec().codec.encode((-50.0, 1.0, 100.0, 4.0, True))
    # dyneq_threshold_u16(-50.0) = raw 20 = 0x0014; expander_threshold_u16(-50.0) = raw 100 = 0x0064.
    assert comp_payload[0:2] == (20).to_bytes(2, "little")
    assert exp_payload[0:2] == (100).to_bytes(2, "little")


@pytest.mark.parametrize(
    "name",
    [
        "mute_in",
        "mute_out",
        "volume_in",
        "volume_out",
        "phase_in",
        "phase_out",
        "delay_in",
        "delay_out",
        "eq_all_bypass_in",
        "eq_all_bypass_out",
        "eq_all_reset_in",
        "eq_all_reset_out",
        "mix_route",
        "input_source_in",
        "fir_in",
        "fir_out",
        "freq_shifter_in",
        "device_program_save",
        "device_program_read",
        "channel_copy_in",
        "channel_copy_out",
    ],
)
def test_registry_has_entry(name: str):
    assert name in REGISTRY
    assert REGISTRY[name].name == name


def test_eq_all_bypass_reset_use_their_own_opcodes_not_write_module_id():
    # Unlike every other REGISTRY entry, these two don't go through the
    # generic 0xA4 write - they're their own dedicated opcodes.
    assert REGISTRY["eq_all_bypass_in"].opcode == Opcode.EQ_ALL_BYPASS
    assert REGISTRY["eq_all_reset_in"].opcode == Opcode.EQ_ALL_RESET


def test_registry_input_output_variants_differ_only_by_channel_type():
    assert REGISTRY["mute_in"].channel_type == ChannelType.IN
    assert REGISTRY["mute_out"].channel_type == ChannelType.OUT
    assert REGISTRY["mute_in"].module_id == REGISTRY["mute_out"].module_id
    assert REGISTRY["mute_in"].data_id == REGISTRY["mute_out"].data_id


def test_mix_route_uses_mix_channel_type_not_in_or_out():
    # Structurally different from every other REGISTRY entry: addressed by
    # ChannelType.MIX, with module_id/data_id both always 0 (not an
    # omitted/sentinel value) and the real crosspoint address carried
    # entirely in the payload.
    assert MIX_ROUTE.channel_type == ChannelType.MIX
    assert MIX_ROUTE.module_id == 0
    assert MIX_ROUTE.data_id == 0
    assert MIX_ROUTE.opcode == Opcode.WRITE_MODULE_ID


def test_input_source_fir_freq_shifter_pin_data_id_zero_for_determinism():
    # All three share Mix matrix's "whole struct, not per-field" shape, and
    # like the EQ all-bypass/all-reset data_id and Mix matrix's channel_id,
    # data_id carries no meaning on these frames - this client pins it to 0.
    for spec in (INPUT_SOURCE_IN, FIR_IN, FIR_OUT, FREQ_SHIFTER_IN):
        assert spec.data_id == 0
        assert spec.opcode == Opcode.WRITE_MODULE_ID

    assert INPUT_SOURCE_IN.channel_type == ChannelType.IN
    assert INPUT_SOURCE_IN.module_id == 43
    assert FREQ_SHIFTER_IN.channel_type == ChannelType.IN
    assert FREQ_SHIFTER_IN.module_id == 48
    assert FIR_IN.channel_type == ChannelType.IN
    assert FIR_OUT.channel_type == ChannelType.OUT
    assert FIR_IN.module_id == FIR_OUT.module_id == 49


def test_device_program_save_read_use_their_own_opcodes_with_zeroed_module_data_id():
    # channel_type/channel_id/module_id/data_id carry no meaning for these
    # two, so this client pins them to 0 for determinism, the same
    # convention as the EQ all-bypass/all-reset and Mix matrix specs above.
    # The meaningful field for these two is user_id (the program slot),
    # passed per-call rather than fixed in the spec.
    assert DEVICE_PROGRAM_SAVE.opcode == Opcode.DEVICE_PROGRAM_SAVE
    assert DEVICE_PROGRAM_READ.opcode == Opcode.DEVICE_PROGRAM_READ
    for spec in (DEVICE_PROGRAM_SAVE, DEVICE_PROGRAM_READ):
        assert spec.module_id == 0
        assert spec.data_id == 0


def test_channel_copy_uses_source_channel_type_variants_with_zeroed_module_data_id():
    # channel_id is the *source* channel (passed per-call), channel_type
    # distinguishes in/out, and module_id/data_id carry no meaning (pinned
    # to 0, same convention as above).
    assert CHANNEL_COPY_IN.opcode == Opcode.CHANNEL_COPY
    assert CHANNEL_COPY_OUT.opcode == Opcode.CHANNEL_COPY
    assert CHANNEL_COPY_IN.channel_type == ChannelType.IN
    assert CHANNEL_COPY_OUT.channel_type == ChannelType.OUT
    for spec in (CHANNEL_COPY_IN, CHANNEL_COPY_OUT):
        assert spec.module_id == 0
        assert spec.data_id == 0


def test_write_param_encodes_via_spec_codec_and_addresses_the_right_channel():
    client, fake = client_with_fake_transport()
    client._write_param(REGISTRY["mute_in"], channel_id=3, value=True)

    assert len(fake.calls) == 1
    opcode, payload, kwargs = fake.calls[0]
    assert opcode == Opcode.WRITE_MODULE_ID
    assert payload == b"\x00"  # muted -> 0x00, confirmed inverted encoding
    assert kwargs["channel_type"] == ChannelType.IN
    assert kwargs["channel_id"] == 3
    assert kwargs["module_id"] == 0
    assert kwargs["data_id"] == 0


def test_write_param_always_forces_linkage_zero():
    client, fake = client_with_fake_transport()
    client._write_param(REGISTRY["volume_out"], channel_id=1, value=-6.0)

    _, _, kwargs = fake.calls[0]
    assert kwargs["linkage"] == 0


def test_write_param_defaults_user_id_zero_and_is_overridable():
    client, fake = client_with_fake_transport()
    client._write_param(REGISTRY["phase_in"], channel_id=0, value=1)
    assert fake.calls[0][2]["user_id"] == 0

    client._write_param(REGISTRY["phase_in"], channel_id=0, value=1, user_id=5)
    assert fake.calls[1][2]["user_id"] == 5


def test_write_param_rejects_out_of_range_channel_id():
    # Regression test: channel_id=256 used to silently wrap to 0 at the wire
    # level (encode_frame masked it with & 0xFF) - the same "well-formed
    # frame aimed at a different channel" hazard as the original incident.
    client, _ = client_with_fake_transport()
    with pytest.raises(ValueError, match="channel_id out of range"):
        client._write_param(REGISTRY["mute_in"], channel_id=256, value=True)
    with pytest.raises(ValueError, match="channel_id out of range"):
        client._write_param(REGISTRY["mute_in"], channel_id=-1, value=True)


def test_write_param_rejects_out_of_range_user_id():
    # Regression test: user_id=256 (e.g. a program slot) used to silently
    # wrap to 0 - `save_program(256)` would overwrite slot 0 with no error.
    client, _ = client_with_fake_transport()
    with pytest.raises(ValueError, match="user_id out of range"):
        client._write_param(REGISTRY["mute_in"], channel_id=0, value=True, user_id=256)


@pytest.mark.parametrize("channel_type", [ChannelType.MIX, ChannelType.DEFAULT, 7, True, 1.0])
def test_direction_spec_builders_reject_non_in_out_channel_types(channel_type):
    with pytest.raises(ValueError, match="channel_type"):
        eq_band_struct_spec(channel_type, 0)
    with pytest.raises(ValueError, match="channel_type"):
        compressor_struct_spec(channel_type)


@pytest.mark.parametrize("band_index", [True, 1.0, "1"])
def test_band_spec_builders_require_int_band_index(band_index):
    with pytest.raises(TypeError, match="band_index"):
        eq_band_struct_spec(ChannelType.IN, band_index)
    with pytest.raises(TypeError, match="band_index"):
        dyneq_struct_spec(band_index)
