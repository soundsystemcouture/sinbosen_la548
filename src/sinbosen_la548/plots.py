"""Plotly response visualization shared by tuning and alignment workspaces."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .response import Response

COLORS = ("#5368ed", "#ec8a37", "#8b5cf6", "#059b86", "#d34f88", "#3283c9")


def response_figure(traces: Sequence[tuple[str, Response, str, str]], *, unwrapped: bool = False,
                    low_frequency: bool = False, title: str = "Filter response") -> Any:
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.075,
                        row_heights=[0.44, 0.30, 0.26])
    for name, response, color, dash in traces:
        for row, values, unit in ((1, response.magnitude_db, "dB"),
                                  (2, response.phase_unwrapped_deg if unwrapped else response.phase_deg, "°"),
                                  (3, response.group_delay_ms, "ms")):
            fig.add_trace(go.Scatter(x=response.frequencies, y=values, name=name, legendgroup=name,
                                    showlegend=row == 1, mode="lines", connectgaps=False,
                                    line={"color": color, "width": 2.8 if dash == "solid" else 1.6, "dash": dash},
                                    hovertemplate=f"%{{x:.1f}} Hz · %{{y:.3f}} {unit}<extra>{name}</extra>"),
                          row=row, col=1)
    fig.update_layout(title={"text": title, "font": {"size": 18}}, height=640, template="plotly_white",
                      margin={"l": 65, "r": 25, "t": 75 + max(len(traces) - 5, 0) * 4, "b": 55},
                      hovermode="x unified",
                      legend={"orientation": "h", "y": 1.10, "x": 0},
                      uirevision=f"response-axes-{low_frequency}-{unwrapped}",
                      font={"family": "Inter, system-ui, sans-serif", "size": 12},
                      paper_bgcolor="#ffffff", plot_bgcolor="#fbfcff")
    fig.update_xaxes(type="log", range=[1, np.log10(300 if low_frequency else 20000)],
                     tickvals=[10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000],
                     ticktext=["10", "20", "50", "100", "200", "500", "1k", "2k", "5k", "10k", "20k"],
                     gridcolor="#e8edf5", showspikes=True, spikemode="across")
    fig.update_xaxes(title_text="Frequency (Hz)", row=3, col=1)
    fig.update_yaxes(title_text="Magnitude (dB)", range=[-60, 20], row=1, col=1)
    fig.update_yaxes(title_text="Phase (°)", row=2, col=1)
    fig.update_yaxes(title_text="Group delay (ms)", row=3, col=1)
    if not unwrapped:
        fig.update_yaxes(range=[-190, 190], row=2, col=1)
    fig.update_yaxes(gridcolor="#e8edf5", zerolinecolor="#ccd5e3")
    return fig
