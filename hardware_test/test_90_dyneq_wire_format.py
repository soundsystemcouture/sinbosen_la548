"""Live confirmation that set_dynamic_eq_band's whole-struct write reaches
the device.

Like crossover/EQ (test_40/test_50), a Dynamic EQ band is written as one
frame carrying the whole band struct (freq/gain/q/slope/type/mode/threshold/
start time/release time/ratio/enable, in that order), `data_id=0`, rather
than one frame per field. There is no "enable every band" call in the
client; enable bands individually with `set_dynamic_eq_band`.

An ACK doesn't prove a write applied, so this checks a `read_group()` byte
diff before and after a deliberately full field change.

This test only checks that the write reaches storage, not that it's
audible - see `test_91_dyneq_audible_effect.py` for that. On hardware this
write once reached storage correctly while producing zero audible change,
until a power cycle fixed it: the device is sensitive to malformed packets,
which can wedge a DSP processing block while storage and ACKs keep working.
"""

from __future__ import annotations

from .rig import Rig

INPUT_CHANNEL = 0
BAND_INDEX = 0

_STATE_A: dict[str, float | int | bool] = dict(
    freq_hz=500,
    gain_db=-15.0,
    qvalue=0.5,
    slope=0,
    type=0,
    startup_mode=0,
    threshold_db=-60.0,
    start_time_ms=0.3,
    release_time_ms=50.0,
    ratio=1.0,
    enabled=False,
)
_STATE_B: dict[str, float | int | bool] = dict(
    freq_hz=8000,
    gain_db=15.0,
    qvalue=3.0,
    slope=1,
    type=2,
    startup_mode=1,
    threshold_db=0.0,
    start_time_ms=20.0,
    release_time_ms=200.0,
    ratio=10.0,
    enabled=True,
)
MIN_EXPECTED_DIFFERING_BYTES = 10  # generous vs. the struct's 18 bytes - a few high bytes can coincidentally match


def test_90_set_dynamic_eq_band_whole_struct_write_reaches_device(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_dynamic_eq_band(INPUT_CHANNEL, BAND_INDEX, **_STATE_A)  # type: ignore[arg-type]
    blob_a = rig.client.read_group()

    rig.client.set_dynamic_eq_band(INPUT_CHANNEL, BAND_INDEX, **_STATE_B)  # type: ignore[arg-type]
    blob_b = rig.client.read_group()

    assert len(blob_a) == len(blob_b), "read_group() blob length changed between reads - unexpected"
    differing = sum(1 for a, b in zip(blob_a, blob_b, strict=True) if a != b)
    print(f"read_group() blob: {differing} bytes differ after writing a fully-changed Dynamic EQ band 0 struct")
    assert differing >= MIN_EXPECTED_DIFFERING_BYTES, (
        f"only {differing} bytes changed - expected at least {MIN_EXPECTED_DIFFERING_BYTES} "
        "for a write touching all 11 struct fields; the whole-struct write may not be reaching the device"
    )

    rig.client.set_dynamic_eq_band(
        INPUT_CHANNEL,
        BAND_INDEX,
        freq_hz=1000,
        gain_db=0.0,
        qvalue=1.0,
        slope=0,
        type=0,
        startup_mode=0,
        threshold_db=-60.0,
        start_time_ms=0.3,
        release_time_ms=50.0,
        ratio=1.0,
        enabled=False,
    )
    print("CONFIRMED: set_dynamic_eq_band's whole-struct write reaches the device's stored parameter state")
