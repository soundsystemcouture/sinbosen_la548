import queue
import threading
import time
from typing import Any

import pytest

from sinbosen_la548 import protocol
from sinbosen_la548.protocol import Frame, Opcode
from sinbosen_la548.transport import _RECV_BUFFER_SIZE, LINK_TIMEOUT, QUARANTINE_S, Transport, _may_collide


class _FakeSocket:
    """Stands in for the real UDP socket - no real socket I/O in these tests."""

    def sendto(self, *args: object, **kwargs: object) -> None:
        pass

    def close(self) -> None:
        pass


def _make_transport(*, quarantine_s: float = 0.05) -> Transport:
    t = Transport("127.0.0.1", 8087)
    t._sock.close()
    t._sock = _FakeSocket()  # type: ignore[assignment]
    t.quarantine_s = quarantine_s
    return t


def test_request_matches_on_opcode_alone_when_no_header_kwargs_given():
    # get_user_name/read_lock_state/handshake-style requests pass no header
    # kwargs at all - opcode alone must still be enough to match for these.
    t = _make_transport()
    frame = Frame(opcode=Opcode.GET_USER_NAME, payload=b"hi")
    t.send = lambda *a, **k: t._inbox.put(frame)  # type: ignore[method-assign]
    try:
        assert t.request(Opcode.GET_USER_NAME, timeout=1.0) is frame
    finally:
        t.stop()


def test_request_skips_a_stale_reply_with_mismatched_header_fields():
    # Regression test for the "~20 writes share opcode 0xA4" hazard: a late
    # ack for an earlier, different write (different module_id/data_id here)
    # must not be accepted as this request's reply.
    t = _make_transport()
    stale = Frame(opcode=Opcode.WRITE_MODULE_ID, channel_type=0, channel_id=0, module_id=0, data_id=0)
    good = Frame(opcode=Opcode.WRITE_MODULE_ID, channel_type=0, channel_id=0, module_id=1, data_id=1)

    def fake_send(*args: Any, **kwargs: Any) -> None:
        t._inbox.put(stale)
        t._inbox.put(good)

    t.send = fake_send  # type: ignore[method-assign]
    try:
        frame = t.request(
            Opcode.WRITE_MODULE_ID,
            b"\x00",
            timeout=1.0,
            channel_type=0,
            channel_id=0,
            module_id=1,
            data_id=1,
        )
        assert frame is good
    finally:
        t.stop()


def test_request_times_out_if_only_mismatched_frames_ever_arrive():
    t = _make_transport()
    stale = Frame(opcode=Opcode.WRITE_MODULE_ID, module_id=0, data_id=0)
    t.send = lambda *a, **k: t._inbox.put(stale)  # type: ignore[method-assign]
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.WRITE_MODULE_ID, timeout=0.05, module_id=1, data_id=1)
    finally:
        t.stop()


def test_request_recovers_after_a_single_lost_reply_to_the_same_control():
    # Regression test: only the *first* of several requests to the same
    # control ever fails to get a reply (a single genuinely lost ack) - the
    # other three each get their own, immediately-available reply during
    # their own send(). All three later requests must succeed; a fix that
    # can't tell "my own reply" from "an earlier timeout's straggler" would
    # otherwise wrongly discard each one and cascade timeouts forever.
    t = _make_transport()
    attempts = 0

    def fake_send(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            t._inbox.put(Frame(opcode=Opcode.WRITE_MODULE_ID, module_id=0, data_id=0, payload=b"healthy"))

    t.send = fake_send  # type: ignore[method-assign]
    try:
        outcomes: list[Any] = []
        for _ in range(4):
            try:
                outcomes.append(
                    t.request(Opcode.WRITE_MODULE_ID, timeout=0.02, module_id=0, data_id=0).payload
                )
            except TimeoutError:
                outcomes.append("timeout")
        assert outcomes == ["timeout", b"healthy", b"healthy", b"healthy"], outcomes
    finally:
        t.stop()


def test_request_draining_an_old_queued_reply_does_not_poison_the_next_request():
    # Regression test: request()'s existing pre-send drain already discards
    # an old reply that arrived after a prior timeout but before the next
    # call - that alone must be enough. The next request's own fresh reply
    # must still be accepted normally, not treated with lingering suspicion.
    t = _make_transport()
    t.send = lambda *a, **k: None  # type: ignore[method-assign]
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.GET_USER_NAME, timeout=0.02)
        t._inbox.put(Frame(opcode=Opcode.GET_USER_NAME, payload=b"old"))
        t.send = lambda *a, **k: t._inbox.put(Frame(opcode=Opcode.GET_USER_NAME, payload=b"new"))  # type: ignore[method-assign]
        assert t.request(Opcode.GET_USER_NAME, timeout=0.02).payload == b"new"
    finally:
        t.stop()


