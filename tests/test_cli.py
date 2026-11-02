import argparse
import struct
from pathlib import Path

import pytest
from conftest import FakeTransport

from sinbosen_la548 import cli, codecs
from sinbosen_la548.client import Client, LockState
from sinbosen_la548.protocol import Opcode
from sinbosen_la548.registry import ChannelType
from sinbosen_la548.settings import decode_settings

FIXTURE = (Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes()


class _FakeClient:
    def __init__(self, payload: bytes, *, locked: bool = False) -> None:
        self._payload = payload
        self.locked = locked
        self.fir_writes: list[bytes] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read_group(self) -> bytes:
        return self._payload

    def read_fir_coefs(self, channel: int, *, output: bool = False) -> bytes:
        return self._payload

    def write_fir_coefs(self, channel: int, coefficients: bytes, *, output: bool = False) -> None:
        self.fir_writes.append(coefficients)
        self._payload = coefficients

    def read_lock_state(self) -> LockState:
        return LockState(self.locked, "0000")

    def read_settings(self):
        return decode_settings(FIXTURE)


def test_dump_group_refuses_to_overwrite_existing_backup_on_empty_reply(monkeypatch, tmp_path: Path):
    # Regression test: an empty/short reply used to be written unconditionally
    # ("wb"), silently destroying a previously-good backup at the same path.
    out = tmp_path / "backup.bin"
    out.write_bytes(b"a previous good backup")
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(b""))

    rc = cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out)))

    assert rc == 1
    assert out.read_bytes() == b"a previous good backup"


def test_dump_group_writes_file_on_valid_snapshot(monkeypatch, tmp_path: Path):
    out = tmp_path / "backup.bin"
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(FIXTURE))

    rc = cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out), no_validate=False))

    assert rc == 0
    assert out.read_bytes() == FIXTURE
    assert [f.name for f in tmp_path.iterdir()] == ["backup.bin"]


def test_dump_group_refuses_a_reply_that_is_not_a_snapshot(monkeypatch, tmp_path: Path):
    out = tmp_path / "backup.bin"
    out.write_bytes(b"a previous good backup")
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(b"real data"))

    assert cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out), no_validate=False)) == 1
    assert out.read_bytes() == b"a previous good backup"
    assert cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out), no_validate=True)) == 0
    assert out.read_bytes() == b"real data"


def test_interrupted_backup_write_keeps_the_previous_file(monkeypatch, tmp_path: Path):
    out = tmp_path / "backup.bin"
    out.write_bytes(b"a previous good backup")
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(FIXTURE))

    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli.os, "fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out), no_validate=False))
    assert out.read_bytes() == b"a previous good backup"
    assert [f.name for f in tmp_path.iterdir()] == ["backup.bin"]


@pytest.mark.parametrize("existing_mode", [None, 0o640])
def test_backup_write_keeps_the_mode_a_plain_open_would_give(monkeypatch, tmp_path: Path, existing_mode):
    out = tmp_path / "backup.bin"
    if existing_mode is not None:
        out.write_bytes(b"old")
        out.chmod(existing_mode)
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(FIXTURE))
    previous = cli.os.umask(0o022)
    try:
        assert cli._cmd_dump_group(argparse.Namespace(ip="1.2.3.4", out=str(out), no_validate=False)) == 0
    finally:
        cli.os.umask(previous)
    assert out.stat().st_mode & 0o777 == (existing_mode if existing_mode is not None else 0o644)


def test_read_fir_coefs_refuses_to_overwrite_existing_backup_on_empty_reply(monkeypatch, tmp_path: Path):
    out = tmp_path / "fir0.bin"
    out.write_bytes(b"a previous good backup")
    monkeypatch.setattr(cli, "Client", lambda ip: _FakeClient(b""))

    rc = cli._cmd_read_fir_coefs(argparse.Namespace(ip="1.2.3.4", channel=0, out=str(out), output=False))

    assert rc == 1
    assert out.read_bytes() == b"a previous good backup"


def test_write_fir_coefs_rejects_wrong_file_size_before_constructing_client(monkeypatch, tmp_path: Path):
    source = tmp_path / "bad.bin"
    source.write_bytes(bytes(2047))
    monkeypatch.setattr(cli, "Client", lambda ip: pytest.fail("must not construct a client"))

    rc = cli._cmd_write_fir_coefs(
        argparse.Namespace(ip="1.2.3.4", channel=0, input=str(source), output=False)
    )

    assert rc == 2


def test_write_fir_coefs_restores_and_verifies_exact_file(monkeypatch, tmp_path: Path):
    source = tmp_path / "fir.bin"
    payload = struct.pack("<512f", 1.0, *([0.0] * 511))
    source.write_bytes(payload)
    fake = _FakeClient(bytes(2048))
    monkeypatch.setattr(cli, "Client", lambda ip: fake)

    rc = cli._cmd_write_fir_coefs(
        argparse.Namespace(ip="1.2.3.4", channel=2, input=str(source), output=True)
    )

    assert rc == 0
    assert fake._payload == payload


