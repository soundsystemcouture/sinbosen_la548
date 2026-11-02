"""Recorded browser checks for the Gradio UI, using the injected fake only."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from sinbosen_la548.service import ControlKey

from .conftest import FAKE_HOST, Scenario

pytestmark = pytest.mark.e2e


def _plot_names(page, selector: str) -> list[str]:
    return page.locator(selector).evaluate("plot => plot.data.map(trace => trace.name || '')")


def _choose(page, label: str, option: str, *, scope=None) -> None:
    target = (scope or page).get_by_label(label, exact=True)
    target.click()
    choices = page.locator('[role="option"]')
    assert choices.count(), f"No open choices for {label}: {page.locator('body').inner_text()[-1000:]}"
    choice = choices.filter(has_text=option)
    assert choice.count(), f"{option!r} is not one of {choices.all_inner_texts()}"
    # The floating option list can open partly outside the viewport, where a
    # click can never land (and scrolling doesn't help); Gradio selects on
    # mousedown, so dispatch that instead.
    choice.first.dispatch_event("mousedown")
    page.locator('[role="option"]').first.wait_for(state="detached")


def _number(page, label: str, value: str, *, scope=None) -> None:
    target = (scope or page).get_by_label(label, exact=True)
    target.fill(value)
    target.press("Enter")


def _drag_slider(page, selector: str, fraction: float) -> None:
    slider = page.locator(selector).locator("input[type=range]")
    slider.scroll_into_view_if_needed()
    box = slider.bounding_box()
    assert box is not None, "slider has no visible box"
    y = box["y"] + box["height"] / 2
    start = slider.evaluate("input => (+input.value - +input.min) / (+input.max - +input.min)")
    page.mouse.move(box["x"] + box["width"] * start, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * fraction, y, steps=8)
    page.mouse.up()


def _live_checkbox(page):
    return page.locator("#live-tuning:visible input[type=checkbox]").last


def _start_eq_drag(page, band: int = 0) -> None:
    page.locator("#response-main").scroll_into_view_if_needed()
    handle = page.locator(f'.eq-handle-overlay g[data-band="{band}"]').locator("circle, path")
    handle.wait_for()
    # SVG nodes can be replaced on the next animation frame. Read their geometry atomically.
    box = handle.evaluate("""shape => {
        const r=shape.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height};
    }""")
    assert box["width"] > 0, handle.evaluate("""shape => {
        const rows=[]; for (let n=shape;n;n=n.parentElement) {
            const r=n.getBoundingClientRect(), s=getComputedStyle(n);
            rows.push([n.tagName,n.id,n.getAttribute('transform'),s.display,s.visibility,r.width,r.height]);
        } return rows;
    }""")
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 3, box["y"] + box["height"] / 2)
    page.locator("#eq-drag-controller").get_by_text("Previewing frequency", exact=False).wait_for(timeout=20000)


def _move_eq_drag(page, frequency: int, *, wait: bool = True) -> None:
    position = page.locator("#response-main .js-plotly-plot").evaluate("""(plot, frequency) => {
        const box = plot.getBoundingClientRect(), x = plot._fullLayout.xaxis, y = plot._fullLayout.yaxis;
        return {x: box.left + x._offset + x.l2p(Math.log10(frequency)), y: box.top + y._offset + 20};
    }""", frequency)
    page.mouse.move(position["x"], position["y"], steps=12)
    if not wait:
        return
    page.wait_for_function("""frequency => {
        const svg = document.querySelector('.eq-handle-overlay');
        return svg.dataset.previewSequence && [...svg.querySelectorAll('g')].some(
            handle => Number(handle.dataset.frequency) === frequency);
    }""", arg=frequency)


def _download(page, filename_part: str, destination: Path) -> None:
    links = page.locator('#management-download:visible a[href*="/gradio_api/file"]:visible')
    assert links.count(), f"No file download link for {filename_part!r}: {page.locator('a').all_inner_texts()}"
    with page.expect_download(timeout=20000) as event:
        links.first.click()
    event.value.save_as(str(destination))


def _upload_trace(page, path: Path) -> None:
    container = page.locator("#trace-import")
    input_file = container.locator("input[type=file]")
    if not input_file.count():
        container.locator("button").last.click()
        assert input_file.count(), "clearing the prior trace did not restore the upload input"
    input_file.set_input_files(str(path))


def _upload_file(page, selector: str, path: Path) -> None:
    container = page.locator(selector)
    input_file = container.locator("input[type=file]")
    if not input_file.count():
        container.locator("button").last.click()
    input_file.set_input_files(str(path))


def _wait_pending(e2e: Scenario, amount: int | None = None) -> None:
    e2e.status(rf"{amount if amount is not None else '[1-9][0-9]*'} pending")


def _edit_advanced(page, values: dict[str, object]) -> None:
    table = page.locator("#advanced-block-editor:visible")
    names = table.locator('[data-testid^="cell-"][data-testid$="-0"]').all_inner_texts()
    for name, value in values.items():
        row = names.index(name)
        cell = table.get_by_test_id(f"cell-{row}-1")
        cell.dblclick()
        page.keyboard.press("Control+A")
        page.keyboard.type(str(value).lower() if isinstance(value, bool) else str(value))
        page.keyboard.press("Enter")


def _stage_and_apply_advanced(e2e: Scenario, block: str, values: dict[str, object], expected_write: str) -> None:
    page = e2e.page
    _choose(page, "Block", block)
    _edit_advanced(page, values)
    before = len(e2e.device.writes)
    page.get_by_role("button", name="Preview block changes", exact=True).click()
    _wait_pending(e2e, 1)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    assert [call[0] for call in e2e.device.writes[before:]] == [expected_write]


def test_drag_eq_and_allpass_frequency_preview_apply(e2e: Scenario) -> None:
    """Horizontal EQ/shelf/allpass drag previews, automatic Live pause, manual Apply, and preserved parameters."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    editor = page.locator("#eq-band-editor")
    editor.get_by_label("Band enabled", exact=True).check()
    _number(page, "Q", "1.5", scope=editor)
    _drag_slider(page, "#eq-gain", 0.65)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    for family, target in [("Parametric EQ", 400), ("Low shelf", 200),
                           ("High shelf", 4000), ("Allpass (2nd order)", 120)]:
        if family != "Parametric EQ":
            _choose(page, "Filter", family, scope=editor)
            _wait_pending(e2e, 1)
            page.get_by_role("button", name="Apply changes", exact=True).click()
            e2e.status("Applied and verified")
        before = e2e.device.settings.outputs[0].eq_bands[0]
        if family.startswith("Allpass"):
            assert before.gain_db == 0
        else:
            assert 5 <= before.gain_db <= 7, "The manual gain slider must produce an audible modeled EQ change."
        if family == "Parametric EQ":
            graph = page.locator("#response-main .js-plotly-plot")
            graph.evaluate("plot => window.Plotly.relayout(plot, {'yaxis.range': [-2, 2]})")
            expect(page.locator('.eq-handle-overlay g[data-band="0"]')).to_have_count(0)
            graph.evaluate("plot => window.Plotly.relayout(plot, {'yaxis.range': [-60, 20]})")
            expect(page.locator('.eq-handle-overlay g[data-band="0"]')).to_have_count(1)
        previous_phase = page.locator("#response-main .js-plotly-plot").evaluate(
            """plot => {
                const t=plot._fullData[1], i=Array.from(t.x).reduce((best,f,index) =>
                    Math.abs(f-700) < Math.abs(t.x[best]-700) ? index : best, 0);
                return t.y[i];
            }""")
        initial_writes = len(e2e.device.writes)
        _live_checkbox(page).check()
        expect(_live_checkbox(page)).to_be_checked()
        _start_eq_drag(page)
        expect(_live_checkbox(page)).not_to_be_checked()
        _move_eq_drag(page, target)
        assert len(e2e.device.writes) == initial_writes
        expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
        # All three response panels are recalculated during the transient preview.
        assert page.locator("#response-main .js-plotly-plot").evaluate(
            "plot => plot._fullData.slice(0, 3).every(trace => trace.x.length === 1024)"
        )
        curves = page.locator("#response-main .js-plotly-plot").evaluate("""plot => {
            const [m,p,d]=plot._fullData;
            const i=Array.from(p.x).reduce((best,f,index) =>
                Math.abs(f-700) < Math.abs(p.x[best]-700) ? index : best, 0);
            return {magnitude: Math.max(...m.y), phase: p.y[i], delay: Math.max(...d.y)};
        }""")
        assert abs(previous_phase - curves["phase"]) > 0.1
        assert curves["delay"] > 0.001
        if not family.startswith("Allpass"):
            assert curves["magnitude"] > 5
        e2e.reveal(page.locator("#response-main"), f"drag-{family.split()[0].lower()}-preview")
        page.mouse.up()
        _wait_pending(e2e, 1)
        expect(page.locator("#eq-frequency input")).to_have_value(str(target))
        expect(page.locator("#draft-status")).to_contain_text("Unapplied changes: 1 controls across 1 channels")
        assert len(e2e.device.writes) == initial_writes
        page.get_by_role("button", name="Apply changes", exact=True).click()
        e2e.status("Applied and verified")
        writes = e2e.device.writes[initial_writes:]
        assert len(writes) == 1 and writes[0][0] == "set_eq_band"
        after = e2e.device.settings.outputs[0].eq_bands[0]
        assert after.freq_hz == target
        assert (after.gain_db, after.q, after.type, after.enabled) == (
            before.gain_db, before.q, before.type, before.enabled)
        expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    e2e.assert_clean_browser()