class _QueueSocket(_FakeSocket):
    """Fake socket whose recvfrom() serves datagrams from its own private
    queue, not the shared Transport._inbox, so tests go through the real
    recv loop (source filter, reassembly, validation)."""

    def __init__(self, source: str = "127.0.0.1") -> None:
        self.datagrams: queue.Queue[bytes] = queue.Queue()
        self.closed = False
        self.source = source

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if self.closed:
            raise OSError("closed")
        try:
            return self.datagrams.get(timeout=0.01), (self.source, 8087)
        except queue.Empty:
            raise TimeoutError from None

    def close(self) -> None:
        self.closed = True


class _ScriptedSocket(_QueueSocket):
    """Answers each sendto() with the next scripted datagram (None = the
    reply is lost) and records when each send happened."""

    def __init__(self, replies: list[bytes | None]) -> None:
        super().__init__()
        self.replies = list(replies)
        self.sent: list[tuple[float, bytes]] = []

    def sendto(self, data: bytes, addr: object) -> None:
        self.sent.append((time.monotonic(), data))
        reply = self.replies.pop(0) if self.replies else None
        if reply is not None:
            self.datagrams.put(reply)


def test_quarantine_default_covers_the_measured_late_reply():
    # docs/PROTOCOL.md: the 0xD9 straggler arrived ~1.05 s after a 0.5 s timeout.
    assert QUARANTINE_S >= 2 * 1.05


def test_late_reply_on_the_same_socket_is_not_taken_for_the_next_request():
    # The device replies to its latest peer endpoint, so a straggler for a
    # timed-out request arrives on the *same* socket the next request uses.
    stale = protocol.encode_frame(Opcode.WRITE_MODULE_ID, b"stale", module_id=7, data_id=0)
    fresh = protocol.encode_frame(Opcode.WRITE_MODULE_ID, b"fresh", module_id=7, data_id=0)
    sock = _ScriptedSocket([None, fresh])
    t = _make_transport(quarantine_s=0.3)
    t._sock = sock  # type: ignore[assignment]
    t.start()
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.WRITE_MODULE_ID, timeout=0.05, module_id=7, data_id=0)
        timed_out_at = time.monotonic()
        threading.Timer(0.15, sock.datagrams.put, args=(stale,)).start()
        frame = t.request(Opcode.WRITE_MODULE_ID, timeout=1.0, module_id=7, data_id=0)
        assert frame.payload == b"fresh"
        assert sock.sent[1][0] - timed_out_at >= 0.25
    finally:
        t.stop()


def test_quarantine_does_not_delay_requests_that_cannot_match_the_straggler():
    other = protocol.encode_frame(Opcode.WRITE_MODULE_ID, b"ok", module_id=8, data_id=0)
    sock = _ScriptedSocket([None, other])
    t = _make_transport(quarantine_s=5.0)
    t._sock = sock  # type: ignore[assignment]
    t.start()
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.WRITE_MODULE_ID, timeout=0.05, module_id=7, data_id=0)
        started = time.monotonic()
        assert t.request(Opcode.WRITE_MODULE_ID, timeout=1.0, module_id=8, data_id=0).payload == b"ok"
        assert time.monotonic() - started < 1.0
    finally:
        t.stop()


def test_signature_collision_rules():
    a4 = int(Opcode.WRITE_MODULE_ID)
    exact = (a4, (("data_id", 0), ("module_id", 7)))
    assert _may_collide(exact, exact)
    assert _may_collide(exact, (a4, (("module_id", 7),)))  # less specific still matches the straggler
    assert _may_collide(exact, (a4, ()))
    assert not _may_collide(exact, (a4, (("data_id", 0), ("module_id", 8))))
    assert not _may_collide(exact, (int(Opcode.READ_GROUP), (("data_id", 0), ("module_id", 7))))


