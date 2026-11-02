"""Capability-aware advanced editors and reviewed preset/device operations."""

from __future__ import annotations

import atexit
import math
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import gradio as gr

from .fir_adapter import FIRParameters
from .presets import export_preset, import_preset
from .service import ControlKey, DSPService, controls
from .settings import ChannelSettings, DeviceSettings, FIRSnapshot

BLOCK_NAMES = {"compressor": "Compressor", "limiter": "Limiter", "expander": "Expander",
               "dyneq": "Dynamic EQ", "input_source": "Input source", "frequency_shifter": "Frequency shifter",
               "fir": "FIR parameters"}
HELP = {
    "compressor": "Output compressor. Dynamics depend on signal level and are shown separately from static plots.",
    "limiter": "Output limiter. Detection 0 is average-sensitive; 1 is peak-sensitive.",
    "expander": "Input expander. Disabled readback can be outside editable ranges; set valid values before staging.",
    "dyneq": "Input dynamic EQ: types 0 EQ, 1 low shelf, 2 high shelf, 3 AllPass1, 4 AllPass2; "
             "slope 0 = 6 dB, 1 = 12 dB; startup 0 above threshold, 1 below. Not a static filter model.",
    "input_source": "Source 0 external analog input, 1 internal sine generator (freq_hz sets its pitch), "
                    "2 internal pink noise, 3 internal white noise. Selecting 1–3 replaces the external "
                    "signal entirely.",
    "frequency_shifter": "Input only. Level 0–5; direction 0 down, 1 up. Omitted from static response plots.",
    "fir": "FIR type 0 low-pass (freq_low_hz is the corner), 1 high-pass (freq_high_hz is the corner), "
           "2 band-pass, 3 band-stop, 4 all-pass. Window indices 0–11 follow the manufacturer's app's list; "
           "0 is the Kaiser candidate. On the DA0408X, order index 0 selects 256 taps and 1 selects 512 taps "
           "for both inputs and outputs. Coefficient backups can be downloaded and restored; enabled FIR makes "
           "plotted totals incomplete.",
}


def channel(settings: DeviceSettings, selection: str) -> ChannelSettings:
    direction, index = selection.split(":")
    return (settings.outputs if direction == "out" else settings.inputs)[int(index)]


def block_value(service: DSPService, session: str, selection: str, block: str, index: int) -> tuple[ControlKey, Any]:
    ch = channel(service.view(session).draft, selection)
    key = ControlKey(ch.output, ch.channel_id, block, int(index) if block == "dyneq" else -1)
    value = controls(service.view(session).draft).get(key)
    if value is None:
        raise ValueError("this block is unavailable on the selected channel")
    return key, FIRParameters.read(value) if isinstance(value, FIRSnapshot) else value


def edited_block(old: Any, rows: list[list[Any]]) -> Any:
    expected = {f.name for f in fields(old) if f.name != "raw"}
    if len(rows) != len(expected) or {row[0] for row in rows} != expected:
        raise ValueError("keep the editor's original field names and rows")
    updates: dict[str, Any] = {}
    for name, text in rows:
        previous = getattr(old, name)
        if isinstance(previous, bool):
            if str(text).lower() not in ("true", "false"):
                raise ValueError(f"{name} requires true or false")
            updates[name] = str(text).lower() == "true"
        elif isinstance(previous, int):
            numeric = float(text)
            if not numeric.is_integer():
                raise ValueError(f"{name} requires an integer")
            updates[name] = int(numeric)
        else:
            updates[name] = float(text)
    return replace(old, **updates)



def _copy_targets(target: int | None) -> tuple[int, ...]:
    """The single copy destination as the tuple DSPService.operation takes."""
    return () if target is None else (int(target),)

