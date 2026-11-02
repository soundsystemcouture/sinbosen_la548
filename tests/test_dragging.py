from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from sinbosen_la548.client import Client, LockState
from sinbosen_la548.dragging import DragEdits, drag_frequency, frequency_at_position, pending_label
from sinbosen_la548.service import ControlKey, DSPService, normalize_control
from sinbosen_la548.settings import decode_settings


@pytest.fixture
def graph():
    settings = decode_settings((Path(__file__).parent / "fixtures/read_group_sample.bin").read_bytes())
    client = Mock(spec=Client)
    client.link_up = True
    client.read_settings.return_value = settings
    client.read_lock_state.return_value = LockState(False, "")
    service = DSPService(settings, client_factory=lambda _: client)
    session = service.new_session()
    yield service, session, DragEdits(service), client
    service.close()


def test_logarithmic_mapping_and_clamps():
    assert frequency_at_position(0.5, 10, 1000) == 100
    assert frequency_at_position(-1, 10, 300) == 10
    assert frequency_at_position(2, 10, 300) == 300
    assert drag_frequency(100.5) == 101
    assert drag_frequency(99999) == 20000
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            drag_frequency(value)
    with pytest.raises(ValueError):
        frequency_at_position(0.5, 0, 100)


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4, 5])
def test_preview_and_finish_preserve_other_band_parameters(graph, filter_type):
    service, session, edits, client = graph
    key = ControlKey(True, 0, "eq", 0)
    original = service.view(session).draft.outputs[0].eq_bands[0]
    service.stage(session, key, replace(original, type=filter_type, enabled=True, q=1.5))
    before = service.view(session)
    context = edits.begin(session, "out:0", 0, (True, True, False, False))
    _, _, preview = edits.preview(session, context.id, 333.4)
    assert service.view(session) == before
    assert preview.eq_bands[0].freq_hz == 333
    edits.finish(session, context.id, 333.4)
    band = service.view(session).draft.outputs[0].eq_bands[0]
    assert band == normalize_control(key, replace(before.draft.outputs[0].eq_bands[0], freq_hz=333))
    client.set_eq_band.assert_not_called()


def test_live_pauses_and_disabled_band_stays_disabled(graph):
    service, session, edits, client = graph
    service.connect(session, "fake")
    service.set_live(session, True)
    context = edits.begin(session, "out:0", 0, (True, True, False, False))
    assert not service.view(session).live
    edits.finish(session, context.id, 200)
    assert not service.view(session).draft.outputs[0].eq_bands[0].enabled
    assert service.flush_live(session) is None
    client.set_eq_band.assert_not_called()


@pytest.mark.parametrize("action", ["cancel", "revert", "edit", "channel", "disconnect"])
def test_cancel_and_changed_contexts_cannot_commit(graph, action):
    service, session, edits, _ = graph
    service.connect(session, "fake")
    context = edits.begin(session, "out:0", 0, (True, True, False, False))
    before = service.view(session).draft.outputs[0].eq_bands[0]
    if action == "cancel":
        edits.cancel(session)
    elif action == "revert":
        service.revert(session)
    elif action == "edit":
        service.stage(session, ControlKey(True, 1, "delay"), 2)
    elif action == "channel":
        assert edits.current(session, "out:1") is None
    else:
        service.close()
    with pytest.raises(ValueError, match="expired"):
        edits.finish(session, context.id, 200)
    assert service.view(session).draft.outputs[0].eq_bands[0] == before


def test_drag_on_a_band_other_than_the_selected_one_survives_a_stale_band_lookup(graph):
    # Regression: UI events carry the eq_index value from before the drag
    # selected its band; a lookup with that stale index used to expire it.
    service, session, edits, _ = graph
    view = service.view(session)
    band = next(i for i, b in enumerate(view.draft.outputs[0].eq_bands) if i > 0 and b.type in (0, 1, 2))
    context = edits.begin(session, "out:0", band, (False, False, False, False))
    assert edits.current(session, "out:0") == context
    # A late selection event carrying the pre-drag selection is not a new choice.
    assert not edits.is_new_selection(context, band)
    edits.finish(session, context.id, 250)
    assert service.view(session).draft.outputs[0].eq_bands[band].freq_hz == 250
    assert edits.current(session, "out:0") is None


