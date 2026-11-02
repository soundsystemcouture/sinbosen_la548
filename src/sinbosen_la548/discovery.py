"""CH9120 chip-level discovery/config protocol.

The LA548's network interface is built on a WCH CH9120 serial-to-Ethernet
bridge chip. This module implements the CH9120 discovery broadcast used to
find units on the LAN. See docs/PROTOCOL.md.

This is a separate, lower-level protocol from the application command
channel in protocol.py/transport.py/client.py, but it shares the same UDP
port (8087).
"""

from __future__ import annotations

import enum
import socket
import struct
import time
from dataclasses import dataclass

MAGIC = b"CH9120_CFG_FLAG\x00"
PORT = 8087
REPLY_FLAG = 0x80

# Packed, no padding: flag[16] (MAGIC), cmd (u8), id[6], cfg_mac[6], len (u8),
# data[255] - 285 bytes. `cmd` and `len` are single bytes, not 32-bit ints.
_STRUCT_FMT = "<16sB6s6sB255s"
_STRUCT_SIZE = struct.calcsize(_STRUCT_FMT)  # 285 bytes


class NetCmd(enum.IntEnum):
    SET = 1
    GET = 2
    RESET = 3
    SEARCH = 4


@dataclass
class DiscoveredDevice:
    ip: str
    mac: bytes
    raw: bytes

    @property
    def mac_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.mac)

    def __repr__(self) -> str:
        return f"DiscoveredDevice(ip={self.ip!r}, mac={self.mac_str!r})"


def _pack(
    cmd: NetCmd,
    id_bytes: bytes = b"\x00" * 6,
    cfg_mac: bytes = b"\x00" * 6,
    data: bytes = b"",
) -> bytes:
    if len(data) > 255:
        raise ValueError("data too long (max 255 bytes)")
    return struct.pack(
        _STRUCT_FMT,
        MAGIC,
        int(cmd),
        id_bytes.ljust(6, b"\x00")[:6],
        cfg_mac.ljust(6, b"\x00")[:6],
        len(data),
        data.ljust(255, b"\x00"),
    )


def _unpack(buf: bytes) -> tuple[int, bytes, bytes, bytes]:
    if len(buf) < _STRUCT_SIZE:
        raise ValueError(f"short discovery reply ({len(buf)} bytes)")
    flag, cmd, id_bytes, cfg_mac, length, data = struct.unpack(_STRUCT_FMT, buf[:_STRUCT_SIZE])
    if flag != MAGIC:
        raise ValueError("bad discovery reply magic")
    return cmd, id_bytes, cfg_mac, data[: min(length, len(data))]


def search(
    timeout: float = 2.0,
    broadcast_addr: str = "255.255.255.255",
    *,
    source_ip: str | None = None,
) -> list[DiscoveredDevice]:
    """Broadcast a CH9120 Search command and collect replies until `timeout`.

    Returns one DiscoveredDevice per distinct replying IP. ``source_ip``
    binds the socket before sending, which is needed to select the LA548's
    Ethernet path on a multi-homed host.
    """
    packet = _pack(NetCmd.SEARCH)
    found: dict[str, DiscoveredDevice] = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if source_ip is not None:
            sock.bind((source_ip, 0))
        sock.sendto(packet, (broadcast_addr, PORT))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                buf, (ip, _port) = sock.recvfrom(4096)
            except TimeoutError:
                break
            try:
                cmd, id_bytes, _cfg_mac, data = _unpack(buf)
            except ValueError:
                continue
            if cmd != (NetCmd.SEARCH | REPLY_FLAG):
                continue
            found[ip] = DiscoveredDevice(ip=ip, mac=id_bytes, raw=data)

    return list(found.values())
