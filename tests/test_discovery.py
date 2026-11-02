import socket

import pytest

from sinbosen_la548 import discovery
from sinbosen_la548.discovery import _STRUCT_SIZE, MAGIC, REPLY_FLAG, NetCmd, _pack, _unpack


def test_pack_size_is_285_bytes():
    packet = _pack(NetCmd.SEARCH)
    assert len(packet) == 285
    assert _STRUCT_SIZE == 285


def test_pack_starts_with_magic():
    packet = _pack(NetCmd.SEARCH)
    assert packet[:16] == MAGIC


def test_pack_unpack_round_trip():
    packet = _pack(NetCmd.GET, id_bytes=b"\x01\x02\x03\x04\x05\x06", data=b"payload")
    cmd, id_bytes, cfg_mac, data = _unpack(packet)
    assert cmd == NetCmd.GET
    assert id_bytes == b"\x01\x02\x03\x04\x05\x06"
    assert data == b"payload"


def test_unpack_rejects_bad_magic():
    packet = b"\x00" * 291
    with pytest.raises(ValueError, match="magic"):
        _unpack(packet)


def test_unpack_rejects_short_buffer():
    with pytest.raises(ValueError, match="short"):
        _unpack(b"\x00" * 10)


def test_pack_rejects_oversized_data():
    with pytest.raises(ValueError, match="too long"):
        _pack(NetCmd.SET, data=b"\x00" * 256)


def test_reply_flag_is_cmd_or_0x80():
    # Replies echo the request's cmd byte with the 0x80 reply flag set.
    assert NetCmd.SEARCH | REPLY_FLAG == 0x84


def test_search_can_bind_a_source_address(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.bound = None
            self.sent = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            self.bound = address

        def sendto(self, payload, address):
            self.sent = (payload, address)

        def settimeout(self, timeout):
            pass

        def recvfrom(self, size):
            raise TimeoutError

    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *args: fake)

    assert discovery.search(timeout=0.001, broadcast_addr="192.0.2.255", source_ip="192.0.2.149") == []
    assert fake.bound == ("192.0.2.149", 0)
    assert fake.sent == (_pack(NetCmd.SEARCH), ("192.0.2.255", discovery.PORT))