def test_drag_disabled_cancel_zoom_and_editor_sync(e2e: Scenario) -> None:
    """Disabled handles, cancellation, limits, zoom/resize, overlapping selection, and inline-error recovery."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    _start_eq_drag(page)
    _move_eq_drag(page, 250)
    page.keyboard.press("Escape")
    page.mouse.up()
    page.wait_for_function("() => document.querySelector('.eq-handle-overlay').dataset.active === ''")
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    expect(page.locator("#eq-frequency input")).to_have_value("1000")
    assert e2e.device.writes == []

    _start_eq_drag(page)
    _move_eq_drag(page, 250)
    page.mouse.up()
    _wait_pending(e2e, 1)
    expect(page.locator("#eq-band-editor").get_by_label("Band enabled", exact=True)).not_to_be_checked()
    _choose(page, "Selected band", "Band 2")
    _number(page, "Frequency (Hz)", "250", scope=page.locator("#eq-band-editor"))
    page.locator("#eq-band-editor").get_by_label("Band enabled", exact=True).check()
    _choose(page, "Selected band", "Band 1")
    # The selected disabled handle is above the enabled handle at the same location.
    page.wait_for_function("""() => {
        const handles = document.querySelectorAll('.eq-handle-overlay g');
        return handles.length === 2 && handles[handles.length - 1].dataset.band === '0';
    }""")
    _number(page, "Frequency (Hz)", "3", scope=page.locator("#eq-band-editor"))
    expect(page.locator("#draft-status")).to_contain_text("Unfinished input")
    _start_eq_drag(page)
    _move_eq_drag(page, 150)
    page.mouse.up()
    expect(page.locator("#eq-frequency input")).to_have_value("150")
    expect(page.locator("#eq-frequency-error")).not_to_be_visible()

    page.get_by_label("Subwoofer zoom", exact=True).check()
    page.wait_for_function("""() => Math.abs(document.querySelector(
        '#response-main .js-plotly-plot')._fullLayout.xaxis.range[1] - Math.log10(300)) < 0.0001""")
    page.set_viewport_size({"width": 1600, "height": 900})
    _start_eq_drag(page)
    # Drag beyond the plot's right edge; the frequency must stop at the visible 300 Hz limit.
    box = page.locator("#response-main .js-plotly-plot").bounding_box()
    page.mouse.move(box["x"] + box["width"] + 100, box["y"] + 130, steps=10)
    page.mouse.up()
    expect(page.locator("#eq-frequency input")).to_have_value("300")
    assert e2e.device.writes == []
    page.set_viewport_size({"width": 1920, "height": 1080})
    e2e.reveal(page.locator("#response-main"), "disabled-overlap-zoom-preview")
    page.get_by_role("button", name="Revert draft", exact=True).click()
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    e2e.assert_clean_browser()


def test_drag_a_band_other_than_the_selected_one(e2e: Scenario) -> None:
    """Dragging an enabled, non-selected band stages that band, not the selected one."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    _choose(page, "Selected band", "Band 2")
    _number(page, "Frequency (Hz)", "2000", scope=page.locator("#eq-band-editor"))
    page.locator("#eq-band-editor").get_by_label("Band enabled", exact=True).check()
    _choose(page, "Selected band", "Band 1")
    page.wait_for_function("() => document.querySelectorAll('.eq-handle-overlay g').length === 2")
    _start_eq_drag(page, band=1)
    _move_eq_drag(page, 500, wait=False)
    # The grab offset on a non-selected handle shifts the landing point by a
    # few percent, so wait for the band-2 preview near 500 Hz and stage that.
    handle = page.wait_for_function("""() => {
        const svg = document.querySelector('.eq-handle-overlay');
        const g = svg.dataset.previewSequence && svg.querySelector('g[data-band="1"]');
        const f = g && Number(g.dataset.frequency);
        return f >= 400 && f <= 600 ? f : null;
    }""")
    previewed = int(handle.json_value())
    page.mouse.up()
    expect(page.locator("#eq-frequency input")).to_have_value(str(previewed))
    expect(page.locator("#eq-drag-controller")).not_to_contain_text("unavailable")
    expect(page.locator("#eq-band-editor").get_by_label("Band enabled", exact=True)).to_be_checked()
    assert e2e.device.writes == []
    page.get_by_role("button", name="Revert draft", exact=True).click()
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    e2e.assert_clean_browser()