def test_only_a_different_band_choice_counts_as_a_new_selection(graph):
    service, session, edits, _ = graph
    context = edits.begin(session, "out:0", 2, (False, False, False, False), selected=0)
    assert not edits.is_new_selection(context, 0)  # stale pre-drag selection
    assert not edits.is_new_selection(context, 2)  # the dragged band itself
    assert edits.is_new_selection(context, 1)


def test_unknown_filter_and_stale_atomic_stage_are_rejected():
    # All wire-legal EQ types (0-5) are now recognized, so an "unknown filter"
    # can no longer arise from a normal stage() call - it can only come from
    # a corrupted/anomalous device readback. Bake that directly into the
    # initial settings, bypassing the codec's own 0-5 range validation.
    settings = decode_settings((Path(__file__).parent / "fixtures/read_group_sample.bin").read_bytes())
    corrupted = replace(settings.outputs[0].eq_bands[0], type=99)
    bands = list(settings.outputs[0].eq_bands)
    bands[0] = corrupted
    outputs = list(settings.outputs)
    outputs[0] = replace(settings.outputs[0], eq_bands=tuple(bands))
    settings = replace(settings, outputs=tuple(outputs))

    client = Mock(spec=Client)
    client.link_up = True
    client.read_settings.return_value = settings
    client.read_lock_state.return_value = LockState(False, "")
    service = DSPService(settings, client_factory=lambda _: client)
    session = service.new_session()
    edits = DragEdits(service)
    key = ControlKey(True, 0, "eq", 0)
    try:
        with pytest.raises(ValueError, match="cannot be dragged"):
            edits.begin(session, "out:0", 0, (False, False, False, False))
        service.stage(session, ControlKey(True, 1, "delay"), 2)
        with pytest.raises(ValueError, match="draft changed"):
            service.stage(session, key, corrupted, expected_revision=0)
    finally:
        service.close()


def test_pending_banner_uses_all_channels_and_baseline_differences(graph):
    service, session, _, _ = graph
    assert "No unapplied changes" in pending_label(service.view(session))
    service.stage(session, ControlKey(True, 0, "delay"), 2)
    service.stage(session, ControlKey(True, 1, "delay"), 3)
    assert "2 controls across 2 channels" in pending_label(service.view(session))
    service.stage(session, ControlKey(True, 1, "delay"), 0)
    label = pending_label(service.view(session), unfinished=True)
    assert "1 controls across 1 channels" in label
    assert "Offline demo" in label and "Unfinished input" in label
    service.revert(session)
    assert "No unapplied changes" in pending_label(service.view(session))


def test_drag_identifiers_are_session_scoped_and_superseded(graph):
    service, session, edits, _ = graph
    other = service.new_session()
    first = edits.begin(session, "out:0", 0, (False, False, False, False))
    edits.begin(other, "out:0", 0, (False, False, False, False))
    with pytest.raises(ValueError, match="expired"):
        edits.preview(other, first.id, 200)
    latest = edits.begin(session, "out:0", 0, (False, False, False, False))
    with pytest.raises(ValueError, match="expired"):
        edits.finish(session, first.id, 200)
    assert edits.current(session, "out:0") == latest
    assert service.view(session).pending == ()


def test_errors_and_disconnect_do_not_clear_pending_banner(graph):
    service, session, _, _ = graph
    service.connect(session, "fake")
    service.stage(session, ControlKey(True, 1, "gain"), 1)
    label = pending_label(service.view(session))
    service.report_error(session, TimeoutError("readback unavailable"))
    assert pending_label(service.view(session)) == label
    service.close()
    assert pending_label(service.view(session)) == label
