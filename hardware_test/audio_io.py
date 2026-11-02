"""Simultaneous play + record over the loopback interface, via pw-cat / pw-record subprocesses.

Requires PipeWire (pw-cat / pw-record). Channels are addressed by channel-map
name (`FL`, `AUX0`, ...), the names `pw-cli` and `pactl list sinks` show,
rather than by index as `sounddevice`/PortAudio would.

Playback and capture must share one hardware clock (normally the same
interface on the same PipeWire graph) — no resampling drift between what was
sent and what came back, which is what makes electrical loopback measurement
viable at sub-millisecond precision.

Capture writes straight to a temp file on disk rather than a stdout pipe.
`pw-record`'s output is a continuous real-time stream that, over the length of
a real test signal, vastly exceeds a pipe's kernel buffer (~64KB, ~0.17s of
s32/48kHz/stereo audio); since nothing here reads that pipe until after
playback finishes, `pw-record` blocks in write() almost immediately and the
rest of the signal is silently never captured — a plain Python subprocess
pipe deadlock (https://docs.python.org/3/library/subprocess.html#subprocess.Popen.wait),
not anything wrong with the device. A real file has no such bound.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

BYTES_PER_SAMPLE = 4  # s32le


def _channel_map_arg(channels: tuple[str, ...]) -> str:
    return ",".join(channels)


def to_int32_le(signal: np.ndarray) -> np.ndarray:
    """Scale [-1, 1] float audio to s32le without wraparound. In float32,
    2**31 - 1 rounds up to 2**31, so a full-scale +1.0 sample would cast
    to -2**31 (a full-scale negative spike); scaling and clipping in float64
    keeps it at the positive maximum."""
    scaled = np.clip(np.asarray(signal, dtype=np.float64) * (2**31 - 1), -(2**31), 2**31 - 1)
    return np.asarray(scaled, dtype="<i4")


def _tail(path: Path, limit: int = 500) -> str:
    try:
        return path.read_bytes()[-limit:].decode(errors="replace")
    except OSError:
        return "<no stderr>"


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Terminate, then kill if it ignores SIGTERM; never raises over an
    exception already propagating from the caller."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def play_and_record(
    signal: np.ndarray,
    *,
    sink_name: str,
    source_name: str,
    playback_channels: tuple[str, ...],
    capture_channels: tuple[str, ...],
    sample_rate: int = 48000,
    pre_roll_s: float = 0.5,
    post_roll_s: float = 0.5,
    startup_settle_s: float = 0.3,
) -> np.ndarray:
    """Play `signal` (n_frames, len(playback_channels)) to `sink_name` and capture
    `capture_channels` from `source_name`, returning a float32 array shaped
    (n_frames_captured, len(capture_channels)) in [-1.0, 1.0].

    Capture starts before playback and keeps running `post_roll_s` after it ends,
    so nothing at the start or end of the signal is clipped by pipe startup
    latency. The returned array therefore starts `pre_roll_s` before the signal
    and ends `post_roll_s` after it — callers doing cross-correlation or FFT-ratio
    analysis should account for that fixed offset (test_30 measures every delay
    against its zero-delay step, which cancels it along with the path's own
    fixed latency).
    """
    if signal.ndim != 2:
        raise ValueError(f"signal must be 2-D (n_frames, n_channels), got shape {signal.shape}")
    n_play_channels = signal.shape[1]
    if n_play_channels != len(playback_channels):
        raise ValueError(f"signal has {n_play_channels} channels but playback_channels has {len(playback_channels)}")

    n_capture_channels = len(capture_channels)

    with tempfile.TemporaryDirectory(prefix="la548-hil-") as tmp_dir:
        capture_path = Path(tmp_dir) / "capture.raw"
        capture_cmd = [
            "pw-record",
            f"--target={source_name}",
            f"--channels={n_capture_channels}",
            f"--channel-map={_channel_map_arg(capture_channels)}",
            f"--rate={sample_rate}",
            "--format=s32",
            "--raw",
            str(capture_path),
        ]
        playback_cmd = [
            "pw-cat",
            "--playback",
            f"--target={sink_name}",
            f"--channels={n_play_channels}",
            f"--channel-map={_channel_map_arg(playback_channels)}",
            f"--rate={sample_rate}",
            "--format=s32",
            "--raw",
            "-",
        ]

        capture_err_path = Path(tmp_dir) / "pw-record.err"
        playback_timeout_s = len(signal) / sample_rate + 10.0
        with capture_err_path.open("wb") as capture_err:
            capture_proc = subprocess.Popen(capture_cmd, stderr=capture_err)
            try:
                time.sleep(startup_settle_s)
                time.sleep(pre_roll_s)
                if capture_proc.poll() is not None:
                    raise RuntimeError(
                        f"pw-record exited early with {capture_proc.returncode}: {_tail(capture_err_path)}"
                    )

                playback_proc = subprocess.Popen(playback_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    _, playback_err = playback_proc.communicate(
                        input=to_int32_le(signal).tobytes(), timeout=playback_timeout_s
                    )
                except BaseException:
                    _stop(playback_proc)
                    raise
                if playback_proc.returncode != 0:
                    raise RuntimeError(
                        f"pw-cat playback exited with {playback_proc.returncode}: "
                        f"{playback_err.decode(errors='replace')[-500:]}"
                    )

                time.sleep(post_roll_s)
                if capture_proc.poll() is not None:
                    raise RuntimeError(
                        f"pw-record exited during capture with {capture_proc.returncode}: {_tail(capture_err_path)}"
                    )
            finally:
                _stop(capture_proc)

        raw = capture_path.read_bytes()

    n_complete_frames = len(raw) // (n_capture_channels * BYTES_PER_SAMPLE)
    usable_bytes = n_complete_frames * n_capture_channels * BYTES_PER_SAMPLE
    captured_int32 = np.frombuffer(raw[:usable_bytes], dtype="<i4").reshape(-1, n_capture_channels)
    return (captured_int32.astype(np.float32)) / (2**31)
