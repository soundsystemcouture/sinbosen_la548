"""Reproduce the HP section calibration from recorded 1 kHz measurements.

Run with the UI dependencies installed: python -m tools.calibrate_bessel_hp
Prints coefficients only; never connects to hardware or edits production code.
Other cutoff frequencies in the fixture are reserved for validation.
"""

import json
from pathlib import Path

import numpy as np
from scipy import optimize, signal


def main() -> None:
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/filter_measurements.json"
    cases = json.loads(fixture.read_text())["cases"]
    for case in cases:
        if not case["id"].startswith("v3_xover-extended_") or case["fc"] != 1000:
            continue
        order = case["slope"] // 6
        if order == 1:
            print("1: ((1.0, None),),")
            continue
        points = np.asarray(case["points"], dtype=float)
        frequencies = points[:, 0]
        measured = 10 ** (points[:, 1] / 20) * np.exp(1j * np.deg2rad(points[:, 2]))
        x = np.tan(np.pi * frequencies / 96000) / np.tan(np.pi * 1000 / 96000)
        mask = (x > 0.5) & (x < 8)
        _, poles, _ = signal.besselap(order, norm="mag")
        starting_q = sorted(abs(p) / (-2 * p.real) for p in poles if p.imag > 0)

        def model(parameters, order=order, x=x):
            scale = np.exp(parameters[0])
            w = np.tan(np.pi * 1000 * scale / 96000) / np.tan(np.pi * 1000 / 96000)
            s = 1j * x
            h = np.ones(len(x), dtype=complex)
            if order % 2:
                first_scale = np.exp(parameters[-1])
                w1 = np.tan(np.pi * 1000 * first_scale / 96000) / np.tan(np.pi * 1000 / 96000)
                h *= s / (s + w1)
            for q in np.exp(parameters[1:1 + order // 2]):
                h *= s * s / (s * s + w / q * s + w * w)
            return h

        def residual(parameters, model=model, measured=measured, mask=mask):
            error = np.log(model(parameters) / measured)[mask]
            return np.r_[error.real, error.imag]

        start = [0.8 if order < 4 else 0.6] + starting_q + ([0.7] if order % 2 else [])
        fit = optimize.least_squares(residual, np.log(start), max_nfev=3000,
                                     gtol=1e-12, xtol=1e-12, ftol=1e-12)
        if not fit.success:
            raise RuntimeError(f"Calibration failed for order {order}: {fit.message}")
        values = np.exp(fit.x)
        sections = [(round(float(values[0]), 6), round(float(q), 6)) for q in values[1:1 + order // 2]]
        if order % 2:
            sections.insert(0, (round(float(values[-1]), 6), None))
        print(f"{order}: {tuple(sections)!r},")


if __name__ == "__main__":
    main()
