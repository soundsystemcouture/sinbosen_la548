import json
from dataclasses import replace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from sinbosen_la548.traces import load_trace, sum_traces  # noqa: E402


def native(tmp_path, **adjustments):
    data = {"name": "Measured", "ftdata": [[0, 9, 1, 0, 1], [100, 9, 1, 0, 1],
                                          [200, 9, 2, 0, 0.2], [300, 9, 1, 0, 1]], **adjustments}
    path = tmp_path / "trace.osm"
    path.write_text(json.dumps({"type": "stored", "data": data}))
    return load_trace(path)


def test_native_units_adjustments_and_immutable_samples(tmp_path):
    trace = native(tmp_path, gain=6, delay=2, polarity=True, inverse=True,
                   impulse=[[0, 1]], sampleRate=96000)
    np.testing.assert_allclose(trace.magnitude_db, [-6, -12.020599913, -6])
    np.testing.assert_allclose(trace.phase_rad, np.pi - 2 * np.pi * trace.frequencies * 0.002)
    np.testing.assert_allclose(trace.impulse_time_ms, [2])
    np.testing.assert_allclose(trace.impulse_values, [-10 ** 0.3])
    assert trace.sample_rate_hz == 96000
    with pytest.raises(ValueError):
        trace.magnitude_db[0] = 0
    response = trace.response(coherence_threshold=0.5)
    assert np.isnan(response.magnitude_db[1])
    assert np.isfinite(response.magnitude_db[[0, 2]]).all()


def test_native_ignore_coherence_and_optional_fields(tmp_path):
    trace = native(tmp_path, icoherence=True)
    np.testing.assert_array_equal(trace.coherence, [1, 1, 1])
    assert np.isfinite(trace.response(coherence_threshold=0.9).magnitude_db).all()


@pytest.mark.parametrize("suffix,separator", [(".csv", ","), (".frd", " "), (".txt", "\t")])
def test_text_units_missing_values_and_comments(tmp_path, suffix, separator):
    path = tmp_path / ("export" + suffix)
    path.write_text("* REW export\nFrequency Magnitude Phase Coherence\n" + "\n".join(
        separator.join(row) for row in [["100", "0", "-90", "1"], ["200", "*", "*", "*"],
                                      ["300", "6", "90", "0.9"], ["400", "0", "180", "1"]]))
    trace = load_trace(path)
    assert np.isnan(trace.magnitude_db[1])
    np.testing.assert_allclose(trace.phase_rad[[0, 2, 3]], [-np.pi / 2, np.pi / 2, np.pi])
    assert np.isnan(trace.response().phase_deg[1])


def test_magnitude_only_never_supplies_fake_phase(tmp_path):
    path = tmp_path / "magnitude.csv"
    path.write_text("100,0\n200,1\n300,2\n")
    trace = load_trace(path)
    assert trace.phase_rad is None
    assert np.isnan(trace.response().phase_deg).all()
    with pytest.raises(ValueError, match="require phase"):
        sum_traces(trace, trace, timing_confirmed=True)


def test_complex_sum_confirmation_cancellation_and_timing(tmp_path):
    trace = native(tmp_path, delay=1200)
    with pytest.raises(ValueError, match="confirmed"):
        sum_traces(trace, trace)
    doubled = sum_traces(trace, trace, timing_confirmed=True)
    np.testing.assert_allclose(doubled.magnitude_db, trace.magnitude_db + 20 * np.log10(2))
    inverted = replace(trace, phase_rad=trace.phase_rad + np.pi)
    assert np.isnan(sum_traces(trace, inverted, timing_confirmed=True).phase_deg).all()
    with pytest.raises(ValueError, match="sample rates"):
        sum_traces(replace(trace, sample_rate_hz=96000), replace(trace, sample_rate_hz=48000),
                   timing_confirmed=True)


@pytest.mark.parametrize("content", ["100,0\n100,1\n200,0", "100,0\n200,inf\n300,0",
                                     "100,0,0,2\n200,0,0,1\n300,0,0,1", "100,0\n200,0"])
def test_malformed_text_is_rejected(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_trace(path)


def test_native_project_is_rejected(tmp_path):
    path = tmp_path / "project.osm"
    path.write_text('{"type":"sourcelsist","data":[]}')
    with pytest.raises(ValueError, match="single OSM stored"):
        load_trace(path)


def test_empty_csv_and_nan_samples_preserve_column_positions(tmp_path):
    path = tmp_path / "gaps.csv"
    path.write_text("100,0,0,1\n200,,90,0.5\n300,0,nan,1\n400,0,180,1\n")
    trace = load_trace(path)
    assert np.isnan(trace.magnitude_db[1])
    assert trace.phase_rad[1] == pytest.approx(np.pi / 2)
    assert np.isnan(trace.phase_rad[2])
    assert trace.response().magnitude_db[2] == 0
    assert np.isnan(trace.response().phase_deg[2])
