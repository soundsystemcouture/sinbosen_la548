import asyncio
from functools import partial

import pytest

pytest.importorskip("gradio")
pytest.importorskip("scipy")

from gradio.state_holder import SessionState  # noqa: E402

from sinbosen_la548.ui import create_app  # noqa: E402


def name(fn):
    return fn.func.__name__ if isinstance(fn, partial) else fn.__name__


def test_offline_app_renders_and_edits_without_sockets():
    app = create_app()
    session = app.dsp_service.new_session()
    state = SessionState(app)
    token_id = next(c["id"] for c in app.config["components"] if c["type"] == "state")
    state[token_id] = session
    fn_id = next(i for i, fn in app.fns.items() if name(fn.fn) == "render")
    inputs = [None, "out:0", True, True, False, False, 0]
    result = asyncio.run(app.process_api(fn_id, inputs, state=state))
    assert "Offline demo" in result["data"][0]
    assert "Magnitude" in str(result["data"][3])
    edit_id = next(i for i, fn in app.fns.items() if isinstance(fn.fn, partial)
                   and name(fn.fn) == "commit_numeric" and fn.fn.args == ("delay",))
    asyncio.run(app.process_api(edit_id, inputs + [2.345], state=state))
    assert app.dsp_service.view(session).draft.outputs[0].base.delay_ms == 2.34
    assert app.dsp_service.view(session).baseline.outputs[0].base.delay_ms == 0
    app.dsp_service.close()


def test_input_channel_hides_crossovers_and_bounds_band_selection():
    app = create_app()
    state = SessionState(app)
    token_id = next(c["id"] for c in app.config["components"] if c["type"] == "state")
    state[token_id] = app.dsp_service.new_session()
    fn_id = next(i for i, fn in app.fns.items() if name(fn.fn) == "render")
    result = asyncio.run(app.process_api(fn_id, [None, "in:0", True, True, False, False, 14], state=state))
    assert result["data"][11]["visible"] is False
    assert result["data"][12]["value"] == 6
    assert len(result["data"][13]["data"]) == 7
    app.dsp_service.close()


def callback(app, function_name):
    return next(fn.fn for fn in app.fns.values() if name(fn.fn) == function_name)


def test_graph_edits_stage_frequency_and_preserve_unrelated_numeric_errors():
    import json

    import gradio as gr

    app = create_app()
    try:
        session = app.dsp_service.new_session()
        inputs = [session, "out:0", True, True, False, False, 0]
        for field, value in [("eq_freq", 3), ("eq_q", -1)]:
            commit = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                          and name(fn.fn) == "commit_numeric" and fn.fn.args == (field,))
            commit(*inputs, value)
        event_data = gr.EventData(None, {"action": "begin", "band": 0, "request": "unit-drag",
                                        "choice": "out:0", "revision": 0})
        started = callback(app, "begin_drag")(*inputs, event_data)
        assert all(component.elem_id != "response-main" for component in started)
        metadata = json.loads(next(value for component, value in started.items()
                                   if component.elem_id == "eq-drag-controller"))
        assert metadata["active"]
        before = app.dsp_service.view(session)
        controller = next(component for component in app.blocks.values()
                          if getattr(component, "elem_id", "") == "eq-drag-controller")
        preview = controller.preview_drag({"session": session, "id": metadata["active"],
                                           "frequency": 321, "sequence": 1})
        assert preview["sequence"] == 1 and "figure" in preview
        assert app.dsp_service.view(session) == before
        finished = callback(app, "finish_drag")(*inputs, gr.EventData(None, {
            "action": "finish", "id": metadata["active"], "band": 0, "frequency": 321,
        }))
        view = app.dsp_service.view(session)
        assert len(view.pending) == 1 and view.draft.outputs[0].eq_bands[0].freq_hz == 321
        assert not view.draft.outputs[0].eq_bands[0].enabled
        assert next(value for component, value in finished.items()
                    if component.elem_id == "eq-frequency-error")["visible"] is False
        assert next(value for component, value in finished.items()
                    if component.elem_id == "eq-q-error")["visible"] is True
        banner = next(value for component, value in finished.items() if component.elem_id == "draft-status")
        assert "Unapplied changes: 1 controls" in banner and "Unfinished input" in banner
        assert next(value for component, value in finished.items()
                    if component.elem_id == "response-main").layout.datarevision
    finally:
        app.dsp_service.close()


