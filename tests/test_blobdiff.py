from sinbosen_la548.blobdiff import ByteDiff, diff_blobs


def test_diff_blobs_finds_changed_bytes():
    before = bytes([0x00, 0x01, 0x02, 0x03])
    after = bytes([0x00, 0x99, 0x02, 0x42])
    assert diff_blobs(before, after) == [ByteDiff(1, 0x01, 0x99), ByteDiff(3, 0x03, 0x42)]


def test_diff_blobs_no_differences():
    blob = bytes([0x01, 0x02, 0x03])
    assert diff_blobs(blob, blob) == []


def test_diff_blobs_handles_length_mismatch_by_comparing_shorter_prefix():
    before = bytes([0x01, 0x02, 0x03])
    after = bytes([0x01, 0x02])
    assert diff_blobs(before, after) == []
