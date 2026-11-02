"""UDP transport for the LA548 application-level command protocol.

Owns one UDP socket to a single unit, runs a background HandShake keepalive
(180ms send interval / ~6s link timeout), and lets callers do simple
request/reply exchanges on top.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable

from . import protocol
from .protocol import Frame, Opcode

DEFAULT_PORT = 8087
HANDSHAKE_INTERVAL = 0.18  # seconds between keepalive HandShake frames
LINK_TIMEOUT = 6.0  # seconds without any reply before the link is reported down
MIN_SEND_GAP = 0.06  # seconds; the device drops a frame that arrives with ~no
# gap after a previous one (confirmed on hardware: two sends microseconds
# apart reliably lose one reply, a >=50ms gap reliably doesn't). 60 ms keeps
# a margin over that measured threshold. This is enforced across all sends
# (keepalive and explicit requests alike).
QUARANTINE_S = 3.0  # seconds a timed-out request's reply signature stays
# quarantined (see Transport.request). The only measured late reply, the
# controlled 0xD9 trial (docs/PROTOCOL.md), arrived 1.548 s after its request,
# about 1 s after that request's 0.5 s timeout; 3 s leaves ~2x margin.


# Header fields a reply must echo back to count as the match for a request
# that specified them - see Transport.request for why opcode alone isn't
# enough for 0xA4. `linkage`/`device_number`/`version` are deliberately not
# included: linkage is always 0 from this client (nothing to disambiguate),
# device_number is connection-level, and version is opcode-dependent in live
# replies (0x0A on normal reads, 0x13 on HandShake and the captured D9 reply).
_REPLY_MATCH_FIELDS = ("channel_type", "user_id", "channel_id", "module_id", "data_id")

# protocol.HEADER_LEN + max payload (0xFFFF) + checksum + tail - the largest
# frame this protocol's own length field can describe, and safely above the
# max possible UDP datagram (65507 bytes) besides.
_RECV_BUFFER_SIZE = protocol.HEADER_LEN + 0xFFFF + 2

# (opcode, sorted matched header fields) - what a reply has to look like to
# be accepted for a request; see _matches_request.
_Signature = tuple[int, tuple[tuple[str, int], ...]]


def _matches_request(frame: Frame, kwargs: dict[str, int]) -> bool:
    return all(getattr(frame, field) == value for field, value in kwargs.items() if field in _REPLY_MATCH_FIELDS)


def _signature(opcode: Opcode | int, kwargs: dict[str, int]) -> _Signature:
    return int(opcode), tuple(sorted((f, v) for f, v in kwargs.items() if f in _REPLY_MATCH_FIELDS))


def _may_collide(a: _Signature, b: _Signature) -> bool:
    """Whether one reply could satisfy both signatures: same opcode and no
    header field that both specify with different values. A request that
    specifies fewer fields than a quarantined one (e.g. module_id only)
    would still accept that request's straggler, so it collides too."""
    if a[0] != b[0]:
        return False
    fields_a, fields_b = dict(a[1]), dict(b[1])
    return all(fields_a[f] == fields_b[f] for f in fields_a.keys() & fields_b.keys())


def _require_sendable(opcode: Opcode | int) -> Opcode:
    """Refuse any opcode this client has no business sending: unknown ints
    (not in the Opcode table, so never verified) and the UPDATE_F* family
    whose purpose was never determined (protocol.py)."""
    if isinstance(opcode, bool):
        raise TypeError(f"opcode must be an Opcode, got bool {opcode!r}")
    try:
        known = Opcode(int(opcode))
    except ValueError:
        raise ValueError(f"refusing to send unknown opcode 0x{int(opcode):02X}; no packet was sent") from None
    if known in protocol.UNKNOWN_DANGEROUS_OPCODES:
        raise ValueError(f"refusing to send {known.name} (0x{known:02X}), purpose unknown; no packet was sent")
    return known