def test_measured_overlay_and_alignment_are_local(tmp_path):
    app = create_app()
    token = app.dsp_service.new_session()
    inputs = [token, "out:0", False, True, False, False, 0]
    path = tmp_path / "measurement.csv"
    path.write_text("100,0,0,1\n200,0,-90,1\n300,0,-180,1\n")
    result = callback(app, "import_trace")(*inputs, "all", str(path))
    assert any("measurement · measured" in str(value) for value in result.values())
    comparison = callback(app, "alignment")(token, 0, 1, 2, False, True)
    assert any("Electrical sum" in str(value) for value in comparison.values())
    callback(app, "stage_alignment")(*inputs, 0, 1, 2, 1.25, True, 2.5, False)
    view = app.dsp_service.view(token)
    assert view.draft.outputs[0].base.delay_ms == 1.25
    assert view.draft.outputs[0].base.inverted
    assert view.draft.outputs[1].base.delay_ms == 2.5
    assert view.baseline.outputs[0].base.delay_ms == 0
    app.dsp_service.close()


def test_trace_import_uses_the_show_on_choice_not_the_selected_input(tmp_path):
    # Regression: the import used the selected channel ("in:0"), which is not
    # one of the "Show on" dropdown's choices.
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "in:0", False, True, False, False, 0]
        path = tmp_path / "trace.csv"
        path.write_text("100,0,0,1\n200,-3,-90,0.8\n400,-6,-180,0.9\n")
        result = callback(app, "import_trace")(*inputs, "out:2", str(path))
        assert [update for component, update in result.items()
                if getattr(component, "label", "") == "Show on"] == ["out:2"]
    finally:
        app.dsp_service.close()


@pytest.mark.parametrize("field,value", [("name", "Renamed"), ("association", "out:1"),
                                        ("coherence_threshold", 0.5), ("visible", False)])
def test_trace_edits_do_not_echo_stale_controls_or_selector_values(tmp_path, field, value):
    app = create_app()
    try:
        session = app.dsp_service.new_session()
        inputs = [session, "out:0", False, True, False, False, 0]
        path = tmp_path / "trace.csv"
        path.write_text("100,0,0,1\n200,-3,-90,0.8\n400,-6,-180,0.9\n")
        imported = callback(app, "import_trace")(*inputs, "all", str(path))
        identity = next(update["value"] for component, update in imported.items()
                        if getattr(component, "label", "") == "Trace")
        edit = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                    and name(fn.fn) == "edit_trace" and fn.fn.args == (field,))
        result = edit(*inputs, identity, value)
        assert not {"Display name", "Show on", "Visible", "Minimum coherence"}.intersection(
            getattr(component, "label", "") for component in result)
        if field == "name":
            selector = next(update for component, update in result.items()
                            if getattr(component, "label", "") == "Trace")
            assert "value" not in selector
            assert selector["choices"][0][0] == "Renamed"
    finally:
        app.dsp_service.close()


def test_advanced_editor_and_operation_confirmation():
    app = create_app()
    token = app.dsp_service.new_session()
    inputs = [token, "out:0", False, True, False, False, 0]
    result = callback(app, "execute")(*inputs, "save", 0, None, False)
    assert any("review and confirm" in str(value) for value in result.values())
    callback(app, "review")(token, "out:0", "save", 0, None)
    result = callback(app, "execute")(*inputs, "save", 1, None, True)
    assert any("destinations changed" in str(value) for value in result.values())
    callback(app, "review")(token, "out:0", "save", 0, None)
    result = callback(app, "execute")(*inputs, "save", 0, None, True)
    assert any("Operation completed" in str(value) for value in result.values())
    render_result = callback(app, "render")(*inputs)
    table = next(value for component, value in render_result.items()
                 if getattr(component, "label", "") == "Precise block editor")
    assert {row[0] for row in table} >= {"threshold_db", "enabled"}
    app.dsp_service.close()