def test_unapplied_banner_across_channels_and_failures(e2e: Scenario) -> None:
    """Persistent global draft status across channels, returning to baseline, partial Apply, disconnect, and Revert."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    _number(page, "Delay (ms)", "2")
    expect(page.locator("#draft-status")).to_contain_text("1 controls across 1 channels")
    page.get_by_label("Out2", exact=True).check()
    _number(page, "Delay (ms)", "3")
    expect(page.locator("#draft-status")).to_contain_text("2 controls across 2 channels")
    _number(page, "Delay (ms)", "0")
    expect(page.locator("#draft-status")).to_contain_text("1 controls across 1 channels")
    _number(page, "Delay (ms)", "3")
    e2e.device.fail_next("set_delay", TimeoutError("banner partial write failure"))
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("pending")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes")
    page.get_by_role("button", name="Disconnect", exact=True).click()
    e2e.status("Disconnected")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes")
    e2e.reveal(page.locator("#draft-status"), "unapplied-after-disconnect")
    page.get_by_role("button", name="Revert draft", exact=True).click()
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    e2e.assert_clean_browser()


def test_drag_late_preview_and_context_changes(e2e: Scenario) -> None:
    """Delayed preview cancellation, pointer cancellation, band/channel invalidation, and unchanged exported curves."""
    import threading
    import time
    import zipfile

    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    page.locator("#eq-band-editor").get_by_label("Band enabled", exact=True).check()
    _drag_slider(page, "#eq-gain", 0.65)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    initial_writes = len(e2e.device.writes)
    controller = next(component for component in e2e.app.blocks.values()
                      if getattr(component, "elem_id", "") == "eq-drag-controller")
    # Gradio copies component instances into session state. Patch the shared
    # preview controller inside its closure rather than one component instance.
    handler = controller.preview_drag
    drag_edits = dict(zip(handler.__code__.co_freevars,
                          [cell.cell_contents for cell in handler.__closure__], strict=True))["drags"]
    original = drag_edits.preview
    started = threading.Event()
    def delayed(*args):
        started.set()
        time.sleep(0.8)
        return original(*args)
    drag_edits.preview = delayed
    _start_eq_drag(page)
    _move_eq_drag(page, 200, wait=False)
    assert started.wait(timeout=3)
    page.keyboard.press("Escape")
    page.mouse.up()
    expect(page.locator("#eq-frequency input")).to_have_value("1000")
    page.wait_for_timeout(1100)
    assert page.locator("#response-main .js-plotly-plot").evaluate(
        "plot => plot._fullData[0].x.length === 4096")
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    drag_edits.preview = original

    _start_eq_drag(page)
    _move_eq_drag(page, 400)
    # Change selection without physically releasing the held drag pointer first.
    page.get_by_label("Selected band", exact=True).focus()
    page.get_by_label("Selected band", exact=True).press("ArrowDown")
    page.locator('[role="option"]').filter(has_text="Band 2").first.dispatch_event("mousedown")
    page.wait_for_function("() => document.querySelector('.eq-handle-overlay').dataset.active === ''")
    page.mouse.up()
    page.wait_for_function("""() => document.querySelector(
        '#response-main .js-plotly-plot')._fullData[0].x.length === 4096""")
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    _choose(page, "Selected band", "Band 1")
    _start_eq_drag(page)
    _move_eq_drag(page, 500)
    page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {pointerId: 1}))")
    page.mouse.up()
    expect(page.locator("#eq-frequency input")).to_have_value("1000")
    _start_eq_drag(page)
    _move_eq_drag(page, 600)
    page.get_by_label("Out2", exact=True).evaluate("input => input.click()")
    page.wait_for_function("() => document.querySelector('.eq-handle-overlay').dataset.active === ''")
    page.mouse.up()
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    assert len(e2e.device.writes) == initial_writes

    page.get_by_role("button", name="Export response plots", exact=True).click()
    download = page.get_by_role("button", name="Download response ZIP", exact=True)
    expect(download).to_be_enabled()
    with page.expect_download() as event:
        download.click()
    destination = e2e.directory / "downloads" / "drag-response.zip"
    event.value.save_as(str(destination))
    with zipfile.ZipFile(destination) as archive:
        curve = archive.read("curves.csv").decode()
        assert "frequency_hz" in curve and "Draft · combined" in curve
        assert "Band 1" not in curve
    e2e.reveal(page.locator("#response-main"), "cancelled-drag-restored")
    e2e.assert_clean_browser()


def test_connection_preview_and_crossovers_allpass(e2e: Scenario) -> None:
    """Connection, crossover/allpass draft preview, bypass/restore, and verified Apply."""
    page = e2e.page
    assert e2e.device.writes == []
    assert "Draft · combined" in _plot_names(page, "#response-main .js-plotly-plot")
    e2e.connect()
    assert e2e.device.writes == []
    assert e2e.factory.hosts == [FAKE_HOST]

    hp = page.locator("#xover-hp")
    lp = page.locator("#xover-lp")
    hp.get_by_label("Enable high-pass", exact=True).check()
    _number(page, "Corner frequency (Hz)", "96", scope=hp)
    _choose(page, "Family", "Linkwitz–Riley", scope=hp)
    _choose(page, "Slope (dB/oct)", "24", scope=hp)
    lp.get_by_label("Enable low-pass", exact=True).check()
    _number(page, "Corner frequency (Hz)", "1800", scope=lp)
    _choose(page, "Family", "Bessel", scope=lp)
    _choose(page, "Slope (dB/oct)", "18", scope=lp)
    page.get_by_role("button", name="Add allpass", exact=True).click()
    _wait_pending(e2e)
    _number(page, "Q", "2", scope=page.locator("#eq-band-editor"))
    assert e2e.device.writes == []
    names = _plot_names(page, "#response-main .js-plotly-plot")
    assert any("Allpass" in name for name in names)
    e2e.reveal(page.locator("#response-main"), "crossover-allpass-preview")

    page.get_by_role("button", name="Bypass all EQ", exact=True).click()
    _wait_pending(e2e)
    page.get_by_role("button", name="Restore EQ", exact=True).click()
    _wait_pending(e2e)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    writes = [name for name, _, _ in e2e.device.writes]
    assert "set_xover" in writes and "set_eq_band" in writes
    assert _plot_names(page, "#response-main .js-plotly-plot")
    e2e.assert_clean_browser()


def test_local_edits_do_not_reload_unrelated_panels(e2e: Scenario) -> None:
    """Quiet polling, retained unstaged advanced edits, and band selection without replotting."""
    page = e2e.page
    e2e.connect()
    page.get_by_role("tab", name="Advanced", exact=True).click()
    cell = page.locator("#advanced-block-editor:visible").get_by_test_id("cell-0-1")
    cell.dblclick()
    page.keyboard.press("Control+A")
    page.keyboard.type("123")
    page.keyboard.press("Enter")
    assert "123" in cell.inner_text()

    page.get_by_label("Subwoofer zoom", exact=True).check()
    page.wait_for_function("""() => {
        const plot = document.querySelector('#response-main .js-plotly-plot');
        return Math.abs(plot.layout.xaxis.range[1] - Math.log10(300)) < 0.0001;
    }""")
    assert "123" in cell.inner_text(), "plot options replaced the unstaged advanced editor"
    _number(page, "Delay (ms)", "2.5")
    _wait_pending(e2e, 1)
    assert "123" in cell.inner_text(), "a base control replaced the unstaged advanced editor"
    e2e.reveal(page.locator("#advanced-block-editor"), "retained-advanced-edit")

    page.get_by_role("tab", name="Crossovers + EQ / allpass", exact=True).click()
    page.locator("#response-main .js-plotly-plot").evaluate("plot => window.stableResponseData = plot.data")
    _choose(page, "Selected band", "Band 2")
    # Let multiple 250 ms polling ticks run; none should replace the plot data.
    page.wait_for_timeout(1000)
    assert page.locator("#response-main .js-plotly-plot").evaluate(
        "plot => plot.data === window.stableResponseData"
    ), "band selection or idle polling replotted an unchanged response"
    assert e2e.device.writes == []
    e2e.reveal(page.locator("#response-main"), "stable-response-preview")
    e2e.assert_clean_browser()


def test_numeric_typing_debounce_and_inline_validation(e2e: Scenario) -> None:
    """Blank/intermediate allpass values stay local, invalid fields explain why, and Live writes only valid values."""
    page = e2e.page
    e2e.connect()
    page.get_by_role("button", name="Add allpass", exact=True).click()
    _wait_pending(e2e)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    _live_checkbox(page).check()
    frequency = page.locator("#eq-frequency input")
    error = page.locator("#eq-frequency-error")
    band = next(band for band in e2e.device.settings.outputs[0].eq_bands if band.enabled and band.type == 4)
    initial_writes = len(e2e.device.writes)

    frequency.fill("")
    error.get_by_text("Enter frequency.", exact=True).wait_for(timeout=20000)
    assert frequency.input_value() == ""
    assert frequency.evaluate("input => getComputedStyle(input).borderColor") == "rgb(197, 48, 48)"
    frequency.fill("3")
    error.get_by_text("Frequency must be between 10 and 20000 Hz.", exact=True).wait_for(timeout=20000)
    assert frequency.input_value() == "3"
    assert len(e2e.device.writes) == initial_writes
    assert any(b.enabled and b.type == 4 and b.freq_hz == band.freq_hz
               for b in e2e.device.settings.outputs[0].eq_bands)
    e2e.reveal(page.locator("#eq-band-editor"), "inline-invalid-frequency")

    frequency.evaluate("""input => {
        for (const value of ["", "3", "30"]) {
            input.value = value;
            input.dispatchEvent(new Event("input", {bubbles: true}));
        }
    }""")
    e2e.status("Applied and verified")
    page.wait_for_function("""() => {
        const input = document.querySelector('#eq-frequency input');
        return input.value === '30' && getComputedStyle(input).borderColor !== 'rgb(197, 48, 48)';
    }""")
    page.wait_for_timeout(600)
    writes = e2e.device.writes[initial_writes:]
    assert len(writes) == 1 and writes[0][0] == "set_eq_band"
    assert writes[0][2]["freq_hz"] == 30
    assert not error.is_visible()
    assert not page.locator(".toast-wrap:visible").count(), "typing produced a Gradio error toast"
    e2e.reveal(page.locator("#eq-band-editor"), "valid-frequency-live-applied")

    _live_checkbox(page).uncheck()
    frequency.fill("30.5")
    error.get_by_text("Frequency must be a whole number (Hz).", exact=True).wait_for(timeout=20000)
    writes_before_apply = len(e2e.device.writes)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Correct highlighted values")
    assert len(e2e.device.writes) == writes_before_apply
    page.get_by_role("button", name="Revert draft", exact=True).click()
    e2e.status("Draft reverted")
    assert frequency.input_value() == "30"
    assert not error.is_visible()
    e2e.assert_clean_browser()


def test_immediate_apply_captures_numeric_fields_and_rapid_eq_edits(e2e: Scenario) -> None:
    """Apply captures the last keystroke, and numeric responses never roll back a newer EQ toggle."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    editor = page.locator("#eq-band-editor")
    page.get_by_role("button", name="Apply changes", exact=True).click()
    expect(page.locator("#draft-status")).to_contain_text("No unapplied changes")
    page.wait_for_timeout(350)
    assert e2e.device.writes == []
    _live_checkbox(page).check()
    expect(_live_checkbox(page)).to_be_checked()
    assert e2e.device.writes == []
    _live_checkbox(page).uncheck()
    values = {
        page.get_by_label("Delay (ms)", exact=True): "7.25",
        editor.get_by_label("Frequency (Hz)", exact=True): "777",
        editor.get_by_label("Q", exact=True): "1.234",
        page.locator("#xover-hp").get_by_label("Corner frequency (Hz)", exact=True): "75",
        page.locator("#xover-lp").get_by_label("Corner frequency (Hz)", exact=True): "8000",
    }
    for field, value in values.items():
        field.fill(value)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    channel = e2e.device.settings.outputs[0]
    assert channel.base.delay_ms == 7.25
    assert channel.eq_bands[0].freq_hz == 777
    assert channel.eq_bands[0].q == pytest.approx(1.234, abs=0.001)
    assert channel.hp.freq_hz == 75 and channel.lp.freq_hz == 8000

    _choose(page, "Selected band", "Band 2")
    enabled = editor.get_by_label("Band enabled", exact=True)
    frequency = editor.get_by_label("Frequency (Hz)", exact=True)
    for attempt in range(10):
        frequency.fill(str(300 + attempt))
        frequency.press("Enter")
        expected = attempt % 2 == 0
        enabled.set_checked(expected)
        if expected:
            expect(enabled).to_be_checked()
        else:
            expect(enabled).not_to_be_checked()
        page.wait_for_timeout(350)
        if expected:
            expect(enabled).to_be_checked()
        else:
            expect(enabled).not_to_be_checked()
    assert e2e.device.writes
    assert not [call for call in e2e.device.writes if call[0] not in {"set_delay", "set_eq_band", "set_xover"}]
    e2e.assert_clean_browser()