def test_timeout_resets_a_partial_frame_left_in_the_receive_buffer():
    # A reply cut short mid-frame (lost UDP chunk) leaves a header claiming
    # 2000 bytes; without a reset it would swallow the next reply whole.
    truncated = protocol.encode_frame(Opcode.GET_USER_NAME, b"x" * 2000)[:40]
    lock = protocol.encode_frame(Opcode.READ_LOCK_STATE, b"\x000000")
    sock = _ScriptedSocket([truncated, lock])
    t = _make_transport()
    t._sock = sock  # type: ignore[assignment]
    t.start()
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.GET_USER_NAME, timeout=0.1)
        assert t.request(Opcode.READ_LOCK_STATE, timeout=1.0).payload == b"\x000000"
    finally:
        t.stop()


def test_stop_during_a_pending_request_stays_stopped():
    sock = _ScriptedSocket([None])
    t = _make_transport()
    t._sock = sock  # type: ignore[assignment]
    t.start()
    errors: list[BaseException] = []

    def pending() -> None:
        try:
            t.request(Opcode.GET_USER_NAME, timeout=0.3)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=pending)
    worker.start()
    time.sleep(0.05)
    t.stop()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert isinstance(errors[0], TimeoutError)
    assert t._stop.is_set()
    assert t._recv_thread is not None and not t._recv_thread.is_alive()
    with pytest.raises(RuntimeError, match="stopped"):
        t.start()
    with pytest.raises(ConnectionError):
        t.request(Opcode.GET_USER_NAME, timeout=0.05)


def test_recv_loop_ignores_frames_from_a_foreign_source_ip():
    sock = _QueueSocket(source="10.9.9.9")
    t = _make_transport()
    t._sock = sock  # type: ignore[assignment]
    t.start()
    try:
        sock.datagrams.put(protocol.encode_frame(Opcode.GET_USER_NAME, b"spoof"))
        with pytest.raises(TimeoutError):
            t.recv(timeout=0.1)
    finally:
        t.stop()


def test_recv_loop_counts_and_drops_a_corrupt_frame():
    good = protocol.encode_frame(Opcode.GET_USER_NAME, b"good")
    bad = bytearray(protocol.encode_frame(Opcode.GET_USER_NAME, b"evil"))
    bad[-2] ^= 0x10
    sock = _QueueSocket()
    t = _make_transport()
    t._sock = sock  # type: ignore[assignment]
    t.start()
    try:
        sock.datagrams.put(bytes(bad) + good)
        assert t.recv(timeout=1.0).payload == b"good"
        assert t.checksum_errors == 1
        with pytest.raises(TimeoutError):
            t.recv(timeout=0.05)
    finally:
        t.stop()


@pytest.mark.parametrize(
    "field, value",
    [("channel_type", 1), ("user_id", 1), ("channel_id", 1), ("module_id", 1), ("data_id", 1)],
)
def test_request_ignores_a_reply_differing_in_any_one_matched_field(field: str, value: int):
    wanted = {"channel_type": 0, "user_id": 0, "channel_id": 0, "module_id": 0, "data_id": 0}
    wrong = Frame(opcode=Opcode.WRITE_MODULE_ID, **{**wanted, field: value})
    t = _make_transport()
    t.send = lambda *a, **k: t._inbox.put(wrong)  # type: ignore[method-assign]
    try:
        with pytest.raises(TimeoutError):
            t.request(Opcode.WRITE_MODULE_ID, timeout=0.05, **wanted)
    finally:
        t.stop()


def test_transport_resolves_hostnames_once_for_send_and_filter():
    t = Transport("localhost", 8087)
    try:
        assert t._addr == ("127.0.0.1", 8087)
    finally:
        t.stop()


@pytest.mark.parametrize("opcode", [Opcode.UPDATE_FA, Opcode.UPDATE_FD, Opcode.UPDATE_FE, Opcode.UPDATE_FF, 0x42, True])
def test_send_refuses_dangerous_and_unknown_opcodes(opcode: Any):
    t = _make_transport()
    sent: list[object] = []
    t._sock.sendto = lambda *a: sent.append(a)  # type: ignore[method-assign]
    try:
        with pytest.raises((ValueError, TypeError)):
            t.send(opcode)
        with pytest.raises((ValueError, TypeError)):
            t.send_frame(Frame(opcode=opcode))
        assert sent == []
    finally:
        t.stop()


