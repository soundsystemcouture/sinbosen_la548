import pytest

from sinbosen_la548.protocol import (
    HEADER_LEN,
    MAX_PAYLOAD,
    SYNC,
    TAIL,
    Frame,
    FrameError,
    Opcode,
    decode_frame,
    encode_frame,
    unpack_frames,
)


def test_encode_frame_empty_payload_shape():
    data = encode_frame(Opcode.HAND_SHAKE)
    assert len(data) == HEADER_LEN + 2
    assert data[0:2] == SYNC
    assert data[5] == Opcode.HAND_SHAKE
    assert data[11:13] == b"\x00\x00"  # zero-length payload
    assert data[-1] == TAIL


def test_encode_frame_checksum_matches_sum_mod_256():
    data = encode_frame(Opcode.READ_DEVICE_ID, b"\x01\x02\x03", device_number=5, version=12)
    body = data[:-2]
    checksum = data[-2]
    assert checksum == sum(body) % 256
    assert data[3] == 5
    assert data[4] == 12


def test_encode_frame_length_field_little_endian():
    payload = bytes(range(0, 40))  # 40-byte payload
    data = encode_frame(Opcode.READ_GROUP, payload)
    assert data[11] == 40
    assert data[12] == 0
    assert len(data) == HEADER_LEN + 40 + 2


def test_round_trip_encode_decode():
    payload = b"hello frame"
    data = encode_frame(
        Opcode.READ_GROUP,
        payload,
        linkage=1,
        device_number=2,
        version=11,
        channel_type=3,
        user_id=4,
        channel_id=5,
        module_id=6,
        data_id=7,
    )
    frame = decode_frame(data)
    assert frame.opcode == Opcode.READ_GROUP
    assert frame.payload == payload
    assert frame.linkage == 1
    assert frame.device_number == 2
    assert frame.version == 11
    assert frame.channel_type == 3
    assert frame.user_id == 4
    assert frame.channel_id == 5
    assert frame.module_id == 6
    assert frame.data_id == 7


def test_decode_frame_unknown_opcode_stays_int():
    data = encode_frame(0x77)
    frame = decode_frame(data)
    assert frame.opcode == 0x77
    assert isinstance(frame.opcode, int)


def test_frame_dataclass_encode_matches_function():
    frame = Frame(opcode=Opcode.EQ_ALL_BYPASS, payload=b"", device_number=9)
    assert frame.encode() == encode_frame(Opcode.EQ_ALL_BYPASS, b"", device_number=9)


def test_decode_frame_rejects_bad_sync():
    data = bytearray(encode_frame(Opcode.HAND_SHAKE))
    data[0] = 0x00
    with pytest.raises(FrameError, match="sync"):
        decode_frame(bytes(data))


def test_decode_frame_rejects_bad_tail():
    data = bytearray(encode_frame(Opcode.HAND_SHAKE))
    data[-1] = 0x00
    with pytest.raises(FrameError, match="tail"):
        decode_frame(bytes(data))


def test_decode_frame_rejects_length_mismatch():
    data = encode_frame(Opcode.HAND_SHAKE) + b"\x00"  # trailing garbage
    with pytest.raises(FrameError, match="length mismatch"):
        decode_frame(data)


def test_decode_frame_rejects_too_short():
    with pytest.raises(FrameError, match="too short"):
        decode_frame(b"\x5a\x5a\x00")


def test_unpack_frames_single():
    data = encode_frame(Opcode.HAND_SHAKE)
    frames, remainder = unpack_frames(data)
    assert len(frames) == 1
    assert frames[0].opcode == Opcode.HAND_SHAKE
    assert remainder == b""


def test_unpack_frames_multiple_concatenated():
    a = encode_frame(Opcode.HAND_SHAKE)
    b = encode_frame(Opcode.READ_DEVICE_ID, b"\x01\x02")
    frames, remainder = unpack_frames(a + b)
    assert len(frames) == 2
    assert frames[0].opcode == Opcode.HAND_SHAKE
    assert frames[1].opcode == Opcode.READ_DEVICE_ID
    assert frames[1].payload == b"\x01\x02"
    assert remainder == b""