class AdvancedUI:
    def __init__(self, service: DSPService, on_reset: Callable[[str], None]) -> None:
        self.service = service
        self.on_reset = on_reset
        self.selections: dict[tuple[str, str], tuple[str, int]] = {}
        self.reviewed: dict[str, tuple[str, str, int, tuple[int, ...]]] = {}
        self.temp = tempfile.TemporaryDirectory(prefix="la548-downloads-")
        atexit.register(self.temp.cleanup)
        with gr.Tab("Advanced"):
            gr.Markdown("Dynamics and advanced stages are separate from the estimated static filter response.")
            with gr.Row():
                self.block = gr.Dropdown(label="Block", choices=[("Compressor", "compressor"),
                                                                ("Limiter", "limiter"), ("FIR parameters", "fir")],
                                        value="compressor")
                self.index = gr.Dropdown(label="Dynamic EQ band", choices=[1, 2, 3], value=1, visible=False)
            self.description = gr.Markdown()
            self.table = gr.Dataframe(headers=["Parameter", "Value"], datatype=["str", "str"], type="array",
                                      column_count=2, interactive=True, label="Precise block editor",
                                      elem_id="advanced-block-editor")
            self.stage = gr.Button("Preview block changes", variant="primary")
            self.message = gr.Markdown()
        with gr.Tab("Presets + device programs"):
            gr.Markdown("Local presets stage a draft. Device programs and channel copy require a clean draft "
                        "and refresh settings after execution.")
            with gr.Row():
                self.preset_file = gr.File(label="Local preset (.json)", file_types=[".json"], type="filepath",
                                           elem_id="preset-import")
                self.import_button = gr.Button("Stage preset")
                self.export_button = gr.Button("Export draft preset")
            self.download = gr.File(label="Download", interactive=False, elem_id="management-download")
            with gr.Row():
                self.snapshot = gr.Button("Download device snapshot")
                self.coefficients = gr.Button("Download selected channel FIR data")
            with gr.Row():
                self.fir_restore_file = gr.File(label="FIR coefficient backup (.bin)", file_types=[".bin"],
                                                type="filepath", elem_id="fir-restore-import")
                self.restore_coefficients = gr.Button("Restore selected channel FIR data")
            self.slot = gr.Number(label="Program slot (0-29)", value=0, precision=0, minimum=0, maximum=29)
            self.operation = gr.Radio(choices=[("Save current device into slot", "save"),
                                               ("Load slot into all channels", "load"),
                                               ("Copy selected channel", "copy")], value="save", label="Operation")
            # One destination per copy: Client.channel_copy (and the manufacturer's app)
            # accept exactly one. Choices are relabelled per selected channel
            # in render(), so input sources list inputs, not "Output N".
            self.targets = gr.Radio(choices=[], label="Copy destination", value=None,
                                    elem_id="program-copy-targets")
            self.review = gr.Markdown()
            self.review_button = gr.Button("Review destinations")
            self.confirm = gr.Checkbox(label="I confirm the destinations shown above", value=False)
            self.execute = gr.Button("Execute reviewed operation")
            self.operation_message = gr.Markdown()

    @property
    def outputs(self) -> list[Any]:
        return [self.block, self.index, self.description, self.table, self.targets, self.review, self.confirm]

    def render(self, session: str, selection: str, block: str | None = None, index: int = 1) -> dict[Any, Any]:
        ch = channel(self.service.view(session).draft, selection)
        self.reviewed.pop(session, None)
        if block is None:
            block, index = self.selections.get((session, selection), ("compressor", 1))
        available = [b for b in BLOCK_NAMES if (bool(ch.dynamic_eq_bands) if b == "dyneq"
                                              else getattr(ch, b, None) is not None)]
        block = block if block in available else available[0]
        index = min(max(int(index), 1), max(len(ch.dynamic_eq_bands), 1))
        self.selections[session, selection] = block, index
        _, value = block_value(self.service, session, selection, block, index - 1)
        rows = [[f.name, str(getattr(value, f.name)).lower() if isinstance(getattr(value, f.name), bool)
                 else str(getattr(value, f.name))] for f in fields(value) if f.name != "raw"]
        settings = self.service.view(session).draft
        destinations = settings.outputs if ch.output else settings.inputs
        return {self.block: gr.update(choices=[(BLOCK_NAMES[b], b) for b in available], value=block),
                self.index: gr.update(visible=block == "dyneq", value=index), self.table: rows,
                self.description: HELP[block],
                self.targets: gr.update(choices=[(c.name, c.channel_id) for c in destinations
                                                 if c.channel_id != ch.channel_id], value=None),
                self.review: "Choose an operation, then review its destinations.", self.confirm: False}

    def file(self, name: str, content: str | bytes) -> str:
        path = Path(self.temp.name) / (uuid.uuid4().hex[:8] + "-" + name)
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(content)
        return str(path)

    def bind(self, render: Callable[..., dict[Any, Any]], inputs: list[Any], outputs: list[Any],
             event: dict[str, Any]) -> None:
        def preview(*args: Any) -> dict[Any, Any]:
            session, *rest, block, index, rows = args
            try:
                key, old = block_value(self.service, session, rest[0], block, index - 1)
                value = edited_block(old, rows)
                if isinstance(value, FIRParameters):
                    value = value.snapshot(output=key.output)
                self.service.stage(session, key, value)
                return {**render(session, *rest, scope="plot"),
                        self.message: "Block staged. Use Apply or Live to send it."}
            except Exception as exc:
                return {self.message: f"Block unchanged: {exc}"}

        self.stage.click(preview, inputs=inputs + [self.block, self.index, self.table],
                         outputs=outputs + [self.message], **event)
        for component in [self.block, self.index]:
            component.input(self.render, inputs=[inputs[0], inputs[1], self.block, self.index],
                            outputs=self.outputs, **event)

        def stage_preset(*args: Any) -> dict[Any, Any]:
            session, *rest, path = args
            try:
                if not path:
                    raise ValueError("choose a local preset")
                settings = self.service.view(session).draft
                values = import_preset(settings, Path(path).read_text())
                self.service.set_live(session, False)
                self.service.stage_many(session, values)
                self.on_reset(session)
                return {**render(session, *rest),
                        self.operation_message: "Preset staged; Live disabled. Review and Apply."}
            except Exception as exc:
                return {self.operation_message: f"Preset unchanged: {exc}"}

        self.import_button.click(stage_preset, inputs=inputs + [self.preset_file],
                                 outputs=outputs + [self.operation_message], **event)

        def download(kind: str, session: str, selection: str) -> dict[Any, Any]:
            try:
                view = self.service.view(session)
                ch = channel(view.draft, selection)
                if kind == "preset":
                    path = self.file("draft-preset.json", export_preset(view.draft))
                    note = "Local draft preset exported."
                else:
                    content = self.service.read_backup(session, fir_channel=ch.channel_id if kind == "fir" else None,
                                                       output=ch.output)
                    path = self.file(f"{'out' if ch.output else 'in'}-{ch.channel_id + 1}-fir.bin" if kind == "fir"
                                     else "device-snapshot.bin", content)
                    note = ("Original demo fixture downloaded." if view.demo else
                            "Actual device data downloaded; pending drafts are not included.")
                return {self.download: path, self.operation_message: note}
            except Exception as exc:
                return {self.operation_message: f"Download failed: {exc}"}

        for button, kind in [(self.export_button, "preset"), (self.snapshot, "snapshot"), (self.coefficients, "fir")]:
            button.click(lambda s, c, k=kind: download(k, s, c), inputs=inputs[:2],
                         outputs=[self.download, self.operation_message], **event)

        def restore_coefficients(session: str, selection: str, path: str | None) -> dict[Any, Any]:
            try:
                if not path:
                    raise ValueError("choose a FIR coefficient backup")
                ch = channel(self.service.view(session).draft, selection)
                self.service.restore_fir_backup(session, Path(path).read_bytes(), channel=ch.channel_id,
                                                output=ch.output)
                return {self.fir_restore_file: None,
                        self.operation_message: f"FIR data restored and verified for {ch.name}."}
            except Exception as exc:
                return {self.operation_message: f"FIR restore failed: {exc}"}

        self.restore_coefficients.click(
            restore_coefficients, inputs=[inputs[0], inputs[1], self.fir_restore_file],
            outputs=[self.fir_restore_file, self.operation_message], **event)

        def review(session: str, selection: str, kind: str, slot: int, target: int | None) -> dict[Any, Any]:
            try:
                targets = _copy_targets(target)
                numeric_slot = float(slot)
                if not math.isfinite(numeric_slot) or not numeric_slot.is_integer() or not 0 <= numeric_slot < 30:
                    raise ValueError("program slot must be in the DA0408X range 0-29")
                slot_index = int(numeric_slot)
                ch = channel(self.service.view(session).draft, selection)
                settings = self.service.view(session).draft
                channels = settings.outputs if ch.output else settings.inputs
                names = ', '.join(channels[t].name for t in targets) or '(none selected)'
                text = (f"Save **current device settings** into slot **{slot_index}**, replacing that slot."
                        if kind == "save"
                        else f"Load slot **{slot_index}**, replacing **all input/output settings and routes**."
                        if kind == "load" else f"Copy **{ch.name}** onto **{names}**.")
                self.reviewed[session] = selection, kind, slot_index, tuple(targets)
                return {self.review: text, self.confirm: False}
            except Exception as exc:
                self.reviewed.pop(session, None)
                return {self.review: f"Cannot review operation: {exc}", self.confirm: False}

        operation_inputs = [inputs[0], inputs[1], self.operation, self.slot, self.targets]
        review_controls: list[Any] = [self.operation, self.slot, self.targets]
        for component in review_controls:
            component.input(review, inputs=operation_inputs, outputs=[self.review, self.confirm], **event)
        self.review_button.click(review, inputs=operation_inputs, outputs=[self.review, self.confirm], **event)

        def execute(*args: Any) -> dict[Any, Any]:
            session, *rest, kind, slot, target, confirmed = args
            try:
                targets = _copy_targets(target)
                if not confirmed:
                    raise ValueError("review and confirm the affected destinations first")
                if self.reviewed.get(session) != (rest[0], kind, int(slot), tuple(targets)):
                    raise ValueError("destinations changed or are unreviewed; use Review destinations first")
                ch = channel(self.service.view(session).draft, rest[0])
                self.service.operation(session, kind, slot=int(slot), output=ch.output,
                                       source=ch.channel_id, targets=tuple(targets))
                self.on_reset(session)
                return {**render(session, *rest), self.operation_message: "Operation completed; readback refreshed.",
                        self.confirm: False}
            except Exception as exc:
                return {**render(session, *rest), self.operation_message: f"Operation: {exc}", self.confirm: False}

        self.execute.click(execute, inputs=inputs + [self.operation, self.slot, self.targets, self.confirm],
                           outputs=outputs + [self.operation_message], **event)