def test_request_holds_request_lock_for_the_full_exchange():
    # Regression test for concurrent request()s stealing each other's
    # replies off the shared inbox: the entire drain-send-await sequence
    # must be serialized, not just the send() pacing call.
    t = _make_transport()
    send_started = threading.Event()
    release_send = threading.Event()
    reply = Frame(opcode=Opcode.GET_USER_NAME, payload=b"hi")

    def fake_send(*args: Any, **kwargs: Any) -> None:
        send_started.set()
        release_send.wait(timeout=2.0)
        t._inbox.put(reply)

    t.send = fake_send  # type: ignore[method-assign]
    thread = threading.Thread(target=lambda: t.request(Opcode.GET_USER_NAME, timeout=2.0))
    try:
        thread.start()
        assert send_started.wait(timeout=1.0)
        assert t._request_lock.locked()
        release_send.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert not t._request_lock.locked()
    finally:
        t.stop()


class _RecvFakeSocket(_FakeSocket):
    """Fake socket whose recvfrom() replays canned datagrams, then raises
    OSError to end _recv_loop deterministically (matching its own
    `except OSError: break` exit path)."""

    def __init__(self, datagrams: list[bytes]) -> None:
        self._datagrams = list(datagrams)
        self.bufsizes: list[int] = []

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        self.bufsizes.append(bufsize)
        if not self._datagrams:
            raise OSError("closed")
        return self._datagrams.pop(0), ("127.0.0.1", 8087)


def test_recv_loop_does_not_queue_handshake_replies():
    t = _make_transport()
    handshake_bytes = protocol.encode_frame(Opcode.HAND_SHAKE, b"\x00" * 4)
    t._sock = _RecvFakeSocket([handshake_bytes, handshake_bytes, handshake_bytes])  # type: ignore[assignment]
    t.start()
    assert t._recv_thread is not None
    t._recv_thread.join(timeout=1.0)
    try:
        assert t._inbox.qsize() == 0
    finally:
        t.stop()


def test_recv_loop_queues_handshake_reply_when_explicitly_awaited():
    # Regression test: request(HAND_SHAKE) must still work. Only *unsolicited*
    # keepalive acks (bug #3) should be dropped - a reply an in-flight
    # request() call is actually waiting for must still reach the inbox.
    t = _make_transport()
    handshake_bytes = protocol.encode_frame(Opcode.HAND_SHAKE, b"\x00" * 4)
    t._sock = _RecvFakeSocket([handshake_bytes])  # type: ignore[assignment]
    t._handshake_reply_wanted = True
    t.start()
    assert t._recv_thread is not None
    t._recv_thread.join(timeout=1.0)
    try:
        assert t._inbox.qsize() == 1
        assert t._inbox.get_nowait().opcode == Opcode.HAND_SHAKE
    finally:
        t.stop()


def test_request_for_handshake_sets_and_clears_the_awaited_flag():
    t = _make_transport()
    seen: dict[str, bool] = {}

    def fake_send(*args: Any, **kwargs: Any) -> None:
        seen["flag_during_send"] = t._handshake_reply_wanted
        t._inbox.put(Frame(opcode=Opcode.HAND_SHAKE))

    t.send = fake_send  # type: ignore[method-assign]
    try:
        frame = t.request(Opcode.HAND_SHAKE, timeout=1.0)
        assert frame.opcode == Opcode.HAND_SHAKE
    finally:
        t.stop()
    assert seen["flag_during_send"] is True
    assert t._handshake_reply_wanted is False


def test_recv_loop_still_queues_non_handshake_replies():
    t = _make_transport()
    reply_bytes = protocol.encode_frame(Opcode.GET_USER_NAME, b"hi")
    t._sock = _RecvFakeSocket([reply_bytes])  # type: ignore[assignment]
    t.start()
    assert t._recv_thread is not None
    t._recv_thread.join(timeout=1.0)
    try:
        assert t._inbox.qsize() == 1
        assert t._inbox.get_nowait().opcode == Opcode.GET_USER_NAME
    finally:
        t.stop()


def test_recv_loop_uses_the_full_protocol_max_buffer_size():
    # A hardcoded small recvfrom() buffer silently truncates any UDP
    # datagram larger than it - this must be sized off the protocol's own
    # max frame length, not an arbitrary constant.
    t = _make_transport()
    fake_sock = _RecvFakeSocket([])
    t._sock = fake_sock  # type: ignore[assignment]
    t.start()
    assert t._recv_thread is not None
    t._recv_thread.join(timeout=1.0)
    try:
        assert fake_sock.bufsizes == [_RECV_BUFFER_SIZE]
    finally:
        t.stop()


def test_stop_sets_link_up_false_and_notifies_listener():
    t = _make_transport()
    t._set_link_up(True)
    calls: list[bool] = []
    t.on_link_change(calls.append)
    t.stop()
    assert t.link_up is False
    assert calls == [False]