def test_unpack_frames_incomplete_tail_kept_as_remainder():
    a = encode_frame(Opcode.HAND_SHAKE)
    b = encode_frame(Opcode.READ_DEVICE_ID, b"\x01\x02\x03\x04")
    partial = a + b[:-3]  # cut off the last few bytes of the second frame
    frames, remainder = unpack_frames(partial)
    assert len(frames) == 1
    assert remainder == b[:-3]


def test_unpack_frames_resyncs_past_garbage():
    garbage = b"\x5a\x5a\xff\xff"  # looks like a sync but isn't a real frame
    good = encode_frame(Opcode.HAND_SHAKE)
    frames, remainder = unpack_frames(garbage + good)
    assert len(frames) == 1
    assert frames[0].opcode == Opcode.HAND_SHAKE
    assert remainder == b""


def test_unpack_frames_empty_buffer():
    frames, remainder = unpack_frames(b"")
    assert frames == []
    assert remainder == b""


def test_unpack_frames_keeps_lone_trailing_sync_byte_as_remainder():
    # A trailing 0x5A could be the first half of a SYNC marker whose second
    # byte just hasn't arrived yet - it must be preserved, not discarded.
    frames, remainder = unpack_frames(b"\x5a")
    assert frames == []
    assert remainder == b"\x5a"


def test_unpack_frames_split_sync_marker_round_trips():
    good = encode_frame(Opcode.HAND_SHAKE)
    frames, remainder = unpack_frames(good[:1])  # just the first sync byte
    assert frames == []
    assert remainder == b"\x5a"

    frames, remainder = unpack_frames(remainder + good[1:])
    assert len(frames) == 1
    assert frames[0].opcode == Opcode.HAND_SHAKE
    assert remainder == b""


def test_decode_frame_rejects_bad_checksum():
    data = bytearray(encode_frame(Opcode.READ_LOCK_STATE, b"\x000000"))
    data[-2] ^= 0x01
    with pytest.raises(FrameError, match="checksum"):
        decode_frame(bytes(data))


def test_decode_frame_rejects_payload_over_max():
    payload = b"\x00" * (MAX_PAYLOAD + 1)
    with pytest.raises(FrameError, match="exceeds"):
        decode_frame(encode_frame(Opcode.READ_GROUP, payload))


def test_unpack_frames_accepts_largest_live_reply():
    # 2464 bytes is the DA0408X ReadGroup reply, the largest seen live.
    frames, remainder = unpack_frames(encode_frame(Opcode.READ_GROUP, b"\x01" * 2464))
    assert len(frames) == 1 and len(frames[0].payload) == 2464
    assert remainder == b""


def test_unpack_frames_drops_bad_checksum_and_resyncs_to_next_frame():
    bad = bytearray(encode_frame(Opcode.WRITE_MODULE_ID, b"\x01", module_id=1))
    bad[-2] ^= 0xFF
    good = encode_frame(Opcode.WRITE_MODULE_ID, b"\x02", module_id=2)
    rejected: list[str] = []
    frames, remainder = unpack_frames(bytes(bad) + good, on_reject=rejected.append)
    assert [(f.module_id, f.payload) for f in frames] == [(2, b"\x02")]
    assert rejected == ["checksum"]
    assert remainder == b""


def test_unpack_frames_resyncs_past_a_false_sync_with_huge_length():
    # A spurious 5A5A whose length field says 0xFFFF must not make the
    # receiver sit on every following frame waiting for 64 KiB.
    junk = b"\x5a\x5a" + b"\x00" * 9 + b"\xff\xff"
    good = [encode_frame(Opcode.WRITE_MODULE_ID, bytes([n]), module_id=n) for n in range(5)]
    rejected: list[str] = []
    frames, remainder = unpack_frames(junk + b"".join(good), on_reject=rejected.append)
    assert [f.module_id for f in frames] == [0, 1, 2, 3, 4]
    assert rejected == ["length"]
    assert remainder == b""


def test_unpack_frames_keeps_payload_sync_bytes_inside_one_frame():
    # The live 0xD9 reply payload itself starts with 5A5A (docs/PROTOCOL.md).
    payload = bytes.fromhex("5a5a000044413034303858")
    wire = encode_frame(Opcode.READ_DEVICE_ID, payload, version=0x13)
    frames, remainder = unpack_frames(wire)
    assert len(frames) == 1 and frames[0].payload == payload and frames[0].version == 0x13
    assert remainder == b""
