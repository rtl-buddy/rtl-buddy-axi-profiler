"""Unit tests for the tolerant JSON-diff helper.

Locks the contract: counts/cycles/bandwidth are exact; percent and
average floats get ±0.5% relative tolerance; ``tool_version`` and
``produced_at`` are ignored. Without this, a stage regression that
inflates an int counter by 1 could slip past as "close enough" if
the diff helper accidentally widened its tolerance to integers.
"""

from __future__ import annotations

from tests._e2e_diff import diff_axi_perf


def _minimal_payload(*, slverr: int = 0, util_pct: float = 10.0) -> dict:
    return {
        "schema_version": "1.0",
        "tool_version": "anything",
        "produced_at": "anything",
        "design_top": "top",
        "bundles": [
            {
                "name": "b",
                "channels": {
                    "ar": {"util_pct": util_pct, "txns": 4},
                },
                "errors": {"slverr": slverr, "decerr": 0},
            }
        ],
    }


def test_identical_payload_no_diff() -> None:
    payload = _minimal_payload()
    assert diff_axi_perf(payload, payload) == []


def test_volatile_fields_ignored() -> None:
    a = _minimal_payload()
    b = _minimal_payload()
    a["tool_version"] = "rev-A"
    b["tool_version"] = "rev-B"
    a["produced_at"] = "2024-01-01T00:00:00Z"
    b["produced_at"] = "2099-12-31T23:59:59Z"
    assert diff_axi_perf(a, b) == []


def test_integer_counter_mismatch_is_caught() -> None:
    """``errors.slverr`` is an exact-int field — off-by-one must fail
    even though the relative diff is tiny."""
    actual = _minimal_payload(slverr=5)
    golden = _minimal_payload(slverr=4)
    diff = diff_axi_perf(actual, golden)
    assert any("slverr" in line for line in diff), diff


def test_float_within_tolerance_passes() -> None:
    """util_pct at 10.0 vs 10.04 = 0.4% — under the 0.5% band."""
    actual = _minimal_payload(util_pct=10.04)
    golden = _minimal_payload(util_pct=10.0)
    assert diff_axi_perf(actual, golden) == []


def test_float_outside_tolerance_fails() -> None:
    """util_pct at 10.0 vs 11.0 = 10% — well over the band."""
    actual = _minimal_payload(util_pct=11.0)
    golden = _minimal_payload(util_pct=10.0)
    diff = diff_axi_perf(actual, golden)
    assert any("util_pct" in line for line in diff), diff


def test_missing_key_is_reported() -> None:
    golden = _minimal_payload()
    actual = _minimal_payload()
    del actual["bundles"][0]["errors"]
    diff = diff_axi_perf(actual, golden)
    assert any("errors" in line and "missing" in line for line in diff), diff