class _AlwaysTimesOutSocket(_FakeSocket):
    """Fake socket whose recvfrom() always times out, driving _recv_loop's
    _check_link_timeout path on every iteration - used to trigger a
    link-down transition from *within* the recv thread itself."""

    def __init__(self) -> None:
        self.closed = False

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def test_stop_called_from_a_link_change_callback_does_not_leak_the_socket():
    # Regression test: link-change callbacks run synchronously on the recv
    # thread. A callback that reacts to a disconnect by calling stop() must
    # not hit "RuntimeError: cannot join current thread" and abort cleanup
    # before the socket is closed.
    t = _make_transport()
    t._sock = _AlwaysTimesOutSocket()  # type: ignore[assignment]
    t._set_link_up(True)
    t._last_reply = time.monotonic() - LINK_TIMEOUT - 1  # force immediate timeout

    errors: list[str] = []
    stopped = threading.Event()

    def on_link(up: bool) -> None:
        if not up:
            try:
                t.stop()
            except Exception as exc:  # pragma: no cover - only on regression
                errors.append(str(exc))
            finally:
                stopped.set()

    t.on_link_change(on_link)
    t.start()
    try:
        assert stopped.wait(timeout=1.0)
        assert t._recv_thread is not None
        t._recv_thread.join(timeout=1.0)
        assert not errors, errors
        assert t._sock.closed  # type: ignore[attr-defined]
    finally:
        t.stop()


class _SplitSocket(_FakeSocket):
    """Fake socket that hands sequential chunks to whichever thread calls
    recvfrom() first, keyed by calling thread identity - reproduces two
    recv threads splitting one logical frame across two separate
    reassembly buffers if start() weren't idempotent."""

    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.lock = threading.Lock()
        self.ready = [threading.Event(), threading.Event()]
        self.chunks: list[queue.Queue[bytes]] = [queue.Queue(), queue.Queue()]

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        ident = threading.get_ident()
        with self.lock:
            if ident not in self.thread_ids:
                self.thread_ids.append(ident)
            index = self.thread_ids.index(ident)
            self.ready[index].set()
        try:
            return self.chunks[index].get(timeout=0.01), ("127.0.0.1", 8087)
        except queue.Empty:
            raise TimeoutError from None


def test_duplicate_start_is_idempotent_and_does_not_split_one_frame():
    t = _make_transport()
    sock = _SplitSocket()
    t._sock = sock  # type: ignore[assignment]
    first = None
    try:
        t.start()
        first = t._recv_thread
        assert sock.ready[0].wait(1.0)
        t.start()  # must be a no-op, not spawn a second receiver
        assert t._recv_thread is first
        wire = protocol.encode_frame(Opcode.READ_GROUP, b"x" * 64)
        sock.chunks[0].put(wire[:20])
        sock.chunks[0].put(wire[20:])
        assert t.recv(timeout=1.0).payload == b"x" * 64
    finally:
        t.stop()
        if first is not None:
            first.join(timeout=1.0)


def test_link_watchdog_fires_even_when_datagrams_keep_arriving():
    # Regression test: the watchdog used to run only on the socket-timeout
    # path - continuous junk datagrams that never time out the socket could
    # starve it forever, even six real seconds past the last valid reply.
    t = _make_transport()
    t._set_link_up(True)
    t._last_reply = time.monotonic() - LINK_TIMEOUT - 1
    notifications: list[bool] = []
    t.on_link_change(notifications.append)

    class _JunkSocket(_FakeSocket):
        def __init__(self) -> None:
            self.reads = 0

        def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
            self.reads += 1
            if self.reads >= 100:
                t._stop.set()  # bound this test; no socket timeout ever occurs
            return b"noise", ("127.0.0.1", 8087)

    t._sock = _JunkSocket()  # type: ignore[assignment]
    try:
        t._recv_loop()
        assert t.link_up is False
        assert notifications == [False]
    finally:
        t.stop()


def test_recv_loop_marks_link_down_on_receive_error():
    # Regression test: a terminal receive-side OSError used to end the recv
    # thread without ever clearing link_up, leaving it stuck at whatever it
    # was indefinitely.
    t = _make_transport()
    t._set_link_up(True)

    class _BrokenSocket(_FakeSocket):
        def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
            raise OSError("network failure")

    t._sock = _BrokenSocket()  # type: ignore[assignment]
    try:
        t._recv_loop()
        assert t.link_up is False
    finally:
        t.stop()
