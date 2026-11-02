"""Offline checks of hardware_test's measurement and safety helpers, using
synthetic signals only - no audio device and no LA548."""

from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
signal = pytest.importorskip("scipy.signal")

from conftest import FakeTransport  # noqa: E402

from hardware_test import analysis, audio_io  # noqa: E402
from hardware_test.restore import restore_settings  # noqa: E402
from hardware_test.rig import Rig  # noqa: E402
from sinbosen_la548.client import Client  # noqa: E402
from sinbosen_la548.protocol import Opcode  # noqa: E402
from sinbosen_la548.response import eq_response  # noqa: E402
from sinbosen_la548.service import ControlKey, replace_control  # noqa: E402
from sinbosen_la548.settings import EQBandSettings, decode_settings  # noqa: E402

RATE = 48000
FIXTURE = Path(__file__).parent / "fixtures" / "read_group_sample.bin"


def _tone_capture(lead_s: float, tone_s: float, tail_s: float, amplitude: float = 0.2) -> np.ndarray:
    t = np.arange(int(tone_s * RATE)) / RATE
    tone = amplitude * np.sin(2 * np.pi * 1000 * t)
    rng = np.random.default_rng(1)
    capture = np.concatenate([np.zeros(int(lead_s * RATE)), tone, np.zeros(int(tail_s * RATE))])
    return capture + rng.normal(0, 1e-5, len(capture))


