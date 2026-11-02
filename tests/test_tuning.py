from dataclasses import replace
from pathlib import Path

import pytest

from sinbosen_la548.service import ControlKey, DSPService
from sinbosen_la548.settings import decode_settings
from sinbosen_la548.tuning import TuningController


@pytest.fixture
def tuning():
    snapshot = decode_settings((Path(__file__).parent / "fixtures" / "read_group_sample.bin").read_bytes())
    service = DSPService(snapshot)
    return TuningController(service), service.new_session()


def test_allpass_uses_disabled_slot_and_quantized_q(tuning):
    controller, token = tuning
    assert controller.add_allpass(token, True, 0) == 0
    assert controller.add_allpass(token, True, 0) == 1
    ch = controller.channel(token, True, 0)
    assert ch.eq_bands[0].type == 4
    assert ch.eq_bands[0].gain_db == 0
    assert ch.eq_bands[0].freq_hz == 80
    assert ch.eq_bands[0].q == pytest.approx(0.9988262647420053)
    assert ch.eq_bands[0].enabled


def test_no_slot_reports_error_without_overwriting_filters(tuning):
    controller, token = tuning
    for _ in range(7):
        controller.add_allpass(token, False, 0)
    before = controller.channel(token, False, 0)
    with pytest.raises(ValueError, match="no disabled EQ slot"):
        controller.add_allpass(token, False, 0)
    assert controller.channel(token, False, 0) == before


def test_allpass_allocation_cannot_overwrite_a_temporarily_bypassed_slot(tuning):
    controller, token = tuning
    controller.add_allpass(token, True, 0)
    controller.bypass(token, True, 0)
    with pytest.raises(ValueError, match="restore EQ"):
        controller.add_allpass(token, True, 0)
    controller.bypass(token, True, 0, restore=True)
    assert controller.channel(token, True, 0).eq_bands[0].enabled
    controller.bypass(token, True, 0)
    controller.reset(token)
    with pytest.raises(ValueError, match="no saved EQ"):
        controller.bypass(token, True, 0, restore=True)


@pytest.mark.parametrize("allpass_type", [3, 4])
def test_allpass_type_change_zeroes_gain_but_toggle_preserves_fields(tuning, allpass_type):
    controller, token = tuning
    controller.eq(token, True, 0, 0, "gain_db", 12)
    controller.eq(token, True, 0, 0, "type", allpass_type)
    band = controller.channel(token, True, 0).eq_bands[0]
    assert band.gain_db == 0
    with pytest.raises(ValueError, match="allpass gain"):
        controller.eq(token, True, 0, 0, "gain_db", 3)
    controller.eq(token, True, 0, 0, "enabled", True)
    assert controller.channel(token, True, 0).eq_bands[0].gain_db == 0


def test_bypass_restore_preserves_frequency_gain_q_type_and_original_mask(tuning):
    controller, token = tuning
    controller.add_allpass(token, False, 0)
    controller.eq(token, False, 0, 2, "enabled", True)
    before = controller.channel(token, False, 0).eq_bands
    controller.bypass(token, False, 0)
    controller.bypass(token, False, 0)  # Repeated bypass must not replace the original mask.
    assert not any(b.enabled for b in controller.channel(token, False, 0).eq_bands)
    controller.bypass(token, False, 0, restore=True)
    assert controller.channel(token, False, 0).eq_bands == before


def test_bypass_mask_is_per_channel(tuning):
    controller, token = tuning
    controller.add_allpass(token, True, 0)
    controller.bypass(token, True, 0)
    with pytest.raises(ValueError, match="no saved"):
        controller.bypass(token, True, 1, restore=True)
    controller.bypass(token, True, 0, restore=True)
    assert controller.channel(token, True, 0).eq_bands[0].enabled


def test_crossover_controls_map_to_full_block(tuning):
    controller, token = tuning
    controller.crossover(token, 0, "hp", "filter_type", 1)
    controller.crossover(token, 0, "hp", "slope_db_per_oct", 24)
    controller.crossover(token, 0, "hp", "freq_hz", 80)
    controller.crossover(token, 0, "hp", "enabled", True)
    ch = controller.channel(token, True, 0)
    assert (ch.hp.freq_hz, ch.hp.filter_type, ch.hp.slope_db_per_oct, ch.hp.enabled) == (80, 1, 24, True)
    assert not ch.lp.enabled and ch.lp.freq_hz == 20000
    controller.crossover(token, 0, "hp", "slope_db_per_oct", 18)
    assert controller.channel(token, True, 0).hp.slope_db_per_oct == 18


def test_channel_switching_preserves_independent_drafts(tuning):
    controller, token = tuning
    controller.add_allpass(token, True, 0)
    controller.add_allpass(token, False, 0)
    controller.eq(token, True, 0, 0, "freq_hz", 90)
    controller.eq(token, False, 0, 0, "freq_hz", 100)
    assert controller.channel(token, True, 0).eq_bands[0].freq_hz == 90
    assert controller.channel(token, False, 0).eq_bands[0].freq_hz == 100


def test_static_eq_rejects_unsupported_slope(tuning):
    controller, token = tuning
    key = ControlKey(True, 0, "eq", 0)
    band = controller.channel(token, True, 0).eq_bands[0]
    with pytest.raises(ValueError, match="slope must be 0 or 1"):
        controller.service.stage(token, key, replace(band, type=4, gain_db=-3, slope=3))


def test_unresolved_types_are_not_new_filter_choices(tuning):
    # All wire-legal EQ types (0-5) are now named/resolved (see EQ_TYPES);
    # only an out-of-wire-range value can exercise this guard now.
    controller, token = tuning
    with pytest.raises(ValueError, match="unresolved"):
        controller.eq(token, True, 0, 0, "type", 99)