def test_write_fir_coefs_refuses_a_locked_device_without_writing(monkeypatch, tmp_path: Path, capsys):
    source = tmp_path / "fir.bin"
    source.write_bytes(struct.pack("<512f", 1.0, *([0.0] * 511)))
    fake = _FakeClient(bytes(2048), locked=True)
    monkeypatch.setattr(cli, "Client", lambda ip: fake)

    rc = cli._cmd_write_fir_coefs(argparse.Namespace(ip="1.2.3.4", channel=0, input=str(source), output=False))

    assert rc == 1
    assert fake.fir_writes == []
    assert "locked" in capsys.readouterr().err


def test_write_fir_coefs_rolls_back_when_readback_differs(monkeypatch, tmp_path: Path, capsys):
    source = tmp_path / "fir.bin"
    desired = struct.pack("<512f", 1.0, *([0.0] * 511))
    source.write_bytes(desired)
    original = struct.pack("<512f", 0.5, *([0.0] * 511))
    fake = _FakeClient(original)
    corrupted = struct.pack("<512f", 0.25, *([0.0] * 511))
    real_write = fake.write_fir_coefs

    def lossy_write(channel: int, coefficients: bytes, *, output: bool = False) -> None:
        real_write(channel, coefficients, output=output)
        if coefficients == desired:
            fake._payload = corrupted

    fake.write_fir_coefs = lossy_write  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "Client", lambda ip: fake)

    rc = cli._cmd_write_fir_coefs(argparse.Namespace(ip="1.2.3.4", channel=1, input=str(source), output=True))

    assert rc == 1
    assert fake.fir_writes == [desired, original]
    assert "original coefficients restored" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("before", "after", "code"),
    [(b"abc", b"abc", 0), (b"abc", b"abd", 1), (b"abc", b"abcd", 1), (b"abcd", b"abc", 1)],
)
def test_diff_group_uses_diff_exit_codes(tmp_path: Path, before: bytes, after: bytes, code: int):
    (tmp_path / "a").write_bytes(before)
    (tmp_path / "b").write_bytes(after)
    args = cli.build_parser().parse_args(["diff-group", str(tmp_path / "a"), str(tmp_path / "b")])
    assert args.func(args) == code


def test_diff_group_missing_file_is_trouble(tmp_path: Path):
    (tmp_path / "a").write_bytes(b"abc")
    args = cli.build_parser().parse_args(["diff-group", str(tmp_path / "a"), str(tmp_path / "missing")])
    assert args.func(args) == 2