def test_drafts_mixer_revert_and_minimal_apply(e2e: Scenario) -> None:
    """Draft retention across channels, binary routing, Revert, and minimal device writes."""
    page = e2e.page
    e2e.connect()
    baseline_writes = len(e2e.device.writes)
    page.get_by_label("Mute", exact=True).check()
    page.get_by_label("Invert polarity", exact=True).uncheck()
    _drag_slider(page, "#gain-control:visible", 0.8)
    _number(page, "Delay (ms)", "3.5")
    route = page.locator("#mixer-route-0-0").get_by_role("checkbox")
    page.get_by_role("tab", name="Mixer", exact=True).click()
    route.uncheck()
    _wait_pending(e2e)
    page.get_by_role("radio", name="Out2", exact=True).check()
    page.locator("#channel-info").get_by_text("Out2", exact=True).wait_for(timeout=20000)
    page.get_by_role("radio", name="Out1", exact=True).check()
    page.locator("#channel-info").get_by_text("Out1", exact=True).wait_for(timeout=20000)
    assert page.get_by_label("Mute", exact=True).is_checked()
    assert not page.get_by_label("Invert polarity", exact=True).is_checked()
    page.get_by_role("button", name="Revert draft", exact=True).click()
    e2e.status("Draft reverted")
    assert page.get_by_label("Invert polarity", exact=True).is_checked()
    assert len(e2e.device.writes) == baseline_writes

    page.get_by_label("Mute", exact=True).check()
    page.get_by_label("Invert polarity", exact=True).uncheck()
    _drag_slider(page, "#gain-control:visible", 0.78)
    _number(page, "Delay (ms)", "4")
    page.get_by_role("tab", name="Mixer", exact=True).click()
    route.uncheck()
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    writes = e2e.device.writes[baseline_writes:]
    assert {name for name, _, _ in writes} == {
        "set_mute", "set_phase", "set_volume", "set_delay", "set_mix_route",
    }
    e2e.reveal(page.locator("#mixer-grid:visible").last, "mixer-applied")
    e2e.assert_clean_browser()


