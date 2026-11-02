"""Per-session trace overlays and explicit measured/electrical alignment views."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import gradio as gr
import plotly.graph_objects as go

from .plots import response_figure
from .response import Response, frequency_grid, path_response, sum_responses
from .service import ControlKey, DSPService
from .traces import Trace, load_trace, sum_traces


@dataclass(frozen=True)
class TraceView:
    trace: Trace
    name: str
    association: str = "all"
    visible: bool = True
    coherence_threshold: float = 0


class Measurements:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, TraceView]] = {}
        self._lock = threading.RLock()

    def drop(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def views(self, token: str) -> dict[str, TraceView]:
        with self._lock:
            return dict(self._sessions.get(token, {}))

    def add(self, token: str, path: str, association: str) -> str:
        trace = load_trace(path)
        with self._lock:
            self._sessions.setdefault(token, {})[trace.id] = TraceView(trace, trace.name, association)
        return trace.id

    def edit(self, token: str, identity: str, **updates: Any) -> None:
        with self._lock:
            entry = self._sessions[token][identity]
            threshold = updates.get("coherence_threshold", entry.coherence_threshold)
            if not 0 <= threshold <= 1:
                raise ValueError("coherence threshold must be 0–1")
            self._sessions[token][identity] = replace(entry, **updates)

    def remove(self, token: str, identity: str) -> None:
        with self._lock:
            self._sessions.get(token, {}).pop(identity, None)

    def overlays(self, token: str, channel: str) -> list[tuple[str, Response, str, str]]:
        return [(entry.name + " · measured", entry.trace.response(coherence_threshold=entry.coherence_threshold),
                 "#de5578", "solid") for entry in self.views(token).values()
                if entry.visible and entry.association in ("all", channel)]


class MeasurementUI:
    def __init__(self, measurements: Measurements, service: DSPService) -> None:
        self.measurements, self.service = measurements, service
        with gr.Tab("Measured traces"):
            gr.Markdown("Import a native OSM stored trace (.osm) or TXT/CSV/FRD/REW text. "
                        "Measurements remain independent of the DSP model.")
            self.file = gr.File(label="Import trace", file_types=[".osm", ".txt", ".csv", ".frd"], type="filepath",
                                elem_id="trace-import")
            self.import_button = gr.Button("Import and overlay", variant="primary")
            self.identity = gr.Dropdown(label="Trace", choices=[])
            with gr.Row():
                self.name = gr.Textbox(label="Display name")
                self.association = gr.Dropdown(label="Show on", choices=[("All channels", "all")]
                    + [(f"Output {i + 1}", f"out:{i}") for i in range(8)], value="all")
                self.visible = gr.Checkbox(label="Visible", value=True)
                self.threshold = gr.Slider(0, 1, step=0.01, label="Minimum coherence", value=0,
                                           elem_id="coherence-control")
            self.remove_button = gr.Button("Remove trace")
            self.info = gr.Markdown()
        with gr.Tab("Alignment"):
            gr.Markdown("Compare two electrical paths through a **common input**. "
                        "Measured sums require a shared timing and level reference.")
            with gr.Row():
                self.input = gr.Dropdown(choices=[(f"Input {chr(65 + i)}", i) for i in range(4)],
                                         value=0, label="Common input")
                self.a = gr.Dropdown(choices=list(range(1, 9)), value=1, label="Output A")
                self.b = gr.Dropdown(choices=list(range(1, 9)), value=2, label="Output B")
            with gr.Row():
                self.delay_a = gr.Number(label="A delay (ms)", minimum=0, maximum=1200)
                self.polarity_a = gr.Checkbox(label="Invert A")
                self.delay_b = gr.Number(label="B delay (ms)", minimum=0, maximum=1200)
                self.polarity_b = gr.Checkbox(label="Invert B")
            self.stage_button = gr.Button("Preview alignment changes")
            self.compare_button = gr.Button("Refresh electrical comparison")
            self.electrical = gr.Plot(label="Electrical paths and complex sum", elem_id="alignment-electrical")
            self.difference = gr.Plot(label="B minus A phase", elem_id="alignment-phase")
            self.alignment_info = gr.Markdown()
            with gr.Row():
                self.measured_a = gr.Dropdown(label="Measured A", choices=[])
                self.measured_b = gr.Dropdown(label="Measured B", choices=[])
            self.timing = gr.Checkbox(label="These traces share timing and level references", value=False)
            self.sum_button = gr.Button("Calculate measured sum")
            self.measured_plot = gr.Plot(label="Measured sum", elem_id="alignment-measured")
            self.measured_info = gr.Markdown()

    @property
    def panel_outputs(self) -> list[Any]:
        return [self.identity, self.name, self.association, self.visible, self.threshold, self.info,
                self.measured_a, self.measured_b]

    def panel(self, session: str, identity: str | None) -> dict[Any, Any]:
        entries = self.measurements.views(session)
        identity = identity if identity in entries else next(iter(entries), None)
        entry = entries.get(identity or "")
        choices = [(v.name, k) for k, v in entries.items()]
        return {self.identity: gr.update(choices=choices, value=identity),
                self.name: entry.name if entry else "", self.association: entry.association if entry else "all",
                self.visible: entry.visible if entry else True,
                self.threshold: entry.coherence_threshold if entry else 0,
                self.info: (f"**{entry.trace.format.upper()}** · {len(entry.trace.frequencies)} samples · "
                            + ("magnitude + phase" if entry.trace.phase_rad is not None else "magnitude only")
                            + "\n\n" + entry.trace.notes) if entry else "No imported traces.",
                self.measured_a: gr.update(choices=choices), self.measured_b: gr.update(choices=choices)}

    def bind(self, render: Callable[..., dict[Any, Any]], inputs: list[Any], outputs: list[Any],
             event: dict[str, Any]) -> None:
        combined = outputs + self.panel_outputs

        def import_trace(*args: Any) -> dict[Any, Any]:
            session, *rest, association, path = args
            try:
                if not path:
                    raise ValueError("choose a trace file")
                # Use the "Show on" choice, not the selected channel: an input
                # selection ("in:N") is not one of that dropdown's choices.
                identity = self.measurements.add(session, path, association or "all")
                return {**render(session, *rest), **self.panel(session, identity)}
            except Exception as exc:
                return {self.info: f"Import failed: {exc}"}

        def edit_trace(field: str, *args: Any) -> dict[Any, Any]:
            session, *rest, identity, value = args
            try:
                if identity:
                    self.measurements.edit(session, identity, **{field: value})
                result = render(session, *rest)
                # Do not echo earlier control values over a newer browser edit.
                # Only renaming changes selector labels; retain their current selections.
                if field == "name":
                    panel = self.panel(session, identity)
                    choice_update = dict(panel[self.identity])
                    choice_update.pop("value", None)
                    result.update({self.identity: choice_update, self.measured_a: panel[self.measured_a],
                                   self.measured_b: panel[self.measured_b]})
                return result
            except Exception as exc:
                return {self.info: f"Trace edit failed: {exc}"}

        def remove_trace(*args: Any) -> dict[Any, Any]:
            session, *rest, identity = args
            self.measurements.remove(session, identity)
            return {**render(session, *rest), **self.panel(session, None)}

        self.import_button.click(import_trace, inputs=inputs + [self.association, self.file], outputs=combined,
                                 **event)
        self.identity.input(self.panel, inputs=[inputs[0], self.identity], outputs=self.panel_outputs, **event)
        fields: list[tuple[Any, str]] = [(self.name, "name"), (self.association, "association"),
                                       (self.visible, "visible")]
        for component, field in fields:
            component.input(partial(edit_trace, field), inputs=inputs + [self.identity, component],
                            outputs=combined, **event)
        self.threshold.release(partial(edit_trace, "coherence_threshold"),
                               inputs=inputs + [self.identity, self.threshold], outputs=combined, **event)
        self.remove_button.click(remove_trace, inputs=inputs + [self.identity], outputs=combined, **event)

        align_inputs = [inputs[0], self.input, self.a, self.b, inputs[4], inputs[5]]
        align_outputs = [self.electrical, self.difference, self.alignment_info, self.delay_a, self.polarity_a,
                         self.delay_b, self.polarity_b]

        def alignment(session: str, input_id: int, a: int, b: int, unwrap: bool, low: bool) -> dict[Any, Any]:
            try:
                if a == b:
                    raise ValueError("select two different outputs")
                settings = self.service.view(session).draft
                grid = frequency_grid(sample_rate_hz=settings.profile.sample_rate_hz)
                ra, rb = (path_response(settings, input_id, output - 1, frequencies=grid) for output in (a, b))
                total = sum_responses(ra, rb)
                figure = response_figure([(f"Output {a}", ra, "#5368ed", "solid"),
                                          (f"Output {b}", rb, "#de5578", "solid"),
                                          ("Electrical sum", total, "#059b86", "dash")],
                                         unwrapped=unwrap, low_frequency=low, title="Common input comparison")
                difference = go.Figure(go.Scatter(x=grid, y=(rb.phase_unwrapped_deg - ra.phase_unwrapped_deg),
                                                  name="B − A", connectgaps=False))
                difference.update_layout(height=280, xaxis_type="log", xaxis_title="Frequency (Hz)",
                                         yaxis_title="Phase difference (°)", template="plotly_white")
                ca, cb = settings.outputs[a - 1], settings.outputs[b - 1]
                return {self.electrical: figure, self.difference: difference,
                        self.alignment_info: "\n\n".join(total.notes) or "Static electrical prediction.",
                        self.delay_a: ca.base.delay_ms, self.polarity_a: ca.base.inverted,
                        self.delay_b: cb.base.delay_ms, self.polarity_b: cb.base.inverted}
            except Exception as exc:
                return {self.alignment_info: f"Comparison unavailable: {exc}"}

        def stage_alignment(*args: Any) -> dict[Any, Any]:
            session, *rest, input_id, a, b, da, pa, db, pb = args
            try:
                if a == b:
                    raise ValueError("select two different outputs")
                self.service.stage_many(session, {ControlKey(True, a - 1, "delay"): da,
                                                 ControlKey(True, a - 1, "polarity"): pa,
                                                 ControlKey(True, b - 1, "delay"): db,
                                                 ControlKey(True, b - 1, "polarity"): pb})
                return {**render(session, *rest), **alignment(session, input_id, a, b, rest[3], rest[4])}
            except Exception as exc:
                return {self.alignment_info: f"Preview failed: {exc}"}

        self.compare_button.click(alignment, inputs=align_inputs, outputs=align_outputs, **event)
        for component in [self.input, self.a, self.b]:
            component.input(alignment, inputs=align_inputs, outputs=align_outputs, **event)
        self.stage_button.click(stage_alignment,
            inputs=inputs + [self.input, self.a, self.b, self.delay_a, self.polarity_a, self.delay_b, self.polarity_b],
            outputs=outputs + align_outputs, **event)

        def measured_sum(session: str, a: str, b: str, confirmed: bool, unwrap: bool, low: bool) -> dict[Any, Any]:
            try:
                if a == b:
                    raise ValueError("select two different traces")
                entries = self.measurements.views(session)
                ea, eb = entries[a], entries[b]
                total = sum_traces(ea.trace, eb.trace, timing_confirmed=confirmed,
                                   coherence_threshold=max(ea.coherence_threshold, eb.coherence_threshold))
                figure = response_figure([(ea.name, ea.trace.response(), "#5368ed", "solid"),
                                          (eb.name, eb.trace.response(), "#de5578", "solid"),
                                          ("Measured sum", total, "#059b86", "dash")],
                                         unwrapped=unwrap, low_frequency=low)
                return {self.measured_plot: figure, self.measured_info: "\n\n".join(total.notes)}
            except Exception as exc:
                return {self.measured_info: f"Measured sum unavailable: {exc}", self.measured_plot: None}

        self.sum_button.click(measured_sum,
            inputs=[inputs[0], self.measured_a, self.measured_b, self.timing, inputs[4], inputs[5]],
            outputs=[self.measured_plot, self.measured_info], **event)
