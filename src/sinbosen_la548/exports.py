"""Standalone interactive plots and numeric curve exports."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any


def plot_archive(figure: Any, notes: str) -> bytes:
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow(["trace", "frequency_hz", "magnitude_db", "phase_deg", "group_delay_ms"])
    for start in range(0, len(figure.data), 3):
        magnitude, phase, delay = figure.data[start:start + 3]
        for f, m, p, d in zip(magnitude.x, magnitude.y, phase.y, delay.y, strict=True):
            writer.writerow([magnitude.name, f, m, p, d])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("response.html", figure.to_html(include_plotlyjs=True, full_html=True))
        output.writestr("curves.csv", table.getvalue())
        output.writestr("model-notes.txt", notes)
    return archive.getvalue()
