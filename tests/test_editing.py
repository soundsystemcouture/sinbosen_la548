import math

import pytest

from sinbosen_la548.editing import NumericEdits, NumericKey, validate_number


def test_numeric_typing_coalesces_and_retains_field_context():
    edits = NumericEdits()
    key = NumericKey("out:1", "eq_freq", 3)
    edits.queue("session", key, None, now=0)
    edits.queue("session", key, 3, now=0.1)
    assert edits.ready("session", now=0.3) == []
    edits.queue("session", key, 30, now=0.2)
    assert edits.ready("session", now=0.4) == []
    ready = edits.ready("session", now=0.5)
    assert len(ready) == 1 and ready[0][0] == key and ready[0][1].value == 30
    assert edits.ready("other", now=10) == []
    edits.finish("session", key)
    assert edits.get("session", key) is None


def test_invalid_numeric_edits_are_local_until_corrected_or_reverted():
    edits = NumericEdits()
    key = NumericKey("out:0", "eq_freq", 1)
    edits.queue("session", key, 3, now=0)
    edits.finish("session", key, "Frequency must be between 10 and 20000 Hz.")
    assert edits.get("session", key).value == 3
    assert edits.has_errors("session")
    assert edits.ready("session", now=10, force=True) == []
    edits.queue("session", key, 30, now=1)
    assert not edits.has_errors("session")
    assert edits.ready("session", now=1, force=True)[0][1].value == 30
    edits.drop("session")
    assert edits.get("session", key) is None


@pytest.mark.parametrize("value,message", [
    (None, "Enter frequency"), (3, "between 10 and 20000 Hz"),
    (20001, "between 10 and 20000 Hz"), (30.5, "whole number"),
    (math.nan, "finite"), (math.inf, "finite"),
])
def test_numeric_validation_explains_invalid_values(value, message):
    with pytest.raises(ValueError, match=message):
        validate_number(value, minimum=10, maximum=20000, label="Frequency", unit="Hz", integer=True)


@pytest.mark.parametrize("value", [10, 30, 20000])
def test_frequency_bounds_are_inclusive(value):
    assert validate_number(value, minimum=10, maximum=20000, label="Frequency", integer=True) == value
