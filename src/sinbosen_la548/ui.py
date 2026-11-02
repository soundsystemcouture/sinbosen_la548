"""Local Gradio DSP application. Install with `uv sync --extra ui`."""

from __future__ import annotations

import argparse
import atexit
import json
import uuid
from collections.abc import Callable
from functools import partial
from importlib.resources import files
from typing import Any

import gradio as gr

from .advanced import AdvancedUI
from .dragging import DragEdits, pending_label
from .editing import NumericEdits, NumericKey, validate_number
from .exports import plot_archive
from .measurements import Measurements, MeasurementUI
from .plots import response_figure
from .response import channel_response, frequency_grid
from .service import ControlKey, DSPService, ServiceView
from .settings import ChannelSettings, DeviceSettings, decode_settings
from .tuning import ALLPASS_TYPES, EQ_TYPES, XOVER_TYPES, TuningController

CSS = """
.gradio-container { max-width: 1600px !important; margin: auto; }
#app-heading { margin-bottom: 0; }
#status { border-left: 4px solid #5368ed; padding-left: 16px; }
#channel-sidebar { position: sticky; top: 12px; align-self: flex-start; }
#draft-status { position: sticky; top: 0; z-index: 20; background: var(--background-fill-primary, white);
 border: 2px solid #5368ed; border-radius: 8px; padding: 10px 16px; }
.eq-handle-overlay [role=button]:focus { outline: none; filter: drop-shadow(0 0 3px #5368ed); }
.invalid-number input { border-color: #c53030 !important; box-shadow: 0 0 0 1px #c53030 !important; }
.field-error { color: #b42318; font-size: 0.85rem; margin-top: -6px; }
"""


def demo_settings() -> DeviceSettings:
    return decode_settings(files("sinbosen_la548").joinpath("data/demo_snapshot.bin").read_bytes())


def selected_channel(settings: DeviceSettings, selection: str) -> ChannelSettings:
    direction, index = selection.split(":")
    if direction not in ("in", "out"):
        raise ValueError("unknown channel direction")
    channels = settings.outputs if direction == "out" else settings.inputs
    if not 0 <= int(index) < len(channels):
        raise ValueError("channel out of range")
    return channels[int(index)]


class DSPApp(gr.Blocks):
    dsp_service: DSPService


