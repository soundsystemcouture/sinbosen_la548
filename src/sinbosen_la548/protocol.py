"""Wire protocol for the LA548 application-level command channel.

See docs/PROTOCOL.md for the full reference, including which opcodes are
confirmed on hardware and which are unverified.

Frame layout (same shape both directions):

    offset  size  field
    0-1     2     0x5A 0x5A sync/magic
    2       1     linkage
    3       1     device_number
    4       1     version / machine model (reply value is opcode-dependent)
    5       1     opcode
    6       1     channel_type
    7       1     user_id
    8       1     channel_id
    9       1     module_id
    10      1     data_id
    11-12   2     payload length, little-endian
    13..    N     payload
    13+N    1     checksum = sum(bytes[0:13+N]) % 256
    14+N    1     0xAA tail marker

The device does not appear to verify the checksum on receipt, but we compute
it correctly on send. On decode the checksum is enforced: every device frame
in the recorded captures (11,033 frames across 9 reply opcodes)
carries a valid one, so a mismatch means corruption or a stray sender, and
the receiver drops the candidate and resyncs rather than trusting it.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

SYNC = b"\x5a\x5a"
TAIL = 0xAA
HEADER_LEN = 13  # bytes 0..12, i.e. everything before the payload
MIN_FRAME_LEN = HEADER_LEN + 2  # + checksum + tail, for a zero-length payload
# Largest payload accepted on receive. The largest reply seen live is the
# 2464-byte ReadGroup (FIR coefficient blocks are 2048, program names 750);
# a header claiming more is treated as a false sync match instead of making
# the receiver wait for up to 64 KiB that will never form a real frame.
MAX_PAYLOAD = 4096


class Opcode(enum.IntEnum):
    """Opcode byte (frame offset 5) values, one per request/reply type."""

    HAND_SHAKE = 0xA1
    WRITE_MODULE_ID = 0xA4
    WRITE_GROUP = 0xA8
    READ_GROUP = 0xA9
    DEVICE_PROGRAM_SAVE = 0xAB
    DEVICE_PROGRAM_READ = 0xAC
    FIRMWARE_UPDATE = 0xAD
    EQ_ALL_BYPASS = 0xAF
    EQ_ALL_RESET = 0xB0
    DATA_RESET = 0xB1
    CHANNEL_COPY = 0xB5
    READ_LOCK_STATE = 0xB6
    CLEAR_PASSWORD = 0xB8
    SET_RESTORE = 0xC5
    READ_FIR_COEFS = 0xC8
    WRITE_FIR_COEFS = 0xC9
    GET_USER_NAME = 0xCA
    SET_USER_NAME = 0xCB
    CHANGE_PASSWORD = 0xD8
    READ_DEVICE_ID = 0xD9
    UPDATE_FA = 0xFA
    UPDATE_FD = 0xFD
    UPDATE_FE = 0xFE
    UPDATE_FF = 0xFF


# Opcodes whose purpose is undetermined (ack-only / empty-payload, as far as
# is known) and which could plausibly be destructive.
# Never send these speculatively against real hardware.
UNKNOWN_DANGEROUS_OPCODES = frozenset(
    {Opcode.UPDATE_FA, Opcode.UPDATE_FD, Opcode.UPDATE_FE, Opcode.UPDATE_FF}
)


class FrameError(ValueError):
    """Raised for malformed frame bytes."""


@dataclass
class Frame:
    opcode: Opcode | int
    payload: bytes = b""
    linkage: int = 0
    device_number: int = 0
    version: int = 10  # request machine model; replies may use an opcode-specific value
    channel_type: int = 0
    user_id: int = 0
    channel_id: int = 0
    module_id: int = 0
    data_id: int = 0

    def encode(self) -> bytes:
        return encode_frame(
            self.opcode,
            self.payload,
            linkage=self.linkage,
            device_number=self.device_number,
            version=self.version,
            channel_type=self.channel_type,
            user_id=self.user_id,
            channel_id=self.channel_id,
            module_id=self.module_id,
            data_id=self.data_id,
        )


def _checksum(data: bytes) -> int:
    return sum(data) % 256


def encode_frame(
    opcode: Opcode | int,
    payload: bytes = b"",
    *,
    linkage: int = 0,
    device_number: int = 0,
    version: int = 10,
    channel_type: int = 0,
    user_id: int = 0,
    channel_id: int = 0,
    module_id: int = 0,
    data_id: int = 0,
) -> bytes:
    """Build a full command-protocol frame, ready to send over UDP."""
    n = len(payload)
    if n > 0xFFFF:
        raise FrameError(f"payload too long ({n} bytes)")

    # Every single-byte header field below must be a real 0-255 value, not
    # masked - masking a caller error (e.g. channel_id=256) silently wraps it
    # into a *different, well-formed frame aimed at a different control*,
    # which the device acks normally. That's the exact class of bug behind
    # the original 0xA4 incident (docs/PROTOCOL.md, "Unsafe opcodes"): a wrong
    # header field that looks fine and gets acked. `bytes()` below raises
    # ValueError on any out-of-range int, which is what we want here.
    try:
        header = bytes(
            (
                SYNC[0],
                SYNC[1],
                linkage,
                device_number,
                version,
                int(opcode),
                channel_type,
                user_id,
                channel_id,
                module_id,
                data_id,
                n & 0xFF,
                (n >> 8) & 0xFF,
            )
        )
    except ValueError as exc:
        raise FrameError(f"header field out of range (0-255): {exc}") from exc
    body = header + bytes(payload)
    return body + bytes((_checksum(body), TAIL))


def _decode_one(chunk: bytes) -> Frame:
    """Decode a single frame from `chunk`, which must be exactly one
    well-formed frame (sync/tail already validated by the caller)."""
    length = chunk[11] | (chunk[12] << 8)
    payload = bytes(chunk[HEADER_LEN : HEADER_LEN + length])
    try:
        opcode: Opcode | int = Opcode(chunk[5])
    except ValueError:
        opcode = chunk[5]
    return Frame(
        opcode=opcode,
        payload=payload,
        linkage=chunk[2],
        device_number=chunk[3],
        version=chunk[4],
        channel_type=chunk[6],
        user_id=chunk[7],
        channel_id=chunk[8],
        module_id=chunk[9],
        data_id=chunk[10],
    )


def decode_frame(data: bytes) -> Frame:
    """Decode exactly one frame from `data`, raising FrameError if `data`
    is not exactly one well-formed frame."""
    if len(data) < MIN_FRAME_LEN:
        raise FrameError(f"frame too short ({len(data)} bytes)")
    if data[0:2] != SYNC:
        raise FrameError("bad sync bytes")
    length = data[11] | (data[12] << 8)
    if length > MAX_PAYLOAD:
        raise FrameError(f"payload length {length} exceeds {MAX_PAYLOAD}")
    expected_len = HEADER_LEN + length + 2
    if len(data) != expected_len:
        raise FrameError(f"length mismatch: header says {expected_len} bytes, got {len(data)}")
    if data[-1] != TAIL:
        raise FrameError("bad tail byte")
    if _checksum(data[: HEADER_LEN + length]) != data[HEADER_LEN + length]:
        raise FrameError("bad checksum")
    return _decode_one(data)


def unpack_frames(
    buf: bytes, *, on_reject: Callable[[str], None] | None = None
) -> tuple[list[Frame], bytes]:
    """Scan `buf` for complete frames, resyncing on SYNC bytes as needed.

    Returns (frames, remainder), where remainder is any trailing bytes that
    don't yet form a complete frame (kept for the next call). A sync match
    whose length field exceeds MAX_PAYLOAD, or whose candidate frame has a
    bad tail or checksum, is skipped one byte at a time until either a valid
    frame or the end of the buffer is found; `on_reject` is called with
    "length", "tail" or "checksum" for each such candidate.
    """
    frames: list[Frame] = []
    i = 0
    n = len(buf)
    while True:
        sync_at = buf.find(SYNC, i)
        if sync_at == -1:
            if n > i and buf[-1] == SYNC[0]:
                return frames, buf[-1:]
            return frames, b""
        if sync_at + HEADER_LEN > n:
            return frames, buf[sync_at:]
        length = buf[sync_at + 11] | (buf[sync_at + 12] << 8)
        if length > MAX_PAYLOAD:
            reason = "length"
        else:
            frame_len = HEADER_LEN + length + 2
            if sync_at + frame_len > n:
                return frames, buf[sync_at:]
            chunk = buf[sync_at : sync_at + frame_len]
            if chunk[-1] != TAIL:
                reason = "tail"
            elif _checksum(chunk[: HEADER_LEN + length]) != chunk[HEADER_LEN + length]:
                reason = "checksum"
            else:
                frames.append(_decode_one(chunk))
                i = sync_at + frame_len
                continue
        if on_reject is not None:
            on_reject(reason)
        i = sync_at + 1
