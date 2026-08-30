"""Property-based tests (Hypothesis) for the safety-critical primitives.

Example-based tests show the code works on the cases we thought of;
property-based tests search for the cases we did not. The three properties
below are the ones a physical-safety layer cannot afford to get wrong:

  P1  A declared Range NEVER admits an out-of-range value, for any float.
  P2  Unit conversion is exact on round-trip and never crosses dimensions.
  P3  Any single-field mutation of any audit record is detected.

Run:  python -m pytest tests/test_property.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from labaiagent.core.audit import AuditLog
from labaiagent.core.types import (
    _TO_BASE,
    OneOf,
    Param,
    Pattern,
    Range,
    convert,
)

finite = st.floats(allow_nan=False, allow_infinity=False, width=64)


# --------------------------------------------------------------------------
# P1: parameter validation admits ONLY in-range values
# --------------------------------------------------------------------------

@settings(max_examples=300)
@given(low=finite, high=finite, value=finite)
def test_range_never_admits_out_of_range(low, high, value):
    if low > high:
        low, high = high, low
    p = Param("x", float, limits=Range(low, high))
    try:
        out = p.validate(value)
    except ValueError:
        # Refusal is only correct when the value is genuinely outside.
        assert not (low <= value <= high)
    else:
        assert low <= out <= high


@settings(max_examples=200)
@given(value=finite)
def test_nan_and_inf_never_pass(value):
    p = Param("x", float, limits=Range(-1e300, 1e300))
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            p.validate(bad)


@settings(max_examples=200)
@given(options=st.lists(st.integers(-100, 100), min_size=1, max_size=8,
                        unique=True),
       probe=st.integers(-200, 200))
def test_oneof_admits_only_listed_values(options, probe):
    p = Param("x", int, limits=OneOf(*options))
    try:
        out = p.validate(probe)
    except ValueError:
        assert probe not in options
    else:
        assert out in options


@settings(max_examples=200)
@given(text=st.text(max_size=12))
def test_pattern_is_fullmatch_not_search(text):
    """'A1' embedded in junk must NOT pass a well-ID pattern."""
    p = Param("well", str, limits=Pattern(r"[A-H][1-9]"))
    import re
    try:
        p.validate(text)
    except (ValueError, TypeError):
        assert re.fullmatch(r"[A-H][1-9]", text) is None
    else:
        assert re.fullmatch(r"[A-H][1-9]", text) is not None


# --------------------------------------------------------------------------
# P2: unit conversion -- exact round-trips, no cross-dimension leaks
# --------------------------------------------------------------------------

_LINEAR_UNITS = [u for u in _TO_BASE if u not in ("degC", "K")]


@settings(max_examples=300)
@given(value=st.floats(min_value=1e-6, max_value=1e6),
       frm=st.sampled_from(_LINEAR_UNITS), to=st.sampled_from(_LINEAR_UNITS))
def test_conversion_roundtrip_is_exact_or_refused(value, frm, to):
    try:
        there = convert(value, frm, to)
    except ValueError:
        # Refusal must mean different dimension families (different bases).
        assert _TO_BASE[frm][0] != _TO_BASE[to][0] or frm not in _TO_BASE
        return
    back = convert(there, to, frm)
    assert back == pytest.approx(value, rel=1e-12)


@settings(max_examples=100)
@given(value=st.floats(min_value=-200, max_value=200))
def test_temperature_roundtrip(value):
    assert convert(convert(value, "degC", "K"), "K", "degC") == pytest.approx(value)


# --------------------------------------------------------------------------
# P3: the audit chain detects ANY single-field mutation
# --------------------------------------------------------------------------

MUTABLE_FIELDS = ["actor", "device", "capability", "risk", "reason", "error",
                  "event", "timestamp"]


@settings(max_examples=60, deadline=None)
@given(n_records=st.integers(2, 6),
       which=st.integers(0, 5),
       field=st.sampled_from(MUTABLE_FIELDS),
       junk=st.text(min_size=1, max_size=10))
def test_any_single_field_mutation_is_detected(tmp_path_factory, n_records,
                                               which, field, junk):
    tmp = tmp_path_factory.mktemp("audit")
    path = tmp / "log.jsonl"
    log = AuditLog(path)
    for i in range(n_records):
        log.record("invoke", device=f"d{i}", capability="x", reason="r")
    assert log.verify()[0]

    lines = path.read_text().splitlines()
    idx = which % n_records
    rec = json.loads(lines[idx])
    if rec.get(field) == junk:
        junk = junk + "_changed"
    rec[field] = junk
    lines[idx] = json.dumps(rec, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    ok, _ = AuditLog(path).verify()
    assert ok is False