class _SessionTransport(FakeTransport):
    """FakeTransport plus the lifecycle hooks Client.connect/close use."""

    def __init__(self) -> None:
        super().__init__()
        self.responses[Opcode.READ_GROUP] = FIXTURE
        self.responses[Opcode.READ_LOCK_STATE] = b"\x000000"
        self.link_up = True

    def start(self) -> None:
        pass

    def start_keepalive(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _cli_client(monkeypatch) -> _SessionTransport:
    transport = _SessionTransport()

    def factory(ip: str) -> Client:
        client = Client.__new__(Client)
        client._transport = transport  # type: ignore[assignment]
        return client

    monkeypatch.setattr(cli, "Client", factory)
    return transport


IN, OUT, MIX = ChannelType.IN, ChannelType.OUT, ChannelType.MIX

SET_COMMANDS = [
    (["set-mute", "1", "mute"], IN, 1, 0, 0, codecs.mute_u8.encode(True)),
    (["set-mute", "2", "unmute", "--output"], OUT, 2, 0, 0, codecs.mute_u8.encode(False)),
    (["set-volume", "3", "-6.5", "--output"], OUT, 3, 1, 1, codecs.volume_u16.encode(-6.5)),
    (["set-phase", "0", "invert"], IN, 0, 2, 2, codecs.phase_u8.encode(True)),
    (["set-delay", "7", "12.5", "--output"], OUT, 7, 3, 3, codecs.delay_u16.encode(12.5)),
    (["set-xover", "5", "lp", "--freq", "2000", "--type", "1", "--slope", "24", "--enable"], OUT, 5, 39, 0,
     codecs.xover_struct.encode((2000, 1, 24, True))),
    (["set-eq-band", "2", "3", "--freq", "800", "--gain", "-4", "--q", "2", "--type", "0", "--slope", "0",
      "--enable"], IN, 2, 7, 0, codecs.eq_struct.encode((800, -4.0, 2.0, 0, 0, True))),
    (["set-dynamic-eq-band", "1", "2", "--freq", "500", "--gain", "3", "--q", "1.5", "--type", "0", "--slope",
      "0", "--startup-mode", "1", "--start-time", "5", "--release-time", "200", "--ratio", "2",
      "--threshold", "-30", "--enable"], IN, 1, 37, 0,
     codecs.dyneq_struct.encode((500, 3.0, 1.5, 0, 0, 1, -30.0, 5.0, 200.0, 2.0, True))),
    (["set-compressor", "4", "--start-time", "10", "--release-time", "100", "--ratio", "4", "--threshold",
      "-20", "--enable", "--output"], OUT, 4, 40, 0, codecs.compressor_struct.encode((-20.0, 10.0, 100.0, 4.0, True))),
    (["set-limiter", "6", "--start-time", "1", "--release-time", "60", "--threshold", "-3", "--detection-type",
      "1", "--disable"], OUT, 6, 41, 0, codecs.limiter_struct.encode((-3.0, 1.0, 60.0, False, 1))),
    (["set-expander", "3", "--start-time", "2", "--release-time", "80", "--ratio", "3", "--threshold", "-70",
      "--enable"], IN, 3, 42, 0, codecs.expander_struct.encode((-70.0, 2.0, 80.0, 3.0, True))),
    (["set-mix-route", "2", "5", "on"], MIX, 0, 0, 0, codecs.mix_route_u8.encode((2, 5, True))),
    (["set-input-source", "1", "on", "--source", "1", "--freq", "1000", "--gain", "-10"], IN, 1, 43, 0,
     codecs.input_source_struct.encode((1, 1000, -10.0, True))),
    (["set-fir", "0", "on", "--type", "0", "--win-type", "1", "--freq-low", "100", "--freq-high", "1000",
      "--atten", "40", "--order", "1"], IN, 0, 49, 0, codecs.fir_struct.encode((True, 0, 1, 100, 1000, 40.0, 1))),
    (["set-freq-shifter", "2", "3", "0", "on"], IN, 2, 48, 0, codecs.freq_shifter_struct.encode((3, 0, True))),
]


@pytest.mark.parametrize(("argv", "channel_type", "channel_id", "module_id", "data_id", "payload"), SET_COMMANDS,
                         ids=[" ".join(c[0][:2]) for c in SET_COMMANDS])
def test_every_set_command_sends_its_exact_frame(monkeypatch, capsys, argv, channel_type, channel_id, module_id,
                                                 data_id, payload):
    transport = _cli_client(monkeypatch)
    args = cli.build_parser().parse_args([argv[0], "192.0.2.1", *argv[1:]])

    assert args.func(args) == 0

    writes = [call for call in transport.calls if call[0] == Opcode.WRITE_MODULE_ID]
    assert len(writes) == 1
    opcode, sent, header = writes[0]
    assert sent == payload
    assert (header["channel_type"], header["channel_id"], header["module_id"], header["data_id"]) == (
        channel_type, channel_id, module_id, data_id)
    assert header["linkage"] == 0
    assert "acknowledged" in capsys.readouterr().out


def test_verify_reports_a_readback_mismatch(monkeypatch, capsys):
    _cli_client(monkeypatch)  # readback stays the fixture, where output 3 delay is 0 ms
    args = cli.build_parser().parse_args(["set-delay", "192.0.2.1", "3", "12.5", "--output", "--verify"])
    assert args.func(args) == 1
    assert "does not match" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["set-phase", "1", "invert"],  # fixture input 2 is already inverted
    ["set-mix-route", "0", "0", "on"],  # fixture InA -> Out1 is already routed
    ["set-xover", "0", "hp", "--freq", "20", "--type", "0", "--slope", "6", "--disable"],
])
def test_verify_accepts_readback_matching_the_write(monkeypatch, capsys, argv):
    _cli_client(monkeypatch)
    args = cli.build_parser().parse_args([argv[0], "192.0.2.1", *argv[1:], "--verify"])
    assert args.func(args) == 0
    assert "verified" in capsys.readouterr().out


def test_search_forwards_source_ip(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.discovery, "search", lambda **kwargs: calls.append(kwargs) or [])

    rc = cli._cmd_search(argparse.Namespace(timeout=1.25, source_ip="192.0.2.149"))

    assert rc == 1
    assert calls == [{"timeout": 1.25, "source_ip": "192.0.2.149"}]
    assert "No devices found" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (cli._cmd_eq_bypass, argparse.Namespace(ip="1.2.3.4", channel=0, state="on", output=False)),
        (cli._cmd_eq_reset, argparse.Namespace(ip="1.2.3.4", channel=0, output=False)),
    ],
)
def test_unsupported_eq_all_cli_commands_do_not_construct_a_client(monkeypatch, capsys, command, args):
    monkeypatch.setattr(cli, "Client", lambda ip: pytest.fail("must not construct a client"))
    assert command(args) == 2
    assert "no packet was sent" in capsys.readouterr().err