def test_live_tuning_coalescing_and_failure(e2e: Scenario) -> None:
    """Live slider/numeric edits coalesce, refresh readback, and stop after a write failure."""
    page = e2e.page
    e2e.connect()
    _live_checkbox(page).check()
    assert _live_checkbox(page).is_checked()
    _drag_slider(page, "#gain-control:visible", 0.8)
    delay = page.get_by_label("Delay (ms)", exact=True)
    delay.evaluate("""input => {
        for (const value of ["1", "2", "3"]) {
            input.value = value;
            input.dispatchEvent(new Event("input", {bubbles: true}));
        }
    }""")
    page.wait_for_timeout(700)
    delay_writes = [call for call in e2e.device.writes if call[0] == "set_delay"]
    assert len(delay_writes) == 1
    assert delay_writes[-1][1][1] == 3.0
    assert any(call[0] == "set_volume" for call in e2e.device.writes)
    assert "Last readback" in _plot_names(page, "#response-main .js-plotly-plot")
    e2e.device.fail_next("set_delay", TimeoutError("injected live write failure"))
    _number(page, "Delay (ms)", "4")
    page.wait_for_timeout(700)
    e2e.status("injected live write failure")
    assert not _live_checkbox(page).is_checked()
    e2e.reveal(page.locator("#status"), "live-failure")
    e2e.assert_clean_browser()


