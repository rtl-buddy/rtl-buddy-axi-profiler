"""Tolerant JSON diff helper for the E2E golden-test harness.

Walks the axi-perf v1 payload alongside a golden copy and applies
field-typed tolerance: ±0.5% on percent/avg floats, exact on
integer counters and bandwidth, exact on percentile cycle counts.
``tool_version`` and ``produced_at`` vary by run and are excluded.

Returns a list of human-readable diff lines. Empty list means
"matches within tolerance" — callers ``assert not diff, '\\n'.join(diff)``.
"""

from __future__ import annotations

from typing import Any

# Top-level keys that vary across runs and must not gate the golden.
_VOLATILE_TOP_LEVEL = frozenset({"tool_version", "produced_at"})

# Float fields use relative tolerance. Anything not listed here is
# treated as exact-int (matches schema reality: counts, bandwidth in
# bps, percentile cycles, hist_log2 buckets).
_FLOAT_RELATIVE_TOL = 0.005  # ±0.5%
_FLOAT_FIELDS = frozenset(
    {
        "util_pct",
        "bp_pct",
        "read_avg",
        "write_avg",
        "fairness_jain",
        "clock_period_ns",
    }
)


def diff_axi_perf(actual: dict[str, Any], golden: dict[str, Any]) -> list[str]:
    """Return a list of difference lines; empty means within tolerance."""
    out: list[str] = []
    _walk(actual, golden, path="", out=out)
    return out


def _walk(actual: Any, golden: Any, *, path: str, out: list[str]) -> None:
    if isinstance(golden, dict):
        if not isinstance(actual, dict):
            out.append(f"{path}: expected object, got {type(actual).__name__}")
            return
        for key in golden:
            if path == "" and key in _VOLATILE_TOP_LEVEL:
                continue
            sub = f"{path}.{key}" if path else key
            if key not in actual:
                out.append(f"{sub}: missing in actual")
                continue
            _walk(actual[key], golden[key], path=sub, out=out)
        for key in actual:
            if path == "" and key in _VOLATILE_TOP_LEVEL:
                continue
            if key not in golden:
                sub = f"{path}.{key}" if path else key
                out.append(f"{sub}: unexpected in actual (not in golden)")
        return
    if isinstance(golden, list):
        if not isinstance(actual, list):
            out.append(f"{path}: expected list, got {type(actual).__name__}")
            return
        if len(actual) != len(golden):
            out.append(f"{path}: list length {len(actual)} != golden {len(golden)}")
            # Still compare the common prefix so the user sees field-level diffs.
        for i, (a, g) in enumerate(zip(actual, golden)):
            _walk(a, g, path=f"{path}[{i}]", out=out)
        return
    # Scalar leaves — apply field-typed tolerance.
    field_name = path.rsplit(".", 1)[-1]
    if isinstance(golden, float) or field_name in _FLOAT_FIELDS:
        _diff_float(actual, golden, path=path, out=out)
        return
    if actual != golden:
        out.append(f"{path}: {actual!r} != golden {golden!r}")


def _diff_float(actual: Any, golden: Any, *, path: str, out: list[str]) -> None:
    try:
        a = float(actual)
        g = float(golden)
    except (TypeError, ValueError):
        out.append(f"{path}: non-numeric float field {actual!r} vs {golden!r}")
        return
    if g == 0.0:
        if abs(a) > _FLOAT_RELATIVE_TOL:
            out.append(f"{path}: {a} not ~= 0 (tol ±{_FLOAT_RELATIVE_TOL})")
        return
    rel = abs(a - g) / abs(g)
    if rel > _FLOAT_RELATIVE_TOL:
        out.append(
            f"{path}: {a} not within ±{_FLOAT_RELATIVE_TOL * 100:.1f}% of {g} "
            f"(rel diff {rel * 100:.3f}%)"
        )