def create_app(service: DSPService | None = None) -> DSPApp:
    service = DSPService(demo_settings()) if service is None else service
    tuning = TuningController(service)
    measurements = Measurements()
    numeric_edits = NumericEdits()
    drags = DragEdits(service)
    drag_requests: dict[str, str] = {}
    drag_values: dict[str, str] = {}
    def reset_editors(session: str) -> None:
        drags.cancel(session)
        tuning.reset(session)
        numeric_edits.drop(session)

    polled_values: dict[str, dict[Any, Any]] = {}
    def drop_session(session: str) -> None:
        drags.cancel(session)
        drag_requests.pop(session, None)
        drag_values.pop(session, None)
        numeric_edits.drop(session)
        polled_values.pop(session, None)
        measurements.drop(session)
        tuning.reset(session)
        for key in list(advanced_ui.selections):
            if key[0] == session:
                advanced_ui.selections.pop(key)
        advanced_ui.reviewed.pop(session, None)
        service.drop_session(session)
    app = DSPApp(title="LA548 · DSP Studio", analytics_enabled=False, fill_width=True)
    with app:
        token = gr.State(value=lambda: service.new_session(), time_to_live=3600,
                         delete_callback=drop_session)
        editor_context = gr.JSON(value=["out:0", 0], visible=False)
        gr.Markdown("# LA548 · DSP Studio", elem_id="app-heading")
        gr.Markdown("Crossovers, phase alignment, and precise DSP control. Connect to load your device settings.")
        with gr.Row():
            ip = gr.Textbox(label="DSP IP address", placeholder="192.168.1.100", scale=2)
            connect = gr.Button("Connect", variant="primary")
            disconnect = gr.Button("Disconnect")
            demo = gr.Button("Offline demo")
            refresh = gr.Button("Refresh")
            apply = gr.Button("Apply changes", variant="primary")
            revert = gr.Button("Revert draft")
            live = gr.Checkbox(label="Live tuning", value=False, elem_id="live-tuning")
        status = gr.Markdown("**Offline demo** · no hardware connection", elem_id="status")
        draft_status = gr.Markdown("**No unapplied changes.** · Offline demo — no device writes.",
                                   elem_id="draft-status")
        with gr.Row():
            with gr.Column(scale=1, min_width=180, elem_id="channel-sidebar"):
                selection = gr.Radio(choices=[(f"Input {chr(65 + i)}", f"in:{i}") for i in range(4)]
                                     + [(f"Output {i + 1}", f"out:{i}") for i in range(8)],
                                     value="out:0", label="Channel")
                channel_info = gr.Markdown(elem_id="channel-info")
                muted = gr.Checkbox(label="Mute")
                inverted = gr.Checkbox(label="Invert polarity")
                gain = gr.Slider(-80, 12, step=0.1, label="Gain (dB)", value=0, elem_id="gain-control")
                delay = gr.Number(label="Delay (ms)", value=0, step=0.02, elem_id="channel-delay")
                delay_error = gr.Markdown(visible=False, elem_classes="field-error", elem_id="delay-error")
            with gr.Column(scale=5):
                with gr.Row():
                    individual = gr.Checkbox(label="Individual filters", value=True)
                    overlay = gr.Checkbox(label="Readback overlay", value=True)
                    unwrapped = gr.Checkbox(label="Unwrapped phase", value=False)
                    low_frequency = gr.Checkbox(label="Subwoofer zoom", value=False)
                plot = gr.Plot(label="Magnitude · phase · group delay", elem_id="response-main")
                def preview_drag(data: dict[str, Any]) -> dict[str, Any]:
                    try:
                        context, view, channel = drags.preview(str(data["session"]), str(data["id"]),
                                                              float(data["frequency"]))
                        figure, _ = figure_for(context.session, view, channel, context.choice,
                                               *context.options, preview=True)
                        return {"sequence": data["sequence"], "figure": figure.to_json()}
                    except (ValueError, KeyError, TypeError) as exc:
                        return {"sequence": data.get("sequence"), "error": str(exc)}

                drag_controls = gr.HTML(
                    value="{}", elem_id="eq-drag-controller",
                    html_template='<p>Drag EQ points left or right to change frequency. '
                                  'Set gain and Q in the editor below. Graph edits require Apply.</p>'
                                  '<p class="drag-message" role="status" aria-live="polite"></p>',
                    js_on_load=files("sinbosen_la548").joinpath("eq_drag.js").read_text(),
                    server_functions=[preview_drag])
                coverage = gr.Markdown()
                with gr.Row():
                    export_plot = gr.Button("Export response plots")
                    plot_download = gr.DownloadButton("Download response ZIP", interactive=False)
                with gr.Tabs():
                    with gr.Tab("Crossovers + EQ / allpass"):
                        xover_controls: dict[str, list[Any]] = {}
                        xover_errors: dict[str, Any] = {}
                        with gr.Row(elem_id="crossover-controls") as xover_row:
                            for band, title in [("hp", "High-pass"), ("lp", "Low-pass")]:
                                with gr.Column(elem_id=f"xover-{band}"):
                                    gr.Markdown(f"### {title} crossover")
                                    enabled = gr.Checkbox(label=f"Enable {title.lower()}")
                                    with gr.Row():
                                        with gr.Column(min_width=140):
                                            frequency = gr.Number(label="Corner frequency (Hz)",
                                                                  step=1, elem_id=f"{band}-frequency")
                                            xover_errors[band] = gr.Markdown(
                                                visible=False, elem_classes="field-error",
                                                elem_id=f"{band}-frequency-error")
                                        family = gr.Dropdown(
                                            choices=[(name, value) for value, name in XOVER_TYPES.items()],
                                            label="Family", value=0, min_width=140)
                                        slope = gr.Dropdown(choices=list(range(6, 49, 6)), label="Slope (dB/oct)",
                                                            value=6, min_width=100)
                                    xover_controls[band] = [enabled, frequency, family, slope]
                        gr.Markdown("### EQ + allpass\nSelect a band below. Allpass changes phase and group delay.")
                        with gr.Row(elem_id="eq-actions"):
                            eq_index = gr.Dropdown(choices=[(f"Band {i + 1}", i) for i in range(15)],
                                                   value=0, label="Selected band")
                            add_allpass = gr.Button("Add allpass", variant="primary")
                            bypass = gr.Button("Bypass all EQ")
                            restore = gr.Button("Restore EQ")
                        eq_table = gr.Dataframe(headers=["Band", "Filter", "Hz", "Gain dB", "Q", "Enabled"],
                                               datatype=["number", "str", "number", "number", "number", "bool"],
                                               type="array", interactive=False, column_count=6, max_height=280)
                        with gr.Row(elem_id="eq-band-editor"):
                            eq_enabled = gr.Checkbox(label="Band enabled")
                            eq_type = gr.Dropdown(choices=[(name, value) for value, name in EQ_TYPES.items()],
                                                  label="Filter", value=0)
                            with gr.Column(min_width=140):
                                eq_freq = gr.Number(label="Frequency (Hz)", step=1, elem_id="eq-frequency")
                                eq_freq_error = gr.Markdown(visible=False, elem_classes="field-error",
                                                            elem_id="eq-frequency-error")
                            eq_gain = gr.Slider(-20, 20, step=0.1, label="Gain (dB)", value=0, elem_id="eq-gain")
                            with gr.Column(min_width=100):
                                eq_q = gr.Number(label="Q", step=0.0001, elem_id="eq-q")
                                eq_q_error = gr.Markdown(visible=False, elem_classes="field-error",
                                                         elem_id="eq-q-error")
                    with gr.Tab("Mixer"):
                        with gr.Group(elem_id="mixer-grid"):
                            gr.Markdown("### Routing matrix\nEach switch connects one input to one output.")
                            routes: list[Any] = []
                            for i in range(4):
                                with gr.Row():
                                    gr.Markdown(f"**Input {chr(65 + i)}**")
                                    for o in range(8):
                                        routes.append(gr.Checkbox(label=f"→ {o + 1}", min_width=65,
                                                                  elem_id=f"mixer-route-{i}-{o}"))
                    measurement_ui = MeasurementUI(measurements, service)
                    advanced_ui = AdvancedUI(service, reset_editors)
        with gr.Accordion("Pending changes", open=False):
            pending = gr.Dataframe(headers=["Control", "Before", "Draft"], interactive=False,
                                   type="array", column_count=3, datatype="str")

        outputs: list[Any] = [status, live, pending, plot, coverage, channel_info, muted, inverted, gain, delay,
                              selection, xover_row, eq_index, eq_table, eq_enabled, eq_type, eq_freq, eq_gain, eq_q]
        outputs += [c for group in xover_controls.values() for c in group] + routes
        outputs += advanced_ui.outputs
        numeric_controls: dict[str, tuple[Any, Any, float, float, str, str, bool]] = {
            "delay": (delay, delay_error, 0, 1200, "Delay", "ms", False),
            "eq_freq": (eq_freq, eq_freq_error, 10, 20000, "Frequency", "Hz", True),
            "eq_q": (eq_q, eq_q_error, 0.404, 28.853, "Q", "", False),
            "hp_freq": (xover_controls["hp"][1], xover_errors["hp"], 10, 20000, "Frequency", "Hz", True),
            "lp_freq": (xover_controls["lp"][1], xover_errors["lp"], 10, 20000, "Frequency", "Hz", True),
        }
        outputs += [config[1] for config in numeric_controls.values()]
        outputs += [draft_status, drag_controls, editor_context]

        def numeric_key(choice: str, index: int, field: str) -> NumericKey:
            return NumericKey(choice, field, int(index or 0) if field.startswith("eq_") else -1)

        def numeric_feedback(session: str, key: NumericKey, *, value: Any) -> dict[Any, Any]:
            component, error_component, *_ = numeric_controls[key.field]
            edit = numeric_edits.get(session, key)
            error = edit.error if edit else ""
            update = gr.update(elem_classes=["invalid-number"] if error else [])
            update["value"] = edit.value if edit else value
            return {component: update, error_component: gr.update(value=error, visible=bool(error))}

        status_outputs = [status, live, pending, draft_status]
        response_outputs = [plot, coverage]
        band_outputs = [eq_index, eq_enabled, eq_type, eq_freq, eq_gain, eq_q]
        scopes: dict[str, list[Any]] = {
            "status": status_outputs,
            "plot": status_outputs + response_outputs,
            "base": status_outputs + response_outputs + [muted, inverted, gain, delay],
            "band": band_outputs + [drag_controls, editor_context],
            "eq": status_outputs + response_outputs + band_outputs + [eq_table, editor_context],
            "xover": status_outputs + response_outputs + [c for group in xover_controls.values() for c in group],
            "route": status_outputs + routes,
        }

        def figure_for(session: str, view: ServiceView, ch: ChannelSettings, choice: str, show_individual: bool,
                       show_readback: bool, unwrap: bool, low: bool, *, preview: bool = False) -> tuple[Any, str]:
            frequencies = frequency_grid(sample_rate_hz=view.draft.profile.sample_rate_hz,
                                         points=1024) if preview else None
            response = channel_response(ch, frequencies=frequencies,
                                        sample_rate_hz=view.draft.profile.sample_rate_hz)
            traces = [("Draft · combined", response.total, "#5368ed", "solid")]
            traces += measurements.overlays(session, choice)
            if show_readback:
                baseline = selected_channel(view.baseline, choice)
                traces.append(("Last readback", channel_response(baseline, frequencies=frequencies,
                              sample_rate_hz=view.baseline.profile.sample_rate_hz).total, "#94a3b8", "dash"))
            if show_individual:
                for i, (name, r) in enumerate(response.contributions[1:]):
                    if r.complete:
                        traces.append((name, r, ("#ec8a37", "#8b5cf6", "#059b86")[i % 3], "dot"))
            detail = "Complete static model" if response.total.complete else "Incomplete model · see omitted stages"
            return (response_figure(traces, unwrapped=unwrap, low_frequency=low, title=ch.name),
                    "**" + detail + "**\n\n" + "\n\n".join(response.total.notes))

        def drag_metadata(session: str, choice: str, index: int) -> str:
            view = service.view(session)
            ch = selected_channel(view.draft, choice)
            context = drags.current(session, choice)
            return json.dumps({"session": session, "choice": choice, "revision": view.revision, "selected": index,
                               "request": drag_requests.get(session), "active": context.id if context else None,
                               "handles": [{"band": i, "frequency": band.freq_hz,
                                            "gain": 0 if band.type in ALLPASS_TYPES else band.gain_db,
                                            "q": round(band.q, 4), "type": EQ_TYPES[band.type],
                                            "enabled": band.enabled, "selected": i == index,
                                            "allpass": band.type in ALLPASS_TYPES}
                                           for i, band in enumerate(ch.eq_bands)
                                           if band.type in EQ_TYPES and (band.enabled or i == index)]})

        def render(session: str, choice: str, show_individual: bool, show_readback: bool,
                   unwrap: bool, low: bool, index: int, *, scope: str = "all") -> dict[Any, Any]:
            context = drags.current(session, choice)
            # Only an explicit choice of another band (dropdown or table row)
            # cancels a drag; other events can carry a stale index.
            if context and (context.options != (show_individual, show_readback, unwrap, low)
                            or (scope == "band" and drags.is_new_selection(context, int(index or 0)))):
                drags.cancel(session)
            view = service.view(session)
            ch = selected_channel(view.draft, choice)
            figure, model_coverage = figure_for(session, view, ch, choice, show_individual, show_readback, unwrap, low)
            mode = "Offline demo" if view.demo else "Connected" if view.connected else "Disconnected"
            index = min(max(int(index or 0), 0), len(ch.eq_bands) - 1)
            eq = ch.eq_bands[index]
            types = [(name, value) for value, name in EQ_TYPES.items()]
            if eq.type not in EQ_TYPES:
                types.append((f"Unresolved type {eq.type}", eq.type))
            result = {
                status: f"**{mode}** · {len(view.pending)} pending · {view.message}",
                live: view.live,
                pending: [[c.key.label, str(c.before), str(c.after)] for c in view.pending],
                draft_status: pending_label(view, unfinished=numeric_edits.has_unfinished(session)),
                drag_controls: drag_metadata(session, choice, index),
                plot: figure,
                coverage: model_coverage,
                channel_info: f"**{ch.name}**\n\n{len(ch.eq_bands)} EQ slots · "
                              f"{view.draft.profile.sample_rate_hz / 1000:g} kHz (provisional)",
                muted: ch.base.muted, inverted: ch.base.inverted, gain: ch.base.gain_db, delay: ch.base.delay_ms,
                selection: gr.update(choices=[(c.name, f"{'out' if c.output else 'in'}:{c.channel_id}")
                                              for c in (*view.draft.inputs, *view.draft.outputs)], value=choice),
                xover_row: gr.update(visible=ch.output),
                eq_index: gr.update(choices=[(f"Band {i + 1}", i) for i in range(len(ch.eq_bands))], value=index),
                eq_table: [[i + 1, EQ_TYPES.get(b.type, f"Unresolved ({b.type})"), b.freq_hz,
                            b.gain_db, round(b.q, 4), b.enabled] for i, b in enumerate(ch.eq_bands)],
                eq_enabled: eq.enabled, eq_type: gr.update(choices=types, value=eq.type), eq_freq: eq.freq_hz,
                eq_gain: gr.update(value=eq.gain_db, interactive=eq.type not in ALLPASS_TYPES), eq_q: round(eq.q, 4),
                editor_context: [choice, index],
            }
            for band, group in xover_controls.items():
                block = ch.hp if band == "hp" else ch.lp
                if block is None:
                    continue
                slope_choices = list(range(6, 49, 6))
                if block.slope_db_per_oct not in slope_choices:
                    slope_choices.append(block.slope_db_per_oct)
                families = [(name, value) for value, name in XOVER_TYPES.items()]
                if block.filter_type not in XOVER_TYPES:
                    families.append((f"Unresolved {block.filter_type}", block.filter_type))
                result.update({group[0]: block.enabled, group[1]: block.freq_hz,
                               group[2]: gr.update(choices=families, value=block.filter_type),
                               group[3]: gr.update(choices=slope_choices, value=block.slope_db_per_oct)})
            result.update({component: view.draft.mix_matrix[i][o]
                           for i in range(4) for o in range(8) for component in [routes[i * 8 + o]]})
            if scope == "all":
                result.update(advanced_ui.render(session, choice))
            else:
                result = {component: value for component, value in result.items()
                          if component in scopes[scope] or component == drag_controls}
            for field, config in numeric_controls.items():
                component = config[0]
                if component in result:
                    result.update(numeric_feedback(session, numeric_key(choice, index, field), value=result[component]))
            if status in result:
                polled_values[session] = {component: result[component] for component in status_outputs}
            if drag_controls in result:
                drag_values[session] = result[drag_controls]
            return result

        render_inputs = [token, selection, individual, overlay, unwrapped, low_frequency, eq_index]

        def drain_numeric(session: str, *, force: bool = False, key: NumericKey | None = None) -> list[NumericKey]:
            processed = []
            for current, edit in numeric_edits.ready(session, force=force, key=key):
                _, _, minimum, maximum, label, unit, integer = numeric_controls[current.field]
                try:
                    value = validate_number(edit.value, minimum=minimum, maximum=maximum,
                                            label=label, unit=unit, integer=integer)
                    ch = selected_channel(service.view(session).draft, current.selection)
                    if current.field == "delay":
                        service.stage(session, ControlKey(ch.output, ch.channel_id, "delay"), value, immediate=True)
                    elif current.field.startswith("eq_"):
                        tuning.eq(session, ch.output, ch.channel_id, current.band,
                                  "freq_hz" if current.field == "eq_freq" else "q", value, immediate=True)
                    else:
                        if not ch.output:
                            raise ValueError("Crossovers are available on output channels only.")
                        tuning.crossover(session, ch.channel_id, current.field[:2], "freq_hz", value, immediate=True)
                except (ValueError, TypeError) as exc:
                    numeric_edits.finish(session, current, str(exc))
                else:
                    numeric_edits.finish(session, current)
                processed.append(current)
            return processed

        def queue_numeric(field: str, session: str, choice: str, index: int, value: float | None) -> dict[Any, Any]:
            key = numeric_key(choice, index, field)
            numeric_edits.queue(session, key, value)
            component, error_component, *_ = numeric_controls[field]
            return {component: gr.update(elem_classes=[]), error_component: gr.update(value="", visible=False),
                    draft_status: pending_label(service.view(session), unfinished=True)}

        def commit_numeric(field: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                           index: int, value: float | None) -> dict[Any, Any]:
            key = numeric_key(choice, index, field)
            numeric_edits.queue(session, key, value)
            drain_numeric(session, force=True, key=key)
            scope = "base" if field == "delay" else "eq" if field.startswith("eq_") else "xover"
            result = render(session, choice, si, sr, uw, lo, index, scope=scope)
            component, error_component, *_ = numeric_controls[field]
            keep = set(status_outputs + response_outputs + [component, error_component])
            if field.startswith("eq_"):
                keep.update([eq_table, drag_controls])
            return {output: update for output, update in result.items() if output in keep}

        numeric_fields = tuple(numeric_controls)
        numeric_components = [numeric_controls[field][0] for field in numeric_fields]

        def sync_current_numeric(session: str, choice: str, index: int, context: list[Any] | tuple[Any, ...],
                                 *values: float | None) -> None:
            expected = (choice, int(index or 0))
            if tuple(context) != expected:
                raise ValueError("Controls are still updating for the selected channel; try Apply again.")
            ch = selected_channel(service.view(session).draft, choice)
            displayed = {
                "delay": ch.base.delay_ms,
                "eq_freq": ch.eq_bands[expected[1]].freq_hz,
                "eq_q": round(ch.eq_bands[expected[1]].q, 4),
                "hp_freq": ch.hp.freq_hz if ch.hp else None,
                "lp_freq": ch.lp.freq_hz if ch.lp else None,
            }
            for field, value in zip(numeric_fields, values, strict=True):
                if field in ("hp_freq", "lp_freq") and not ch.output:
                    continue
                key = numeric_key(choice, index, field)
                if numeric_edits.get(session, key) is None and value == displayed[field]:
                    continue
                numeric_edits.queue(session, key, value)
            drain_numeric(session, force=True)

        def run(session: str, choice: str, show_individual: bool, show_readback: bool, unwrap: bool, low: bool,
                index: int, operation: Callable[[], object], *, scope: str = "all") -> dict[Any, Any]:
            error = None
            try:
                operation()
            except Exception as exc:
                error = str(exc)
                service.report_error(session, exc)
            result = render(session, choice, show_individual, show_readback, unwrap, low, index, scope=scope)
            if error:
                result[status] = "**Action failed — draft retained.** " + error
            return result

        def act(name: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                index: int) -> dict[Any, Any]:
            drags.cancel(session)
            operation = {"apply": service.apply, "revert": service.revert, "refresh": service.refresh,
                         "demo": service.demo}[name]
            if name == "apply":
                drain_numeric(session, force=True)
                if numeric_edits.has_errors(session):
                    service.report_error(session, ValueError(
                        "Correct highlighted values before Apply; check other channels and EQ bands as well."))
                    return render(session, choice, si, sr, uw, lo, index)
            def perform() -> None:
                operation(session)
                if name in ("revert", "demo"):
                    numeric_edits.drop(session)
                    tuning.reset(session)
            return run(session, choice, si, sr, uw, lo, index, perform)

        def apply_current(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool, index: int,
                          context: list[Any] | tuple[Any, ...], *values: float | None) -> dict[Any, Any]:
            try:
                sync_current_numeric(session, choice, index, context, *values)
            except (TypeError, ValueError) as exc:
                service.report_error(session, exc)
                return render(session, choice, si, sr, uw, lo, index)
            return act("apply", session, choice, si, sr, uw, lo, index)

        def connect_device(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                           index: int, host: str) -> dict[Any, Any]:
            drags.cancel(session)
            def perform() -> None:
                service.connect(session, host.strip())
                tuning.reset(session)
            return run(session, choice, si, sr, uw, lo, index, perform)

        def disconnect_device(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                              index: int) -> dict[Any, Any]:
            drags.cancel(session)
            return run(session, choice, si, sr, uw, lo, index, service.close)

        def edit_base(block: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                      index: int, value: Any) -> dict[Any, Any]:
            ch = selected_channel(service.view(session).draft, choice)
            result = run(session, choice, si, sr, uw, lo, index,
                         lambda: service.stage(session, ControlKey(ch.output, ch.channel_id, block), value,
                                               immediate=block != "delay"), scope="base")
            component = {"mute": muted, "polarity": inverted, "gain": gain, "delay": delay}[block]
            keep = set(status_outputs + response_outputs + [component])
            return {output: update for output, update in result.items() if output in keep}

        def toggle_live(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                        index: int, value: bool, context: list[Any] | tuple[Any, ...],
                        *values: float | None) -> dict[Any, Any]:
            drags.cancel(session)
            if value:
                try:
                    sync_current_numeric(session, choice, index, context, *values)
                except (TypeError, ValueError) as exc:
                    service.report_error(session, exc)
                    return render(session, choice, si, sr, uw, lo, index)
                if numeric_edits.has_errors(session):
                    service.report_error(session, ValueError("Correct highlighted values before enabling Live."))
                    return render(session, choice, si, sr, uw, lo, index)
            return run(session, choice, si, sr, uw, lo, index, lambda: service.set_live(session, value), scope="status")

        def edit_eq(field: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                    index: int, value: Any) -> dict[Any, Any]:
            ch = selected_channel(service.view(session).draft, choice)
            result = run(session, choice, si, sr, uw, lo, index,
                         lambda: tuning.eq(session, ch.output, ch.channel_id, index, field, value), scope="eq")
            components: list[Any]
            if field == "enabled":
                components = [eq_enabled]
            elif field == "type":
                components = [eq_type, eq_gain]
            else:
                components = [eq_gain]
            keep = set(status_outputs + response_outputs + components + [eq_table, drag_controls])
            return {output: update for output, update in result.items() if output in keep}

        def edit_xover(band: str, field: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                       index: int, value: Any) -> dict[Any, Any]:
            ch = selected_channel(service.view(session).draft, choice)
            def operation() -> None:
                if not ch.output:
                    raise ValueError("crossovers are output-only")
                tuning.crossover(session, ch.channel_id, band, field, value)
            result = run(session, choice, si, sr, uw, lo, index, operation, scope="xover")
            component = xover_controls[band][{"enabled": 0, "filter_type": 2, "slope_db_per_oct": 3}[field]]
            keep = set(status_outputs + response_outputs + [component])
            return {output: update for output, update in result.items() if output in keep}

        def eq_action(action: str, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                      index: int) -> dict[Any, Any]:
            ch = selected_channel(service.view(session).draft, choice)
            if action == "add":
                try:
                    index = tuning.add_allpass(session, ch.output, ch.channel_id)
                except ValueError as exc:
                    result = render(session, choice, si, sr, uw, lo, index, scope="eq")
                    result[status] = str(exc)
                    return result
                return render(session, choice, si, sr, uw, lo, index, scope="eq")
            return run(session, choice, si, sr, uw, lo, index,
                       lambda: tuning.bypass(session, ch.output, ch.channel_id, restore=action == "restore"),
                       scope="eq")

        def edit_route(i: int, o: int, session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool,
                       index: int, value: bool) -> dict[Any, Any]:
            result = run(session, choice, si, sr, uw, lo, index,
                         lambda: service.stage(session, ControlKey(False, i, "route", o), value, immediate=True),
                         scope="route")
            keep = set(status_outputs + [routes[i * 8 + o]])
            return {output: update for output, update in result.items() if output in keep}

        def table_select(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool, index: int,
                         event_data: gr.SelectData) -> dict[Any, Any]:
            selected = event_data.index
            row = selected[0] if isinstance(selected, (tuple, list)) else selected
            return render(session, choice, si, sr, uw, lo, int(row), scope="band")

        def begin_drag(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool, index: int,
                       event_data: gr.EventData) -> dict[Any, Any]:
            data = event_data._data
            band = int(data["band"])
            if data.get("action") == "begin":
                drag_requests[session] = str(data["request"])
            def operation() -> None:
                if data.get("action") == "begin":
                    view = service.view(session)
                    if data.get("choice") != choice or data.get("revision") != view.revision:
                        raise ValueError("plot changed; select the band and try again")
                    drags.begin(session, choice, band, (si, sr, uw, lo), selected=int(index or 0))
                    numeric_edits.discard(session, numeric_key(choice, band, "eq_freq"))
            result = run(session, choice, si, sr, uw, lo, band, operation, scope="eq")
            # Selection/begin must not redraw the response underneath a held pointer.
            result.pop(plot, None)
            result.pop(coverage, None)
            return result

        def finish_drag(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool, index: int,
                        event_data: gr.EventData) -> dict[Any, Any]:
            data = event_data._data
            def operation() -> None:
                if data["action"] == "finish":
                    context = drags.current(session, choice)
                    if context is None or context.id != data["id"]:
                        raise ValueError("graph edit expired; no frequency change staged")
                    drags.finish(session, str(data["id"]), float(data["frequency"]))
                else:
                    context = drags.current(session, choice)
                    if context and context.id == data["id"]:
                        drags.cancel(session)
            result = run(session, choice, si, sr, uw, lo, index, operation, scope="eq")
            # A cancelled/disabled-band preview can have identical server curve data.
            # Force Gradio to refresh its figure after client-side Plotly edits.
            result[plot].update_layout(datarevision=uuid.uuid4().hex)
            return result

        event: dict[str, Any] = {"concurrency_id": "dsp-ui", "concurrency_limit": 1,
                                 "api_visibility": "private", "show_progress": "hidden"}
        device_event = {**event, "show_progress": "minimal", "show_progress_on": status}
        app.load(render, inputs=render_inputs, outputs=outputs, **event)
        drag_controls.drag_start(begin_drag, inputs=render_inputs, outputs=outputs, trigger_mode="multiple", **event)
        drag_controls.drag_end(finish_drag, inputs=render_inputs, outputs=outputs, trigger_mode="multiple", **event)
        selection.input(render, inputs=render_inputs, outputs=outputs, **event)
        for component in [individual, overlay, unwrapped, low_frequency]:
            component.input(partial(render, scope="plot"), inputs=render_inputs, outputs=outputs, **event)
        eq_index.input(partial(render, scope="band"), inputs=render_inputs, outputs=outputs, **event)
        connect.click(connect_device, inputs=render_inputs + [ip], outputs=outputs, **device_event)
        disconnect.click(disconnect_device, inputs=render_inputs, outputs=outputs, **device_event)
        apply.click(apply_current, inputs=render_inputs + [editor_context] + numeric_components,
                    outputs=outputs, **device_event)
        for button, name in [(revert, "revert"), (refresh, "refresh"), (demo, "demo")]:
            button.click(partial(act, name), inputs=render_inputs, outputs=outputs, **device_event)
        base_components: list[tuple[Any, str]] = [(muted, "mute"), (inverted, "polarity")]
        for component, block in base_components:
            component.input(partial(edit_base, block), inputs=render_inputs + [component], outputs=outputs, **event)
        gain.release(partial(edit_base, "gain"), inputs=render_inputs + [gain], outputs=outputs, **event)
        live.input(toggle_live, inputs=render_inputs + [live, editor_context] + numeric_components,
                   outputs=outputs, **event)
        eq_components: list[tuple[Any, str]] = [(eq_enabled, "enabled"), (eq_type, "type")]
        for component, field in eq_components:
            component.input(partial(edit_eq, field), inputs=render_inputs + [component], outputs=outputs, **event)
        eq_gain.release(partial(edit_eq, "gain_db"), inputs=render_inputs + [eq_gain], outputs=outputs, **event)
        eq_table.select(table_select, inputs=render_inputs, outputs=outputs, **event)
        for button, name in [(add_allpass, "add"), (bypass, "bypass"), (restore, "restore")]:
            button.click(partial(eq_action, name), inputs=render_inputs, outputs=outputs, **event)
        for band, group in xover_controls.items():
            for component, field in zip(group, ["enabled", "freq_hz", "filter_type", "slope_db_per_oct"], strict=True):
                if field == "freq_hz":
                    continue
                component.input(partial(edit_xover, band, field), inputs=render_inputs + [component],
                                outputs=outputs, **event)
        for field, config in numeric_controls.items():
            component, error_component = config[:2]
            component.input(partial(queue_numeric, field), inputs=[token, selection, eq_index, component],
                            outputs=[component, error_component, draft_status], **event)
            for commit_event in [component.submit, component.blur]:
                commit_event(partial(commit_numeric, field), inputs=render_inputs + [component],
                             outputs=outputs, **event)
        for i in range(4):
            for o in range(8):
                routes[i * 8 + o].input(partial(edit_route, i, o), inputs=render_inputs + [routes[i * 8 + o]],
                                        outputs=outputs, **event)

        measurement_ui.bind(partial(render, scope="plot"), render_inputs, outputs, event)
        advanced_ui.bind(render, render_inputs, outputs, event)

        def export_response(*args: Any) -> Any:
            result = render(*args)
            path = advanced_ui.file("response.zip", plot_archive(result[plot], result[coverage]))
            return gr.update(value=path, interactive=True)

        export_plot.click(export_response, inputs=render_inputs, outputs=[plot_download], **event)

        def tick(session: str, choice: str, si: bool, sr: bool, uw: bool, lo: bool, index: int) -> dict[Any, Any]:
            error = None
            applied = None
            processed = drain_numeric(session)
            try:
                applied = service.flush_live(session)
            except Exception as exc:
                error = str(exc)
                service.set_live(session, False)
            view = service.view(session)
            mode = "Offline demo" if view.demo else "Connected" if view.connected else "Disconnected"
            result = {status: f"**{mode}** · {len(view.pending)} pending · {error or view.message}", live: view.live,
                      pending: [[c.key.label, str(c.before), str(c.after)] for c in view.pending],
                      draft_status: pending_label(view, unfinished=numeric_edits.has_unfinished(session))}
            previous = polled_values.get(session, {})
            polled_values[session] = result.copy()
            result = {component: value for component, value in result.items() if previous.get(component) != value}
            metadata = drag_metadata(session, choice, int(index or 0))
            if drag_values.get(session) != metadata:
                result[drag_controls] = metadata
                drag_values[session] = metadata
            if applied is not None:
                rendered = render(session, choice, si, sr, uw, lo, index, scope="plot")
                result.update({plot: rendered[plot], coverage: rendered[coverage]})
            if processed:
                rendered = render(session, choice, si, sr, uw, lo, index, scope="eq")
                if any(numeric_edits.get(session, key) is None for key in processed):
                    result.update({component: rendered[component] for component in status_outputs + response_outputs})
                    if any(key.selection == choice and key.field.startswith("eq_") for key in processed):
                        result[eq_table] = rendered[eq_table]
                ch = selected_channel(view.draft, choice)
                index = min(max(int(index or 0), 0), len(ch.eq_bands) - 1)
                values = {"delay": ch.base.delay_ms, "eq_freq": ch.eq_bands[index].freq_hz,
                          "eq_q": round(ch.eq_bands[index].q, 4),
                          "hp_freq": ch.hp.freq_hz if ch.hp else None,
                          "lp_freq": ch.lp.freq_hz if ch.lp else None}
                for key in processed:
                    if key == numeric_key(choice, index, key.field):
                        result.update(numeric_feedback(session, key, value=values[key.field]))
            return result or gr.skip()

        gr.Timer(0.25).tick(tick, inputs=render_inputs, outputs=outputs, **event)
    app.queue(max_size=64, api_open=False)
    # Retain service for tests/embedding and close sockets on interpreter shutdown.
    app.dsp_service = service
    atexit.register(service.close)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local LA548 DSP Studio (requires the ui extra)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--demo", action="store_true", help="start offline (also the default)")
    args = parser.parse_args()
    create_app().launch(server_name=args.host, server_port=args.port, share=False, inbrowser=False,
                        footer_links=[], theme=gr.themes.Soft(), css=CSS)


if __name__ == "__main__":
    main()