def test_onset_aligned_dbfs_reads_the_tone_level_despite_an_asymmetric_lead():
    # play_and_record: ~0.8 s + startup before playback, 0.5 s after.
    capture = _tone_capture(0.83, 1.0, 0.5)
    expected = 20 * np.log10(0.2 / np.sqrt(2))
    assert analysis.onset_aligned_dbfs(capture, RATE, 1.0) == pytest.approx(expected, abs=0.01)
    n = RATE
    centred = analysis.dbfs(capture[(len(capture) - n) // 2 : (len(capture) - n) // 2 + n])
    assert expected - centred > 0.5  # a centred window would include ~165 ms of silence


def test_band_average_refuses_an_empty_band():
    freqs = np.linspace(0, 24000, 1000)
    with pytest.raises(ValueError, match="empty band"):
        analysis.band_average_db(freqs, np.zeros_like(freqs), 12000, 9000)


@pytest.mark.parametrize(("corner", "highpass"), [(200, True), (800, True), (3000, True),
                                                  (300, False), (1500, False), (6000, False)])
def test_every_crossover_test_frequency_gets_a_clear_passband(corner, highpass):
    lo, hi = analysis.passband_beyond_corner(corner, highpass=highpass)
    assert lo < hi
    assert (lo >= 1.5 * corner) if highpass else (hi <= corner / 1.5)


def _butterworth_db(freqs: np.ndarray, corner: float, *, highpass: bool, order: int = 2) -> np.ndarray:
    with np.errstate(divide="ignore"):
        ratio = (freqs / corner) if highpass else (corner / np.maximum(freqs, 1e-9))
        power = ratio ** (2 * order) / (1 + ratio ** (2 * order))
        return 10 * np.log10(np.maximum(power, 1e-30))


def test_corner_frequency_finds_a_3000hz_highpass():
    freqs = np.fft.rfftfreq(4 * RATE, d=1 / RATE)
    mag = _butterworth_db(freqs, 3000, highpass=True) + np.random.default_rng(2).normal(0, 0.2, len(freqs))
    passband = analysis.passband_beyond_corner(3000, highpass=True)
    corner = analysis.corner_frequency(freqs, mag, highpass=True, passband=passband)
    assert abs(np.log2(corner / 3000)) < 0.1


def test_lowpass_corner_ignores_the_rig_rolloff_below_20hz():
    # A parked 10 Hz HP plus the loopback's own AC coupling: a plain ascending
    # search would stop at the first sub-20 Hz bin already 3 dB down.
    freqs = np.fft.rfftfreq(4 * RATE, d=1 / RATE)
    mag = _butterworth_db(freqs, 300, highpass=False) + _butterworth_db(freqs, 15, highpass=True, order=1)
    passband = analysis.passband_beyond_corner(300, highpass=False)
    corner = analysis.corner_frequency(freqs, mag, highpass=False, passband=passband, search_hz=(20, 20000))
    assert abs(np.log2(corner / 300)) < 0.1


def _allpass_coefficients(allpass_type: int, center: float) -> tuple[list[float], list[float]]:
    omega = 2 * np.pi * center / RATE
    c, s = np.cos(omega), np.sin(omega)
    if allpass_type == 3:
        t = s / (1 + c)
        a1 = (t - 1) / (t + 1)
        return [a1, 1.0], [1.0, a1]
    alpha = s / 2  # Q = 1
    return [1 - alpha, -2 * c, 1 + alpha], [1 + alpha, -2 * c, 1 - alpha]


def _two_channel_capture(noise: np.ndarray, start_offset: int, *, processed=None) -> np.ndarray:
    test_path = np.concatenate([np.zeros(37), noise])[: len(noise)]  # 37-sample path latency
    reference_path = np.concatenate([np.zeros(12), noise])[: len(noise)] * 0.9  # different latency and gain
    if processed is not None:
        test_path = signal.lfilter(*processed, test_path)
    lead = np.zeros((start_offset, 2))
    return np.vstack([lead, np.column_stack([test_path, reference_path])])


@pytest.mark.parametrize(("allpass_type", "center"), [(4, 1000.0), (4, 1500.0), (3, 1000.0)])
def test_relative_response_reads_allpass_phase_off_the_0_180_grid(allpass_type, center):
    noise = np.random.default_rng(3).uniform(-0.2, 0.2, 4 * RATE)
    baseline = _two_channel_capture(noise, 900)
    processed = _two_channel_capture(noise, 2400, processed=_allpass_coefficients(allpass_type, center))
    at_hz = (center / 2, center, center * 2)
    measured = analysis.relative_response(baseline[: 4 * RATE], processed[: 4 * RATE], RATE, at_hz)
    band = EQBandSettings(int(center), 0.0, 1.0, 0, allpass_type, True, raw=b"")
    model = np.degrees(np.angle(eq_response(band, np.asarray(at_hz), sample_rate_hz=RATE).transfer))
    for (phase, level), want in zip(measured, model, strict=True):
        assert abs((phase - want + 180) % 360 - 180) < 2.0
        assert abs(level) < 0.1
    if allpass_type == 3:
        assert measured[1][0] == pytest.approx(-90, abs=2)  # unreachable for a pure-tone method


def test_full_scale_playback_samples_do_not_wrap():
    converted = audio_io.to_int32_le(np.array([[1.0, -1.0], [1.5, -1.5], [0.5, 0.0]], dtype=np.float32))
    assert converted[0, 0] == 2**31 - 1 and converted[0, 1] == -(2**31 - 1)
    assert converted[1, 0] == 2**31 - 1 and converted[1, 1] == -(2**31)
    assert converted[2, 0] > 0


def test_rig_plays_extra_configured_playback_channels_silent(monkeypatch):
    played = {}

    def fake_play_and_record(signal, **kwargs):
        played["signal"], played["kwargs"] = signal, kwargs
        return np.zeros((10, len(kwargs["capture_channels"])), dtype=np.float32)

    monkeypatch.setattr(audio_io, "play_and_record", fake_play_and_record)
    rig = Rig(None, sink_name="sink", source_name="source",  # type: ignore[arg-type]
              playback_channels=("AUX0", "AUX1", "AUX2"), capture_channels=("AUX4", "AUX5"), sample_rate=44100)
    stereo = np.ones((5, 2), dtype=np.float32)
    rig.measure(stereo)
    assert played["signal"].shape == (5, 3)
    assert np.array_equal(played["signal"][:, :2], stereo) and not played["signal"][:, 2].any()
    assert played["kwargs"]["playback_channels"] == ("AUX0", "AUX1", "AUX2")
    assert played["kwargs"]["sample_rate"] == 44100


def _client_reading(snapshots: list) -> tuple[Client, FakeTransport]:
    transport = FakeTransport()
    client = Client.__new__(Client)
    client._transport = transport  # type: ignore[assignment]
    client.read_settings = lambda **kwargs: snapshots.pop(0)  # type: ignore[method-assign]
    return client, transport


def test_restore_rewrites_changed_controls_including_out_of_range_factory_blocks():
    baseline = decode_settings(FIXTURE.read_bytes())
    out = next(ch for ch in baseline.outputs if ch.compressor.threshold_db > 20)
    factory = out.compressor  # disabled factory block, outside the encodable range
    delay_key = ControlKey(True, out.channel_id, "delay")
    compressor_key = ControlKey(True, out.channel_id, "compressor")
    changed = replace_control(baseline, delay_key, 5.0)
    changed = replace_control(changed, compressor_key, replace(factory, threshold_db=-20.0, ratio=4.0,
                                                                  enabled=True))
    client, transport = _client_reading([changed, baseline])

    assert set(restore_settings(client, baseline)) == {delay_key, compressor_key}

    sent = {(call[2]["module_id"], call[1]) for call in transport.calls if call[0] == Opcode.WRITE_MODULE_ID}
    assert (3, (0).to_bytes(2, "little")) in sent  # delay back to 0 ms
    assert (40, factory.raw) in sent  # the exact factory bytes, not a re-encoding


def test_restore_reports_controls_that_did_not_come_back():
    baseline = decode_settings(FIXTURE.read_bytes())
    changed = replace_control(baseline, ControlKey(True, 0, "delay"), 5.0)
    client, _ = _client_reading([changed, changed])
    with pytest.raises(RuntimeError, match="Output 1: delay"):
        restore_settings(client, baseline)