def test_tuning_updates_leave_unrelated_editors_and_reviews_untouched():
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        callback(app, "render")(*inputs)
        callback(app, "review")(token, "out:0", "save", 0, None)
        reviewed = app.dsp_service.view(token).draft
        edit = next(fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                    and name(fn.fn) == "commit_numeric" and fn.fn.args == ("delay",))
        result = edit.fn(*inputs, 3.5)
        labels = {getattr(component, "label", "") for component in result}
        assert "Delay (ms)" in labels
        assert "Precise block editor" not in labels
        assert "Copy destination" not in labels
        assert "Selected band" not in labels
        assert reviewed.outputs[0].base.delay_ms == 0
        assert edit.show_progress == "hidden"
        plot_render = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                           and name(fn.fn) == "render" and fn.fn.keywords == {"scope": "plot"})
        result = plot_render(*inputs)
        assert all(getattr(component, "label", "") != "Precise block editor" for component in result)
        # A plot/tuning update retains the review, so execution reaches the dirty-draft guard.
        result = callback(app, "execute")(*inputs, "save", 0, None, True)
        assert any("Apply or revert pending edits" in str(value) for value in result.values())
    finally:
        app.dsp_service.close()


def test_idle_polling_skips_unchanged_components_and_does_not_replot():
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        callback(app, "render")(*inputs)
        tick = callback(app, "tick")
        assert tick(*inputs) == {"__type__": "update"}
        assert tick(*inputs) == {"__type__": "update"}
        state = SessionState(app)
        token_id = next(c["id"] for c in app.config["components"] if c["type"] == "state")
        state[token_id] = token
        tick_id = next(i for i, fn in app.fns.items() if name(fn.fn) == "tick")
        processed = asyncio.run(app.process_api(tick_id, [None, *inputs[1:]], state=state))
        # Rich HTML components wrap skipped prop updates; both forms leave values untouched.
        assert all(update in ({"__type__": "update"}, {"props": {"__type__": "update"}, "__type__": "update"})
                   for update in processed["data"])
        app.dsp_service.report_error(token, ValueError("Example action failure"))
        result = tick(*inputs)
        assert len(result) == 1
        component, message = next(iter(result.items()))
        assert component.elem_id == "status"
        assert "Example action failure" in message
        assert tick(*inputs) == {"__type__": "update"}
        assert next(fn for fn in app.fns.values() if name(fn.fn) == "tick").show_progress == "hidden"
    finally:
        app.dsp_service.close()


@pytest.mark.parametrize("field,value,reason", [
    ("eq_freq", None, "Enter frequency"), ("eq_freq", 3, "between 10 and 20000 Hz"),
    ("eq_freq", 30.5, "whole number"), ("eq_q", -1, "between 0.404 and 28.853"),
    ("hp_freq", 3, "between 10 and 20000 Hz"), ("lp_freq", 20001, "between 10 and 20000 Hz"),
    ("delay", -1, "between 0 and 1200 ms"),
])
def test_numeric_input_errors_are_inline_and_leave_the_valid_draft_unchanged(field, value, reason):
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        before = app.dsp_service.view(token)
        commit = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                      and name(fn.fn) == "commit_numeric" and fn.fn.args == (field,))
        result = commit(*inputs, value)
        assert app.dsp_service.view(token) == before
        assert any(isinstance(update, dict) and update.get("visible") and reason in update.get("value", "")
                   for update in result.values())
        assert any(isinstance(update, dict) and update.get("elem_classes") == ["invalid-number"]
                   for update in result.values())
        # Apply refuses to send other changes while a local numeric edit is invalid.
        channel = app.dsp_service.view(token).draft.outputs[0]
        values = {"delay": channel.base.delay_ms, "eq_freq": channel.eq_bands[0].freq_hz,
                  "eq_q": round(channel.eq_bands[0].q, 4), "hp_freq": channel.hp.freq_hz,
                  "lp_freq": channel.lp.freq_hz}
        values[field] = value
        result = callback(app, "apply_current")(*inputs, ["out:0", 0], *values.values())
        assert any("Correct highlighted values" in str(update) for update in result.values())
    finally:
        app.dsp_service.close()