def test_measured_trace_imports_and_immutable_failure(e2e: Scenario) -> None:
    """Native OSM, missing CSV samples, magnitude-only data, and failed imports preserve traces."""
    from playwright.sync_api import expect

    page = e2e.page
    native = e2e.directory / "adjusted.osm"
    native.write_text(json.dumps({"type": "stored", "data": {"name": "Adjusted OSM", "gain": 2,
                                  "delay": 1.25, "polarity": True, "inverse": False, "icoherence": False,
                                  "ftdata": [[0, 0, 1, 0, 1], [100, 0, 1, 0, 0.8],
                                             [200, 0, 0.5, -1.57, 0.4], [400, 0, 0.25, -3.14, 1]]}}))
    missing = e2e.directory / "gaps.csv"
    missing.write_text("100,0,0,1\n200,*,*,0.2\n400,-6,-180,0.9\n")
    magnitude_only = e2e.directory / "magnitude-only.frd"
    magnitude_only.write_text("100 -3\n200 -6\n400 -9\n")
    page.get_by_role("tab", name="Measured traces", exact=True).click()
    for path, expected in [(native, "Adjusted OSM"), (missing, "gaps"), (magnitude_only, "magnitude-only")]:
        _upload_trace(page, path)
        page.get_by_role("button", name="Import and overlay", exact=True).click()
        expect(page.get_by_label("Display name", exact=True)).to_have_value(expected, timeout=20000)
    page.get_by_label("Display name", exact=True).fill("Front left measurement")
    page.get_by_label("Display name", exact=True).press("Enter")
    _choose(page, "Show on", "Output 2")
    coherence = page.locator("#coherence-control:visible input[type=range]")
    coherence.click(position={"x": 90, "y": 5})
    page.get_by_label("Visible", exact=True).uncheck()
    expect(page.get_by_label("Visible", exact=True)).not_to_be_checked()
    bad = e2e.directory / "bad.csv"
    bad.write_text("this is not a trace\n")
    _upload_trace(page, bad)
    page.get_by_role("button", name="Import and overlay", exact=True).click()
    page.get_by_text("Import failed", exact=False).wait_for(timeout=20000)
    assert page.get_by_label("Display name", exact=True).input_value() == "Front left measurement"
    assert e2e.device.writes == []
    e2e.reveal(page.get_by_label("Trace", exact=True), "trace-imports")
    e2e.assert_clean_browser()


