from e2e.conftest import expected_stream_close


def test_only_the_reviewed_sdk_close_stream_abort_is_expected():
    asset = "http://127.0.0.1:7860/assets/index-CRMvpSRm.js:4:1384"
    closed = f"AbortError: signal is aborted without reason\n    at xt ({asset})\n    at s.onmessage ({asset})"
    assert expected_stream_close(closed)
    assert not expected_stream_close(closed.replace("s.onmessage", "disconnect"))
    assert not expected_stream_close(closed.replace("index-CRMvpSRm.js", "index-unreviewed.js"))
    assert not expected_stream_close(closed.replace("AbortError", "TypeError"))
    assert not expected_stream_close("AbortError: signal is aborted without reason")
