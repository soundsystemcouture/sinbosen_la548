"""Command-line interface for the LA548 Linux client."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import discovery
from .blobdiff import diff_blobs
from .client import Client
from .service import ControlKey, FIRRestoreError, controls, restore_fir_coefficients, validated_fir_coefficients
from .settings import decode_settings


def _write_atomic(path: str, data: bytes) -> None:
    """Replace `path` only once `data` is fully on disk, so an interrupted
    write can never leave a truncated backup in place of a good one.

    mkstemp creates the file 0600; give it the mode a plain open() would
    have: the existing file's, or 0666 minus the umask."""
    target = Path(path)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _finish_write(client: Client, args: argparse.Namespace, key: ControlKey) -> int:
    """Report a set-* write. An ack only means the frame was received
    (transport.Transport.request), so --verify reads the settings back and
    compares the stored control with exactly the bytes that were sent."""
    if not args.verify:
        print("acknowledged by device (not verified; pass --verify to read back)")
        return 0
    if client.last_write is None:
        raise RuntimeError("no acknowledged write to verify")
    spec, _, payload = client.last_write
    stored = controls(client.read_settings())[key]
    if key.block == "route":
        matches = stored == spec.codec.decode(payload)[2]
    else:
        raw = getattr(stored, "raw", None)
        matches = (raw if raw is not None else spec.codec.encode(stored)) == payload
    if not matches:
        print(f"error: readback of {key.label} does not match the write (stored {stored!r})", file=sys.stderr)
        return 1
    print(f"verified: readback of {key.label} matches the write")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    devices = discovery.search(timeout=args.timeout, source_ip=args.source_ip)
    if not devices:
        print("No devices found. Discovery is unconfirmed against real hardware - see docs/PROTOCOL.md.")
        return 1
    for dev in devices:
        print(f"{dev.ip}\t{dev.mac_str}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        lock = client.read_lock_state()
        name = client.get_program_names()[0]
        print(f"link:   {'up' if client.link_up else 'down'}")
        print(f"program 1: {name}")
        print(f"locked: {lock.locked}")
    return 0


def _cmd_dump_group(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        blob = client.read_group()
    if not blob:
        print(f"error: device returned an empty reply, not overwriting {args.out}", file=sys.stderr)
        return 1
    if not args.no_validate:
        try:
            decode_settings(blob)
        except ValueError as exc:
            print(f"error: reply is not a valid DA0408X snapshot ({exc}), not overwriting {args.out}; "
                  "pass --no-validate to save it anyway", file=sys.stderr)
            return 1
    _write_atomic(args.out, blob)
    print(f"wrote {len(blob)} bytes to {args.out}")
    return 0


def _cmd_set_mute(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_mute(args.channel, args.state == "mute", output=args.output)
        return _finish_write(client, args, ControlKey(args.output, args.channel, "mute"))


def _cmd_set_volume(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_volume(args.channel, args.db, output=args.output)
        return _finish_write(client, args, ControlKey(args.output, args.channel, "gain"))


def _cmd_set_phase(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_phase(args.channel, args.state == "invert", output=args.output)
        return _finish_write(client, args, ControlKey(args.output, args.channel, "polarity"))


def _cmd_set_delay(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_delay(args.channel, args.ms, output=args.output)
        return _finish_write(client, args, ControlKey(args.output, args.channel, "delay"))


def _cmd_set_xover(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_xover(
            args.channel,
            args.band,
            freq_hz=args.freq,
            filter_type=args.type,
            slope_db_per_oct=args.slope,
            enabled=args.enabled,
        )
        return _finish_write(client, args, ControlKey(True, args.channel, args.band))


def _cmd_set_eq_band(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_eq_band(
            args.channel,
            args.band_index,
            freq_hz=args.freq,
            gain_db=args.gain,
            q=args.q,
            type=args.type,
            slope=args.slope,
            enabled=args.enabled,
            output=args.output,
        )
        return _finish_write(client, args, ControlKey(args.output, args.channel, "eq", args.band_index))


def _cmd_eq_bypass(args: argparse.Namespace) -> int:
    print("EQ-all bypass is unsupported by this firmware; no packet was sent.", file=sys.stderr)
    return 2


def _cmd_eq_reset(args: argparse.Namespace) -> int:
    print("EQ-all reset is a firmware no-op; no packet was sent.", file=sys.stderr)
    return 2


def _cmd_set_dynamic_eq_band(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_dynamic_eq_band(
            args.channel,
            args.band_index,
            freq_hz=args.freq,
            gain_db=args.gain,
            qvalue=args.q,
            type=args.type,
            slope=args.slope,
            startup_mode=args.startup_mode,
            start_time_ms=args.start_time,
            release_time_ms=args.release_time,
            ratio=args.ratio,
            threshold_db=args.threshold,
            enabled=args.enabled,
        )
        return _finish_write(client, args, ControlKey(False, args.channel, "dyneq", args.band_index))


def _cmd_set_compressor(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_compressor(
            args.channel,
            threshold_db=args.threshold,
            start_time_ms=args.start_time,
            release_time_ms=args.release_time,
            ratio=args.ratio,
            enabled=args.enabled,
            output=args.output,
        )
        return _finish_write(client, args, ControlKey(args.output, args.channel, "compressor"))


def _cmd_set_limiter(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_limiter(
            args.channel,
            threshold_db=args.threshold,
            start_time_ms=args.start_time,
            release_time_ms=args.release_time,
            enabled=args.enabled,
            detection_type=args.detection_type,
        )
        return _finish_write(client, args, ControlKey(True, args.channel, "limiter"))


def _cmd_set_expander(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_expander(
            args.channel,
            threshold_db=args.threshold,
            start_time_ms=args.start_time,
            release_time_ms=args.release_time,
            ratio=args.ratio,
            enabled=args.enabled,
        )
        return _finish_write(client, args, ControlKey(False, args.channel, "expander"))


def _cmd_set_mix_route(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_mix_route(args.in_index, args.out_index, args.state == "on")
        return _finish_write(client, args, ControlKey(False, args.in_index, "route", args.out_index))


def _cmd_set_input_source(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_input_source(
            args.channel,
            source_select=args.source,
            freq_hz=args.freq,
            gain_db=args.gain,
            enabled=args.state == "on",
        )
        return _finish_write(client, args, ControlKey(False, args.channel, "input_source"))


def _cmd_set_fir(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_fir(
            args.channel,
            enabled=args.state == "on",
            fir_type=args.type,
            win_type=args.win_type,
            freq_low_hz=args.freq_low,
            freq_high_hz=args.freq_high,
            attenuation_db=args.atten,
            order_index=args.order,
            output=args.output,
        )
        return _finish_write(client, args, ControlKey(args.output, args.channel, "fir"))


def _cmd_set_freq_shifter(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.set_freq_shifter(
            args.channel,
            freq_level=args.level,
            direction=args.direction,
            enabled=args.state == "on",
        )
        return _finish_write(client, args, ControlKey(False, args.channel, "frequency_shifter"))


def _cmd_save_program(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.save_program(args.slot)
    return 0


def _cmd_load_program(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.load_program(args.slot)
    return 0


def _cmd_read_fir_coefs(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        data = client.read_fir_coefs(args.channel, output=args.output)
    if not data:
        print(f"error: device returned an empty reply, not overwriting {args.out}", file=sys.stderr)
        return 1
    try:
        validated_fir_coefficients(data)
    except ValueError as exc:
        print(f"error: {exc}; not overwriting {args.out}", file=sys.stderr)
        return 1
    _write_atomic(args.out, data)
    print(f"wrote {len(data)} bytes to {args.out}")
    return 0


def _cmd_write_fir_coefs(args: argparse.Namespace) -> int:
    with open(args.input, "rb") as stream:
        data = stream.read()
    try:
        validated_fir_coefficients(data)
    except ValueError as exc:
        print(f"error: {exc}; got {len(data)} bytes; no packet was sent", file=sys.stderr)
        return 2
    written = False

    def before_write() -> None:
        nonlocal written
        written = True

    with Client(args.ip) as client:
        try:
            restore_fir_coefficients(client, args.channel, data, output=args.output, before_write=before_write)
        except PermissionError as exc:
            print(f"error: {exc}; no FIR write was sent", file=sys.stderr)
            return 1
        except FIRRestoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            stage = "outcome unknown; read the block back before further writes" if written else "nothing written"
            print(f"error: FIR restore failed ({exc}); {stage}", file=sys.stderr)
            return 1
    print(f"restored and verified {len(data)} bytes from {args.input}")
    return 0


def _cmd_channel_copy(args: argparse.Namespace) -> int:
    with Client(args.ip) as client:
        client.channel_copy(args.source, args.target, args.total_channels, output=args.output)
    return 0


def _cmd_diff_group(args: argparse.Namespace) -> int:
    """Exit codes follow diff(1): 0 identical, 1 different, 2 trouble."""
    try:
        with open(args.before, "rb") as f:
            before = f.read()
        with open(args.after, "rb") as f:
            after = f.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    diffs = diff_blobs(before, after)
    if len(before) != len(after):
        print(f"length mismatch: {len(before)} vs {len(after)} bytes "
              "(probably not the same group/model; offsets past the shorter file not compared)")
    elif not diffs:
        print("no differences")
        return 0
    for d in diffs:
        print(f"offset {d.offset}: {d.before:#04x} -> {d.after:#04x}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sinbosen-la548",
        description="Linux control client for the Sinbosen LA548 DSP.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser(
        "search", help="Try CH9120 discovery (known to return no reply on the verified bridge/firmware)"
    )
    p_search.add_argument("--timeout", type=float, default=2.0)
    p_search.add_argument("--source-ip", help="bind discovery to this local IPv4 address")
    p_search.set_defaults(func=_cmd_search)

    p_status = sub.add_parser("status", help="Show program slot 1 name and lock state")
    p_status.add_argument("ip")
    p_status.set_defaults(func=_cmd_status)

    p_dump = sub.add_parser("dump-group", help="Read the active channel group and save it to a file")
    p_dump.add_argument("ip")
    p_dump.add_argument("out")
    p_dump.add_argument("--no-validate", action="store_true", dest="no_validate",
                        help="save the reply even if it does not decode as a DA0408X snapshot")
    p_dump.set_defaults(func=_cmd_dump_group)

    p_mute = sub.add_parser("set-mute", help="Mute or unmute one channel")
    p_mute.add_argument("ip")
    p_mute.add_argument("channel", type=int)
    p_mute.add_argument("state", choices=["mute", "unmute"])
    p_mute.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_mute.set_defaults(func=_cmd_set_mute)

    p_volume = sub.add_parser("set-volume", help="Set one channel's gain in dB")
    p_volume.add_argument("ip")
    p_volume.add_argument("channel", type=int)
    p_volume.add_argument("db", type=float)
    p_volume.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_volume.set_defaults(func=_cmd_set_volume)

    p_phase = sub.add_parser("set-phase", help="Set one channel's polarity")
    p_phase.add_argument("ip")
    p_phase.add_argument("channel", type=int)
    p_phase.add_argument("state", choices=["normal", "invert"])
    p_phase.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_phase.set_defaults(func=_cmd_set_phase)

    p_delay = sub.add_parser("set-delay", help="Set one channel's delay in milliseconds (0-1200)")
    p_delay.add_argument("ip")
    p_delay.add_argument("channel", type=int)
    p_delay.add_argument("ms", type=float)
    p_delay.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_delay.set_defaults(func=_cmd_set_delay)

    p_xover = sub.add_parser(
        "set-xover",
        help="Set a crossover block (output channels only - all fields required, sent as one struct write)",
    )
    p_xover.add_argument("ip")
    p_xover.add_argument("channel", type=int, help="output channel, 0-based - crossover is output-only")
    p_xover.add_argument("band", choices=["hp", "lp"])
    p_xover.add_argument("--freq", type=int, required=True, metavar="HZ", help="10-20000 Hz")
    p_xover.add_argument(
        "--type", type=int, required=True, metavar="N", help="0=Butterworth, 1=Linkwitz-Riley, 2=Bessel"
    )
    p_xover.add_argument("--slope", type=int, required=True, metavar="DB_PER_OCT", help="6/12/18/24/30/36/42/48")
    xover_enable = p_xover.add_mutually_exclusive_group(required=True)
    xover_enable.add_argument("--enable", dest="enabled", action="store_true")
    xover_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_xover.set_defaults(func=_cmd_set_xover)

    p_eq = sub.add_parser(
        "set-eq-band", help="Set an EQ band (all fields required except --gain for AllPass - one struct write)"
    )
    p_eq.add_argument("ip")
    p_eq.add_argument("channel", type=int)
    p_eq.add_argument("band_index", type=int, help="0-based; module_id = 4 + band_index")
    p_eq.add_argument("--freq", type=int, required=True, metavar="HZ", help="10-20000 Hz")
    p_eq.add_argument("--gain", type=float, metavar="DB", help="+-20 dB (optional only for AllPass, types 3 and 4)")
    p_eq.add_argument("--q", type=float, required=True, metavar="Q")
    p_eq.add_argument(
        "--type",
        type=int,
        required=True,
        metavar="N",
        help="0=EQ,1=LowShelf,2=HighShelf,3=AllPass1,4=AllPass2,5=Notch(neg. gain only) - see docs/PROTOCOL.md",
    )
    p_eq.add_argument(
        "--slope", type=int, required=True, metavar="N", help="shelf types only: 0=6dB/oct, 1=12dB/oct"
    )
    eq_enable = p_eq.add_mutually_exclusive_group(required=True)
    eq_enable.add_argument("--enable", dest="enabled", action="store_true")
    eq_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_eq.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_eq.set_defaults(func=_cmd_set_eq_band)

    p_eq_bypass = sub.add_parser(
        "eq-bypass",
        help="Toggle every EQ band on one channel at once (confirmed non-functional on this hardware)",
    )
    p_eq_bypass.add_argument("ip")
    p_eq_bypass.add_argument("channel", type=int)
    p_eq_bypass.add_argument("state", choices=["on", "off"])
    p_eq_bypass.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_eq_bypass.set_defaults(func=_cmd_eq_bypass)

    p_eq_reset = sub.add_parser("eq-reset", help="Reset every EQ band on one channel at once")
    p_eq_reset.add_argument("ip")
    p_eq_reset.add_argument("channel", type=int)
    p_eq_reset.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_eq_reset.set_defaults(func=_cmd_eq_reset)

    p_dyneq = sub.add_parser(
        "set-dynamic-eq-band",
        help="Set an input-only Dynamic EQ band (all fields required - one struct write)",
    )
    p_dyneq.add_argument("ip")
    p_dyneq.add_argument("channel", type=int)
    p_dyneq.add_argument("band_index", type=int, help="0-based; module_id = 35 + band_index")
    p_dyneq.add_argument("--freq", type=int, required=True, metavar="HZ", help="10-20000 Hz")
    p_dyneq.add_argument("--gain", type=float, required=True, metavar="DB", help="+-20 dB")
    p_dyneq.add_argument("--q", type=float, required=True, metavar="Q")
    p_dyneq.add_argument(
        "--type", type=int, required=True, metavar="N",
        help="0=EQ, 1=LowShelf, 2=HighShelf, 3=AllPass1, 4=AllPass2",
    )
    p_dyneq.add_argument("--slope", type=int, required=True, metavar="N", help="0=6dB, 1=12dB")
    p_dyneq.add_argument(
        "--startup-mode", type=int, required=True, metavar="N", dest="startup_mode",
        help="0=trigger above threshold, 1=trigger below threshold",
    )
    p_dyneq.add_argument(
        "--start-time", type=float, required=True, metavar="MS", dest="start_time", help="minimum 0.3 ms"
    )
    p_dyneq.add_argument(
        "--release-time", type=float, required=True, metavar="MS", dest="release_time", help="minimum 50 ms"
    )
    p_dyneq.add_argument("--ratio", type=float, required=True, metavar="N", help="compression ratio, minimum 1.0")
    p_dyneq.add_argument("--threshold", type=float, required=True, metavar="DB", help="minimum -60 dB")
    dyneq_enable = p_dyneq.add_mutually_exclusive_group(required=True)
    dyneq_enable.add_argument("--enable", dest="enabled", action="store_true")
    dyneq_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_dyneq.set_defaults(func=_cmd_set_dynamic_eq_band)

    p_comp = sub.add_parser(
        "set-compressor", help="Set the Compressor block (all fields required - one struct write)"
    )
    p_comp.add_argument("ip")
    p_comp.add_argument("channel", type=int)
    p_comp.add_argument(
        "--start-time", type=float, required=True, metavar="MS", dest="start_time", help="minimum 0.3 ms"
    )
    p_comp.add_argument(
        "--release-time", type=float, required=True, metavar="MS", dest="release_time", help="minimum 50 ms"
    )
    p_comp.add_argument("--ratio", type=float, required=True, metavar="N", help="N:1, minimum 0.0")
    p_comp.add_argument("--threshold", type=float, required=True, metavar="DB", help="minimum -60 dB")
    comp_enable = p_comp.add_mutually_exclusive_group(required=True)
    comp_enable.add_argument("--enable", dest="enabled", action="store_true")
    comp_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_comp.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_comp.set_defaults(func=_cmd_set_compressor)

    p_lim = sub.add_parser(
        "set-limiter",
        help="Set the Limiter block, output-only (all fields required - one struct write)",
    )
    p_lim.add_argument("ip")
    p_lim.add_argument("channel", type=int)
    p_lim.add_argument(
        "--start-time", type=float, required=True, metavar="MS", dest="start_time", help="minimum 0.3 ms"
    )
    p_lim.add_argument(
        "--release-time", type=float, required=True, metavar="MS", dest="release_time", help="minimum 50 ms"
    )
    p_lim.add_argument("--threshold", type=float, required=True, metavar="DB", help="minimum -60 dB")
    p_lim.add_argument(
        "--detection-type", type=int, required=True, metavar="N", dest="detection_type",
        help="0=average-sensitive, 1=peak-sensitive",
    )
    lim_enable = p_lim.add_mutually_exclusive_group(required=True)
    lim_enable.add_argument("--enable", dest="enabled", action="store_true")
    lim_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_lim.set_defaults(func=_cmd_set_limiter)

    p_exp = sub.add_parser(
        "set-expander",
        help="Set the Expander block, input-only (all fields required - one struct write)",
    )
    p_exp.add_argument("ip")
    p_exp.add_argument("channel", type=int)
    p_exp.add_argument(
        "--start-time", type=float, required=True, metavar="MS", dest="start_time", help="minimum 0.3 ms"
    )
    p_exp.add_argument(
        "--release-time", type=float, required=True, metavar="MS", dest="release_time", help="minimum 50 ms"
    )
    p_exp.add_argument("--ratio", type=float, required=True, metavar="N", help="compression ratio, minimum 1.0")
    p_exp.add_argument("--threshold", type=float, required=True, metavar="DB", help="minimum -100 dB")
    exp_enable = p_exp.add_mutually_exclusive_group(required=True)
    exp_enable.add_argument("--enable", dest="enabled", action="store_true")
    exp_enable.add_argument("--disable", dest="enabled", action="store_false")
    p_exp.set_defaults(func=_cmd_set_expander)

    p_mix = sub.add_parser(
        "set-mix-route",
        help="Toggle one input/output crosspoint of the mix matrix ('on' = connected, confirmed live)",
    )
    p_mix.add_argument("ip")
    p_mix.add_argument("in_index", type=int, help="0-based")
    p_mix.add_argument("out_index", type=int, help="0-based")
    p_mix.add_argument("state", choices=["on", "off"])
    p_mix.set_defaults(func=_cmd_set_mix_route)

    p_insrc = sub.add_parser(
        "set-input-source", help="Set the Input Source block, input-only (all fields required, whole struct sent)"
    )
    p_insrc.add_argument("ip")
    p_insrc.add_argument("channel", type=int)
    p_insrc.add_argument("state", choices=["on", "off"])
    p_insrc.add_argument(
        "--source", type=int, required=True, metavar="N",
        help="0=external, 1=sine, 2=pink noise, 3=white noise",
    )
    p_insrc.add_argument("--freq", type=int, required=True, metavar="HZ", help="sine frequency (source 1 only)")
    p_insrc.add_argument("--gain", type=float, required=True, metavar="DB")
    p_insrc.set_defaults(func=_cmd_set_input_source)

    p_fir = sub.add_parser("set-fir", help="Set the FIR block (all fields required, whole struct sent)")
    p_fir.add_argument("ip")
    p_fir.add_argument("channel", type=int)
    p_fir.add_argument("state", choices=["on", "off"])
    p_fir.add_argument(
        "--type", type=int, required=True, metavar="N", help="0=LP,1=HP,2=BP,3=BS,4=AllPass", dest="type"
    )
    p_fir.add_argument(
        "--win-type",
        type=int,
        required=True,
        metavar="N",
        dest="win_type",
        help="0-11, candidate: 0=KAISER,1=HANNING,2=HAMMING,3=BLACKMAN,4=FLATTOP,5=BLACKMAN_HARRIS,... "
        "(see docs/PROTOCOL.md's FIR section - confirmed on hardware to select real, distinct shapes, "
        "names not proven for all 12)",
    )
    p_fir.add_argument("--freq-low", type=int, required=True, metavar="HZ", dest="freq_low")
    p_fir.add_argument("--freq-high", type=int, required=True, metavar="HZ", dest="freq_high")
    p_fir.add_argument("--atten", type=float, required=True, metavar="DB", help="minimum 21.0 dB")
    p_fir.add_argument(
        "--order",
        type=int,
        required=True,
        metavar="N",
        help="tap-count index: 0=256 taps, 1=512 taps, on inputs and outputs (other values are refused) "
        "- confirmed on hardware to affect filter steepness",
    )
    p_fir.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_fir.set_defaults(func=_cmd_set_fir)

    p_fs = sub.add_parser(
        "set-freq-shifter",
        help="Set the Frequency Shifter block, input-only (all fields required, whole struct sent)",
    )
    p_fs.add_argument("ip")
    p_fs.add_argument("channel", type=int)
    p_fs.add_argument("level", type=int, help="index 0-5: approximately 1/2.5/3.5/5/7/9 Hz")
    p_fs.add_argument("direction", type=int, help="0=down, 1=up (both confirmed on hardware)")
    p_fs.add_argument("state", choices=["on", "off"])
    p_fs.set_defaults(func=_cmd_set_freq_shifter)

    p_save = sub.add_parser("save-program", help="Save the device's current live state to a program slot")
    p_save.add_argument("ip")
    p_save.add_argument("slot", type=int, help="0-based")
    p_save.set_defaults(func=_cmd_save_program)

    p_load = sub.add_parser(
        "load-program",
        help="Load a program slot, OVERWRITING ALL of the device's current live settings",
    )
    p_load.add_argument("ip")
    p_load.add_argument("slot", type=int, help="0-based")
    p_load.set_defaults(func=_cmd_load_program)

    p_readfir = sub.add_parser(
        "read-fir-coefs", help="Back up one DA0408X channel's exact 2048-byte FIR coefficient block"
    )
    p_readfir.add_argument("ip")
    p_readfir.add_argument("channel", type=int)
    p_readfir.add_argument("out")
    p_readfir.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_readfir.set_defaults(func=_cmd_read_fir_coefs)

    p_writefir = sub.add_parser(
        "write-fir-coefs",
        help="Restore and verify one channel's exact 2048-byte DA0408X FIR coefficient backup",
    )
    p_writefir.add_argument("ip")
    p_writefir.add_argument("channel", type=int)
    p_writefir.add_argument("input")
    p_writefir.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_writefir.set_defaults(func=_cmd_write_fir_coefs)

    p_chcopy = sub.add_parser("channel-copy", help="Copy one DA0408X channel's full settings onto another")
    p_chcopy.add_argument("ip")
    p_chcopy.add_argument("source", type=int, help="0-based")
    p_chcopy.add_argument("target", type=int, help="0-based")
    p_chcopy.add_argument(
        "total_channels", type=int,
        help="compatibility argument: must be 4 for inputs or 8 with --output on DA0408X",
    )
    p_chcopy.add_argument("--output", action="store_true", help="target an output channel (default: input)")
    p_chcopy.set_defaults(func=_cmd_channel_copy)

    p_diff = sub.add_parser(
        "diff-group",
        help="Diff two dump-group outputs to locate a control's byte offset (exit 0 same, 1 different, 2 error)",
    )
    p_diff.add_argument("before")
    p_diff.add_argument("after")
    p_diff.set_defaults(func=_cmd_diff_group)

    for name, subparser in sub.choices.items():
        if name.startswith("set-"):
            subparser.add_argument("--verify", action="store_true",
                                   help="read settings back and confirm the stored control matches the write")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))
