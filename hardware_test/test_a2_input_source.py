"""Live confirmation of set_input_source (module_id=43, input-only).

`source_select` chooses what feeds the input channel, confirmed on hardware:

- `source_select=0`: external analog input - the device's factory default,
  which every other test in this directory depends on. `enabled`/`gain_db`
  have no effect on it at all.
- `source_select=1`: internal sine generator - `freq_hz` sets its frequency
  (and has no effect on any other source). With `freq_hz=0` it is silent
  (~-108 dBFS), which is easy to misread as an unwired source.
- `source_select=2`: internal pink noise generator.
- `source_select=3`: internal white noise generator (`WORKING_SOURCE_SELECT`
  below).

Selecting 1/2/3 replaces the external signal with the internally generated
one, and `enabled`/`gain_db` gate and trim that generator (enabled=False
drops ~35-45 dB; gain_db tracks a real dB delta closely).

Every test below restores `source_select=0` at the end (and the `rig`
fixture's snapshot restore backs that up) - **never leave the device on a
different source_select**, since every other test depends on source 0's
transparent passthrough.
"""

from __future__ import annotations

import time

from . import analysis, signal_gen
from .rig import Rig

CHANNEL = 0
FREQ_HZ = 1000
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.2
WORKING_SOURCE_SELECT = 3  # confirmed live: enabled/gain_db have a real effect here (internal white noise)
SINE_SOURCE_SELECT = 1  # internal sine generator; freq_hz sets its pitch
SINE_TEST_FREQ_HZ = 2000  # deliberately != FREQ_HZ, so the generator's tone is distinguishable from playback
DEFAULT_SOURCE_SELECT = 0  # the device's factory default: transparent external-input passthrough
BOOST_DB = 10.0  # inside the manufacturer's app's -80..+12 dB input-source gain range (codecs.VOLUME_DB)


def _dbfs(rig: Rig) -> float:
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    captured = rig.measure(tone)
    return analysis.onset_aligned_dbfs(captured[:, CHANNEL], rig.sample_rate, TONE_DURATION_S)


def _restore_default(rig: Rig) -> None:
    rig.client.set_input_source(
        CHANNEL, source_select=DEFAULT_SOURCE_SELECT, freq_hz=0, gain_db=0.0, enabled=True
    )
    time.sleep(rig.settle_s)


def test_a2_source_select_changes_which_signal_reaches_the_channel(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_input_source(CHANNEL, source_select=DEFAULT_SOURCE_SELECT, freq_hz=0, gain_db=0.0, enabled=True)
    time.sleep(rig.settle_s)
    tone = signal_gen.sine(FREQ_HZ, TONE_DURATION_S, amplitude=TONE_AMPLITUDE, sample_rate=rig.sample_rate)
    default_captured = rig.measure(tone)
    n = int(rig.sample_rate * TONE_DURATION_S)
    mid_start = (len(default_captured) - n) // 2
    default_peak_hz = analysis.peak_frequency(default_captured[mid_start : mid_start + n, CHANNEL], rig.sample_rate)

    rig.client.set_input_source(
        CHANNEL, source_select=SINE_SOURCE_SELECT, freq_hz=SINE_TEST_FREQ_HZ, gain_db=0.0, enabled=True
    )
    time.sleep(rig.settle_s)
    sine_captured = rig.measure(tone)
    sine_peak_hz = analysis.peak_frequency(sine_captured[mid_start : mid_start + n, CHANNEL], rig.sample_rate)

    _restore_default(rig)
    print(f"source_select={DEFAULT_SOURCE_SELECT} peak: {default_peak_hz:.2f} Hz, "
          f"source_select={SINE_SOURCE_SELECT} (freq_hz={SINE_TEST_FREQ_HZ}) peak: {sine_peak_hz:.2f} Hz")
    assert abs(default_peak_hz - FREQ_HZ) < 20.0, (
        f"expected the default source to pass the {FREQ_HZ} Hz playback tone through, measured peak "
        f"{default_peak_hz:.2f} Hz"
    )
    assert abs(sine_peak_hz - SINE_TEST_FREQ_HZ) < 20.0, (
        f"expected source_select={SINE_SOURCE_SELECT} to substitute its own internal "
        f"{SINE_TEST_FREQ_HZ} Hz tone regardless of what's being played back, measured peak {sine_peak_hz:.2f} Hz"
    )
    print("CONFIRMED: source_select=1 is an internal sine generator whose freq_hz sets its pitch, "
          "independent of external playback")


def test_a2_enable_toggles_signal_presence_on_a_working_source(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_input_source(CHANNEL, source_select=WORKING_SOURCE_SELECT, freq_hz=0, gain_db=0.0, enabled=True)
    time.sleep(rig.settle_s)
    enabled_db = _dbfs(rig)

    rig.client.set_input_source(CHANNEL, source_select=WORKING_SOURCE_SELECT, freq_hz=0, gain_db=0.0, enabled=False)
    time.sleep(rig.settle_s)
    disabled_db = _dbfs(rig)

    _restore_default(rig)
    delta = enabled_db - disabled_db
    print(f"source_select={WORKING_SOURCE_SELECT}: enabled={enabled_db:.2f} dBFS, disabled={disabled_db:.2f} dBFS")
    assert delta > 20.0, f"expected disabling to clearly drop the signal, only saw a {delta:.2f} dB difference"
    print("CONFIRMED: enabled=False silences a working source, not inverted")


def test_a2_gain_tracks_configured_db_on_a_working_source(rig: Rig) -> None:
    rig.reset_both_channels_safe()

    rig.client.set_input_source(CHANNEL, source_select=WORKING_SOURCE_SELECT, freq_hz=0, gain_db=0.0, enabled=True)
    time.sleep(rig.settle_s)
    baseline_db = _dbfs(rig)

    rig.client.set_input_source(CHANNEL, source_select=WORKING_SOURCE_SELECT, freq_hz=0, gain_db=BOOST_DB,
                                enabled=True)
    time.sleep(rig.settle_s)
    boosted_db = _dbfs(rig)

    _restore_default(rig)
    delta = boosted_db - baseline_db
    print(f"source_select={WORKING_SOURCE_SELECT}: 0dB={baseline_db:.2f} dBFS, +{BOOST_DB:g}dB={boosted_db:.2f} dBFS")
    assert abs(delta - BOOST_DB) < 2.0, (
        f"expected +{BOOST_DB:g} dB gain to track configured value, measured delta {delta:.2f} dB"
    )
    print("CONFIRMED: gain_db tracks configured dB on a working source")