class Transport:
    """UDP connection to one LA548 unit."""

    def __init__(self, ip: str, port: int = DEFAULT_PORT, *, device_number: int = 0, version: int = 10):
        # Resolve once, IPv4 to match AF_INET: the same address is both the
        # sendto() target and the source filter in _recv_loop, so a hostname
        # (or "localhost") must not be compared as text against reply IPs.
        self._addr = (socket.gethostbyname(ip), port)
        self.device_number = device_number
        self.version = version
        self.quarantine_s = QUARANTINE_S
        self.rejected_frames: Counter[str] = Counter()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.2)
        self._recv_thread: threading.Thread | None = None
        self._handshake_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset_recv_buffer = threading.Event()
        self._keepalive_started = threading.Event()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._quarantine: dict[_Signature, float] = {}
        self._handshake_reply_wanted = False
        self._last_send = 0.0
        self._last_reply = time.monotonic()
        self._link_up = False
        self._on_link_change: Callable[[bool], None] | None = None
        self._inbox: queue.Queue[Frame] = queue.Queue()

    @property
    def checksum_errors(self) -> int:
        """Received frames dropped for a bad checksum. Every device frame in
        the recorded captures validates, so a nonzero count means corruption
        or a sender other than the LA548."""
        return self.rejected_frames["checksum"]

    @property
    def link_up(self) -> bool:
        return self._link_up

    def on_link_change(self, callback: Callable[[bool], None]) -> None:
        self._on_link_change = callback

    def start(self) -> None:
        """Start listening for incoming frames. Does not start the HandShake
        keepalive - call start_keepalive() once the session is bootstrapped
        (see Client.connect()), since firing it immediately races the very
        first request against the device's firmware, which appears to drop
        one of two frames received back-to-back with no gap.

        Idempotent - calling this again while already running is a no-op,
        so a caller (including a repeated Client.connect()) doesn't need to
        guard against a duplicate start itself. Without this, two recv
        threads would both read from the same socket, each with its own
        frame-reassembly buffer, silently losing any reply whose bytes
        happen to split across two recvfrom() calls that land on different
        threads.

        Single use: once stop() has run, the socket is closed and this
        raises - create a new Transport (or Client) instead. service.py
        already builds a fresh Client per connect.
        """
        if self._stop.is_set():
            raise RuntimeError("transport is stopped; create a new Transport to reconnect")
        if self._recv_thread is not None and self._recv_thread.is_alive():
            return
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def start_keepalive(self) -> None:
        if self._keepalive_started.is_set():
            return
        self._keepalive_started.set()
        self._handshake_thread = threading.Thread(target=self._handshake_loop, daemon=True)
        self._handshake_thread.start()

    def stop(self) -> None:
        self._stop.set()
        # A link-change callback runs synchronously on the recv thread and
        # may itself call stop() (e.g. to react to a disconnect) - joining
        # that same thread from within itself would raise RuntimeError and
        # abort cleanup before the socket is ever closed, so skip whichever
        # thread happens to be the caller.
        current = threading.current_thread()
        if self._recv_thread and self._recv_thread is not current:
            self._recv_thread.join(timeout=1.0)
        if self._handshake_thread and self._handshake_thread is not current:
            self._handshake_thread.join(timeout=1.0)
        self._sock.close()
        self._set_link_up(False)

    def __enter__(self) -> Transport:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def send(self, opcode: Opcode | int, payload: bytes = b"", **kwargs: int) -> None:
        frame_bytes = protocol.encode_frame(
            _require_sendable(opcode),
            payload,
            device_number=self.device_number,
            version=self.version,
            **kwargs,
        )
        self._send_paced(frame_bytes)

    def send_frame(self, frame: Frame) -> None:
        _require_sendable(frame.opcode)
        self._send_paced(frame.encode())

    def _send_paced(self, frame_bytes: bytes) -> None:
        with self._send_lock:
            wait = self._last_send + MIN_SEND_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._sock.sendto(frame_bytes, self._addr)
            self._last_send = time.monotonic()

    def recv(self, timeout: float = 2.0) -> Frame:
        """Block for the next received frame (any opcode)."""
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no frame received") from None

    def request(self, opcode: Opcode | int, payload: bytes = b"", *, timeout: float = 2.0, **kwargs: int) -> Frame:
        """Send a frame and wait for the matching reply.

        Matching on opcode alone isn't enough for 0xA4 (WriteModuleId): it's
        shared by ~20 different controls (see registry.py), so a reply
        that's merely late could otherwise be picked up as the ack for a
        *different* 0xA4 write sent moments later. So beyond the opcode, we
        also require the reply to echo back every header field this request
        actually specified (channel_type/user_id/channel_id/module_id/
        data_id, whichever of these were passed in `kwargs`) - unrelated
        frames (handshake acks, replies for other controls) are ignored
        rather than returned. Live-confirmed across the staged 0xA4 hardware
        tests; device_number/version are deliberately not part of this match
        because version is opcode-dependent (docs/PROTOCOL.md).

        Header fields alone can't distinguish two writes to the *same*
        control (mute=True then mute=False on one channel): the protocol has
        no per-message nonce, and acks aren't confirmed to echo the payload.
        Two measures narrow that gap:

        - Quarantine after a timeout. The timed-out request's reply may
          still arrive - on this same socket: the 0xD9 trial showed the
          device sends late replies to its single, most recent peer
          endpoint, so changing local port does not divert them. The
          request's signature is quarantined for `quarantine_s`; a later
          request whose reply could match it first waits that out, then
          drains the inbox, so the straggler is discarded rather than taken
          as the new request's reply. Requests that can't match it proceed
          at once. The recv loop's partial-frame buffer is also reset, so a
          reply cut short mid-frame can't swallow the next one.
        - The pre-send drain discards anything already queued.

        Neither excludes a *duplicate* reply (seen live: two 0xB6 frames for
        one request) that lands after the drain but before the real reply,
        nor a straggler slower than the quarantine. An ack therefore says a
        frame was received, not that the write applied - the service's
        readback after apply (and the CLI's --verify) is the proof.
        """
        signature = _signature(opcode, kwargs)
        with self._request_lock:
            if self._stop.is_set():
                raise ConnectionError("transport is stopped; create a new Transport to reconnect")
            self._wait_out_quarantine(signature)
            self._drain_inbox()
            awaiting_handshake = int(opcode) == int(Opcode.HAND_SHAKE)
            if awaiting_handshake:
                self._handshake_reply_wanted = True
            try:
                self.send(opcode, payload, **kwargs)
                deadline = time.monotonic() + timeout
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        frame = self.recv(timeout=remaining)
                    except TimeoutError:
                        break
                    if int(frame.opcode) == int(opcode) and _matches_request(frame, kwargs):
                        return frame
                self._quarantine[signature] = time.monotonic() + self.quarantine_s
                self._reset_recv_buffer.set()
                raise TimeoutError(f"no reply to opcode {opcode!r}")
            finally:
                if awaiting_handshake:
                    self._handshake_reply_wanted = False

    def _wait_out_quarantine(self, signature: _Signature) -> None:
        """Block (request lock held) until no unexpired quarantine entry
        could share a reply with `signature`; see request()."""
        now = time.monotonic()
        self._quarantine = {sig: until for sig, until in self._quarantine.items() if until > now}
        until = max((u for sig, u in self._quarantine.items() if _may_collide(sig, signature)), default=None)
        if until is None:
            return
        if self._stop.wait(until - now):
            raise ConnectionError("transport stopped while waiting out a timed-out request's quarantine")
        now = time.monotonic()
        self._quarantine = {sig: u for sig, u in self._quarantine.items() if u > now}

    def _drain_inbox(self) -> None:
        while not self._inbox.empty():
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break

    def _recv_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            self._check_link_timeout()
            try:
                data, addr = self._sock.recvfrom(_RECV_BUFFER_SIZE)
            except TimeoutError:
                continue
            except OSError:
                self._set_link_up(False)
                break
            if self._reset_recv_buffer.is_set():
                # Checked after recvfrom: request() sets this before sending
                # anything new, so whatever arrives from here on is fresh.
                self._reset_recv_buffer.clear()
                buf = b""
            if addr[0] != self._addr[0]:
                continue
            buf += data
            frames, buf = protocol.unpack_frames(buf, on_reject=self._count_rejected)
            for frame in frames:
                self._last_reply = time.monotonic()
                self._set_link_up(True)
                if frame.opcode == Opcode.HAND_SHAKE and not self._handshake_reply_wanted:
                    # Unsolicited keepalive ack (its payload is per-channel
                    # meter levels, docs/PROTOCOL.md "Keepalive and meter levels", currently
                    # unused) - drop it so it doesn't accumulate in the inbox
                    # forever. But if a request(HAND_SHAKE) call is actually
                    # waiting for a reply, let it through so that call can
                    # complete instead of always timing out.
                    continue
                self._inbox.put(frame)

    def _count_rejected(self, reason: str) -> None:
        self.rejected_frames[reason] += 1

    def _handshake_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.send(Opcode.HAND_SHAKE)
            except OSError:
                pass
            self._stop.wait(HANDSHAKE_INTERVAL)

    def _check_link_timeout(self) -> None:
        if self._link_up and (time.monotonic() - self._last_reply) > LINK_TIMEOUT:
            self._set_link_up(False)

    def _set_link_up(self, up: bool) -> None:
        if up != self._link_up:
            self._link_up = up
            if self._on_link_change:
                self._on_link_change(up)