def test_numeric_error_context_survives_channel_and_band_switches_without_overwriting_typing():
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        commit = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                      and name(fn.fn) == "commit_numeric" and fn.fn.args == ("eq_freq",))
        commit(*inputs, 3)
        render = callback(app, "render")
        for choice, band in [("out:1", 0), ("out:0", 1)]:
            result = render(token, choice, True, True, False, False, band)
            field_update = next(v for c, v in result.items() if c.elem_id == "eq-frequency")
            assert field_update["elem_classes"] == []
            assert field_update["value"] != 3
        result = render(*inputs)
        field_update = next(v for c, v in result.items() if c.elem_id == "eq-frequency")
        assert field_update["value"] == 3 and field_update["elem_classes"] == ["invalid-number"]
        queue = next(fn.fn for fn in app.fns.values() if isinstance(fn.fn, partial)
                     and name(fn.fn) == "queue_numeric" and fn.fn.args == ("eq_freq",))
        before = app.dsp_service.view(token)
        result = queue(token, "out:0", 0, 30)
        field_update = next(v for c, v in result.items() if c.elem_id == "eq-frequency")
        assert "value" not in field_update, "typing responses must not overwrite later frontend keystrokes"
        assert field_update["elem_classes"] == []
        assert app.dsp_service.view(token) == before
    finally:
        app.dsp_service.close()


def test_apply_preflights_current_numeric_values_and_rejects_stale_editor_context():
    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        channel = app.dsp_service.view(token).draft.outputs[0]
        values = [4.0, channel.eq_bands[0].freq_hz, channel.eq_bands[0].q, channel.hp.freq_hz, channel.lp.freq_hz]
        apply_current = callback(app, "apply_current")

        before = app.dsp_service.view(token)
        result = apply_current(*inputs, ["out:1", 0], *values)
        after_rejection = app.dsp_service.view(token)
        assert (after_rejection.baseline, after_rejection.draft, after_rejection.pending) == (
            before.baseline, before.draft, before.pending)
        assert any("Controls are still updating" in str(update) for update in result.values())

        apply_current(*inputs, ["out:0", 0], *values)
        view = app.dsp_service.view(token)
        assert view.baseline.outputs[0].base.delay_ms == 4.0
        assert view.draft == view.baseline
    finally:
        app.dsp_service.close()


def test_advanced_preview_does_not_echo_editor_selectors_or_table():
    from dataclasses import fields

    from sinbosen_la548.fir_adapter import FIRParameters

    app = create_app()
    try:
        token = app.dsp_service.new_session()
        inputs = [token, "out:0", True, True, False, False, 0]
        fir = FIRParameters.read(app.dsp_service.view(token).draft.outputs[0].fir)
        rows = [[field.name, str(getattr(fir, field.name)).lower()
                 if isinstance(getattr(fir, field.name), bool) else str(getattr(fir, field.name))]
                for field in fields(fir)]
        rows[0][1] = "true"
        result = callback(app, "preview")(*inputs, "fir", 1, rows)
        labels = {getattr(component, "label", "") for component in result}
        assert "Block" not in labels
        assert "Dynamic EQ band" not in labels
        assert "Precise block editor" not in labels
        assert any(getattr(component, "elem_id", "") == "response-main" for component in result)
        assert len(app.dsp_service.view(token).pending) == 1
    finally:
        app.dsp_service.close()