def test_alignment_and_response_plot_controls(e2e: Scenario) -> None:
    """Common-input electrical comparison, draft alignment, phase display, and measured timing gate."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    measured_a = e2e.directory / "alignment-a.csv"
    measured_b = e2e.directory / "alignment-b.csv"
    measured_a.write_text("100,0,0,1\n200,0,0,1\n400,0,0,1\n")
    measured_b.write_text("100,0,180,1\n200,0,180,1\n400,0,180,1\n")
    page.get_by_role("tab", name="Measured traces", exact=True).click()
    for path in (measured_a, measured_b):
        _upload_trace(page, path)
        page.get_by_role("button", name="Import and overlay", exact=True).click()
        expect(page.get_by_label("Display name", exact=True)).to_have_value(path.stem, timeout=20000)
    page.get_by_role("tab", name="Alignment", exact=True).click()
    page.get_by_role("button", name="Refresh electrical comparison", exact=True).click()
    page.locator("#alignment-electrical .js-plotly-plot").wait_for(timeout=20000)
    assert "Electrical sum" in _plot_names(page, "#alignment-electrical .js-plotly-plot")
    assert _plot_names(page, "#alignment-phase .js-plotly-plot")
    _number(page, "A delay (ms)", "2")
    page.get_by_label("Invert A", exact=True).check()
    page.get_by_role("button", name="Preview alignment changes", exact=True).click()
    _wait_pending(e2e)
    page.get_by_label("Unwrapped phase", exact=True).check()
    page.get_by_label("Subwoofer zoom", exact=True).check()
    _choose(page, "Measured A", "alignment-a")
    _choose(page, "Measured B", "alignment-b")
    page.get_by_role("button", name="Calculate measured sum", exact=True).click()
    page.get_by_text("Measured sum unavailable", exact=False).wait_for(timeout=20000)
    page.get_by_label("These traces share timing and level references", exact=True).check()
    page.get_by_role("button", name="Calculate measured sum", exact=True).click()
    page.get_by_text("Measured complex sum", exact=False).wait_for(timeout=20000)
    assert "Measured sum" in _plot_names(page, "#alignment-measured .js-plotly-plot")
    e2e.reveal(page.locator("#alignment-electrical"), "alignment-electrical")
    e2e.reveal(page.locator("#alignment-phase"), "alignment-phase")
    e2e.reveal(page.locator("#alignment-measured"), "alignment-measured")
    e2e.assert_clean_browser()


def test_all_advanced_blocks_apply_to_the_selected_channel(e2e: Scenario) -> None:
    """Every DA0408X advanced block stages, writes once, verifies, and remains channel-scoped."""
    page = e2e.page
    e2e.connect()
    page.get_by_role("tab", name="Advanced", exact=True).click()
    _stage_and_apply_advanced(e2e, "Compressor", {
        "threshold_db": -20, "start_time_ms": 10, "release_time_ms": 200, "ratio": 4, "enabled": True,
    }, "set_compressor")
    _stage_and_apply_advanced(e2e, "Limiter", {
        "threshold_db": -18, "start_time_ms": 10, "release_time_ms": 200,
        "enabled": True, "detection_type": 1,
    }, "set_limiter")
    _stage_and_apply_advanced(e2e, "FIR parameters", {
        "enabled": True, "fir_type": 0, "win_type": 0, "freq_low_hz": 100,
        "freq_high_hz": 1000, "attenuation_db": 60, "order_index": 1,
    }, "set_fir")
    page.get_by_text("256 taps", exact=False).wait_for(timeout=20000)
    page.get_by_role("tab", name="Crossovers + EQ / allpass", exact=True).click()
    page.get_by_text("Incomplete model", exact=False).wait_for(timeout=20000)
    e2e.reveal(page.locator("#response-main"), "fir-incomplete")

    page.get_by_role("tab", name="Advanced", exact=True).click()
    page.get_by_role("radio", name="InA", exact=True).click()
    _stage_and_apply_advanced(e2e, "Expander", {
        "threshold_db": -40, "start_time_ms": 10, "release_time_ms": 200, "ratio": 2, "enabled": True,
    }, "set_expander")
    _choose(page, "Block", "Dynamic EQ")
    _choose(page, "Dynamic EQ band", "2")
    _edit_advanced(page, {
        "freq_hz": 1000, "gain_db": 6, "q": 1, "slope": 1, "type": 0, "startup_mode": 1,
        "threshold_db": -30, "start_time_ms": 10, "release_time_ms": 200, "ratio": 2, "enabled": True,
    })
    before = len(e2e.device.writes)
    page.get_by_role("button", name="Preview block changes", exact=True).click()
    _wait_pending(e2e, 1)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Applied and verified")
    assert [call[0] for call in e2e.device.writes[before:]] == ["set_dynamic_eq_band"]
    assert e2e.device.writes[-1][1][:2] == (0, 1)
    _stage_and_apply_advanced(e2e, "Input source", {
        "source_select": 1, "freq_hz": 1000, "gain_db": -20, "enabled": True,
    }, "set_input_source")
    _stage_and_apply_advanced(e2e, "Frequency shifter", {
        "level": 2, "direction": 1, "enabled": True,
    }, "set_freq_shifter")
    assert {call[0] for call in e2e.device.writes} >= {
        "set_compressor", "set_limiter", "set_fir", "set_expander", "set_dynamic_eq_band",
        "set_input_source", "set_freq_shifter",
    }
    e2e.assert_clean_browser()


def test_presets_downloads_and_reviewed_operations(e2e: Scenario) -> None:
    """Preset staging, downloaded payload inspection, and reviewed copy/program destinations."""
    page = e2e.page
    e2e.connect()
    page.get_by_role("tab", name="Presets + device programs", exact=True).click()
    page.get_by_role("button", name="Export draft preset", exact=True).click()
    page.get_by_text("Local draft preset exported", exact=False).wait_for(timeout=20000)
    preset_path = e2e.directory / "downloads" / "draft-preset.json"
    _download(page, "draft-preset", preset_path)
    preset = json.loads(preset_path.read_text())
    assert preset["format"] == "sinbosen-la548-preset"
    assert e2e.device.writes == []

    staged = json.loads(preset_path.read_text())
    gain = next(entry for entry in staged["controls"]
                if entry["key"] == {"output": True, "channel": 0, "block": "gain", "index": -1})
    gain["value"] = -5.0
    staged_path = e2e.directory / "stage-preset.json"
    staged_path.write_text(json.dumps(staged))
    _upload_file(page, "#preset-import", staged_path)
    page.get_by_role("button", name="Stage preset", exact=True).click()
    page.get_by_text("Preset staged", exact=False).wait_for(timeout=20000)
    _wait_pending(e2e)
    assert e2e.device.writes == []
    incompatible = dict(staged)
    incompatible["profile"] = {**incompatible["profile"], "name": "not-this-profile"}
    incompatible_path = e2e.directory / "incompatible-preset.json"
    incompatible_path.write_text(json.dumps(incompatible))
    _upload_file(page, "#preset-import", incompatible_path)
    page.get_by_role("button", name="Stage preset", exact=True).click()
    page.get_by_text("Preset unchanged", exact=False).wait_for(timeout=20000)
    _wait_pending(e2e)
    page.get_by_role("button", name="Revert draft", exact=True).click()
    e2e.status("Draft reverted")

    page.get_by_role("button", name="Download device snapshot", exact=True).click()
    page.get_by_text("Actual device data downloaded", exact=False).wait_for(timeout=20000)
    snapshot_path = e2e.directory / "downloads" / "device-snapshot.bin"
    _download(page, "device-snapshot", snapshot_path)
    assert snapshot_path.read_bytes() == e2e.device.settings.raw
    page.get_by_role("button", name="Download selected channel FIR data", exact=True).click()
    page.get_by_text("Actual device data downloaded", exact=False).wait_for(timeout=20000)
    fir_path = e2e.directory / "downloads" / "out-1-fir.bin"
    _download(page, "fir", fir_path)
    impulse = struct.pack("<512f", 1.0, *([0.0] * 511))
    assert fir_path.read_bytes() == impulse

    bad_fir = e2e.directory / "bad-fir.bin"
    bad_fir.write_bytes(b"short")
    writes_before = len(e2e.device.writes)
    _upload_file(page, "#fir-restore-import", bad_fir)
    page.get_by_role("button", name="Restore selected channel FIR data", exact=True).click()
    page.get_by_text("must contain exactly 2048 bytes", exact=False).wait_for(timeout=20000)
    assert len(e2e.device.writes) == writes_before

    restored = struct.pack("<512f", 1.0, *([0.0] * 510), 0.0625)
    restore_path = e2e.directory / "restored-fir.bin"
    restore_path.write_bytes(restored)
    _upload_file(page, "#fir-restore-import", restore_path)
    page.get_by_role("button", name="Restore selected channel FIR data", exact=True).click()
    page.get_by_text("FIR data restored and verified for Out1", exact=False).wait_for(timeout=20000)
    assert e2e.device.fir_coefficients[True, 0] == restored
    assert e2e.device.writes[-1][0] == "write_fir_coefs"

    _number(page, "Program slot (0-29)", "2")
    page.get_by_role("radio", name="Save current device into slot", exact=True).check()
    page.get_by_role("button", name="Review destinations", exact=True).click()
    page.get_by_text("Save current device settings into slot 2", exact=False).wait_for(timeout=20000)
    page.get_by_label("I confirm the destinations shown above", exact=True).check()
    page.get_by_role("button", name="Execute reviewed operation", exact=True).click()
    page.get_by_text("Operation completed", exact=False).wait_for(timeout=20000)
    page.get_by_role("radio", name="Load slot into all channels", exact=True).check()
    page.get_by_role("button", name="Review destinations", exact=True).click()
    page.get_by_text("Load slot 2", exact=False).wait_for(timeout=20000)
    page.get_by_label("I confirm the destinations shown above", exact=True).check()
    page.get_by_role("button", name="Execute reviewed operation", exact=True).click()
    page.get_by_text("Operation completed", exact=False).wait_for(timeout=20000)

    page.get_by_role("radio", name="Copy selected channel", exact=True).check()
    page.locator("#program-copy-targets").get_by_role("radio", name="Out2", exact=True).check()
    page.get_by_role("button", name="Review destinations", exact=True).click()
    page.get_by_text("Copy Out1 onto Out2.", exact=True).wait_for(timeout=20000)
    page.get_by_label("I confirm the destinations shown above", exact=True).check()
    page.get_by_role("button", name="Execute reviewed operation", exact=True).click()
    page.get_by_text("Operation completed", exact=False).wait_for(timeout=20000)
    assert {call[0] for call in e2e.device.writes} >= {
        "write_fir_coefs", "save_program", "load_program", "channel_copy",
    }
    e2e.reveal(page.get_by_role("button", name="Execute reviewed operation", exact=True), "preset-management")
    e2e.assert_clean_browser()


def test_failures_conflicts_partial_apply_and_reconnect(e2e: Scenario) -> None:
    """Lock, external conflict, partial Apply, missing readback, disconnect, and safe reconnect behavior."""
    from playwright.sync_api import expect

    page = e2e.page
    e2e.connect()
    e2e.device.locked = True
    _number(page, "Delay (ms)", "3")
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("locked")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes: 1 controls")
    assert e2e.device.writes == []
    e2e.device.locked = False
    page.get_by_role("button", name="Revert draft", exact=True).click()
    _number(page, "Delay (ms)", "4")
    e2e.device.external_edit(ControlKey(True, 0, "delay"), 9.0)
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("External changes conflict")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes: 1 controls")
    page.get_by_role("button", name="Revert draft", exact=True).click()
    page.get_by_role("button", name="Refresh", exact=True).click()
    e2e.status("Readback refreshed")

    page.get_by_label("Mute", exact=True).check()
    _number(page, "Delay (ms)", "5")
    e2e.device.fail_next("set_delay", TimeoutError("injected partial apply"))
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("injected partial apply")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes: 1 controls")
    assert any(call[0] == "set_mute" for call in e2e.device.writes)
    _number(page, "Delay (ms)", "6")
    e2e.device.fail_verification_after("set_delay", TimeoutError("verification missing"))
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("Readback unavailable")
    expect(page.locator("#draft-status")).to_contain_text("Unapplied changes")
    e2e.device.link_up = False
    page.get_by_role("button", name="Apply changes", exact=True).click()
    e2e.status("connection is down")
    pending_writes = len(e2e.device.writes)
    e2e.device.link_up = True
    page.get_by_role("button", name="Connect", exact=True).click()
    e2e.status("Draft retained")
    assert len(e2e.device.writes) == pending_writes
    e2e.reveal(page.locator("#status"), "failure-reconnect")
    e2e.assert_clean_browser()
