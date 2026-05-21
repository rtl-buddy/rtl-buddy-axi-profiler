"""Render the per-bundle SV monitor from a manifest.

v1 emits a `bind`-style monitor (zero DUT modification); explicit
instantiation support is tracked as a follow-up to #4. Time precision
is plumbed from rtl_buddy's root_config.yaml when invoked via
`rb axi-profile`; the standalone CLI accepts ``--time-precision``.

The generated SV is intentionally a skeleton: it builds, instantiates
per-bundle modules, hooks signal paths via cross-module references,
and reserves the file-emission section. The byte-exact axi-stream
binary serialization in the `final` block is a follow-up since it
requires simulator-specific byte-order helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import jinja2
import yaml


class GenMonitorError(ValueError):
    """Raised when a manifest can't be rendered into an SV monitor."""


# Valid IEEE-1800 timeprecision atoms.
TIME_PRECISIONS = (
    "1s",
    "100ms",
    "10ms",
    "1ms",
    "100us",
    "10us",
    "1us",
    "100ns",
    "10ns",
    "1ns",
    "100ps",
    "10ps",
    "1ps",
    "100fs",
    "10fs",
    "1fs",
)


def render_monitor(
    manifest_path: Path,
    *,
    time_precision: str = "1ps",
    buffer_cap: int = 65_536,
    tool_version: str | None = None,
) -> str:
    """Render the SV monitor source from a manifest YAML at ``manifest_path``.

    ``time_precision`` must be an IEEE-1800 timeprecision atom
    (``1ns`` / ``100ps`` / ``1ps`` / ``1fs`` / …). Mismatches against
    the testbench `timeprecision are a sim setup bug — the generator
    encodes the value into a header comment + the `timescale directive.

    ``buffer_cap`` is the per-bundle FIFO depth cap. Drained only at
    `$finish`; raise via plus-arg if your sim emits a very chatty
    fabric without finishing in time.
    """
    if time_precision not in TIME_PRECISIONS:
        raise GenMonitorError(
            f"time_precision {time_precision!r} is not a valid IEEE-1800 "
            f"timeprecision atom. Use one of {TIME_PRECISIONS}."
        )

    payload = yaml.safe_load(manifest_path.read_text())
    if not isinstance(payload, dict) or "bundles" not in payload:
        raise GenMonitorError(
            f"{manifest_path}: not a valid axi-bundles.yaml (no 'bundles' key)"
        )

    bundles = _flatten_bundles(payload["bundles"])
    if not bundles:
        raise GenMonitorError(f"{manifest_path}: no bundles to monitor")

    env = jinja2.Environment(
        loader=jinja2.PackageLoader(
            "rtl_buddy_axi_profiler.stages.gen_monitor", "templates"
        ),
        autoescape=False,
        trim_blocks=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    template = env.get_template("axi_perf_mon.sv.j2")
    rendered = template.render(
        tool_version=tool_version or _read_version(),
        generated_at=_now_iso(),
        time_precision=time_precision,
        time_unit_ps=_precision_to_ps(time_precision),
        buffer_cap=buffer_cap,
        design_top=payload.get("design_top", "<design_top>"),
        bundles=bundles,
    )
    return rendered


def write_monitor(
    manifest_path: Path,
    output: Path,
    *,
    time_precision: str = "1ps",
    buffer_cap: int = 65_536,
) -> None:
    """Convenience wrapper: render and write to disk."""
    text = render_monitor(
        manifest_path, time_precision=time_precision, buffer_cap=buffer_cap
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)


def _flatten_bundles(raw: list) -> list[dict]:
    """Walk a manifest's bundle list one level deep and return a flat
    list with parent-then-children ordering (matching the bundle_id
    in the binary stream)."""
    out: list[dict] = []
    for b in raw:
        out.append(b)
        for child in b.get("children", []) or []:
            out.append(child)
    return out


def _precision_to_ps(prec: str) -> int:
    """Return the number of picoseconds per tick for ``prec``."""
    mapping = {
        "1ps": 1,
        "10ps": 10,
        "100ps": 100,
        "1ns": 1_000,
        "10ns": 10_000,
        "100ns": 100_000,
        "1us": 1_000_000,
        "10us": 10_000_000,
        "100us": 100_000_000,
        "1ms": 1_000_000_000,
        "10ms": 10_000_000_000,
        "100ms": 100_000_000_000,
        "1s": 1_000_000_000_000,
        "1fs": 0,  # represented as 0 in the header (sub-ps).
        "10fs": 0,
        "100fs": 0,
    }
    return mapping.get(prec, 1)


def _read_version() -> str:
    from importlib.metadata import version as _v

    try:
        return _v("rtl-buddy-axi-profiler")
    except Exception:
        return "0.0.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Importable so other tools can introspect the bundled templates.
def list_templates() -> list[str]:
    return [
        p.name
        for p in resources.files(
            "rtl_buddy_axi_profiler.stages.gen_monitor.templates"
        ).iterdir()
        if p.is_file()
    ]
