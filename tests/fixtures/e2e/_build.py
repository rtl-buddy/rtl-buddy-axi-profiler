"""Shared builders for the E2E trust-set fixtures.

Each fixture under ``tests/fixtures/e2e/<name>/`` ships:

- ``dump.vcd`` — paired waveform input (text-format VCD; wellen
  reads it identically to a Verilator-produced FST)
- ``axi-bundles.yaml`` — the manifest the run consumes
- ``axi-perf.json.golden`` — expected pipeline output

To regenerate any fixture after a stage contract changes, edit the
relevant builder here, then::

    uv run python tests/fixtures/e2e/build_fixtures.py

The script writes all three files for each fixture from scratch, so
review the golden diff carefully before committing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rtl_buddy_axi_profiler.stages.discover._emit import emit_manifest
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Manifest,
    Protocol,
)

from tests._vcd_helpers import VcdWriter

# Canonical fixture layout — every fixture dir holds these names.
DUMP_VCD_NAME = "dump.vcd"
MANIFEST_NAME = "axi-bundles.yaml"
GOLDEN_NAME = "axi-perf.json.golden"

# AXI signal roles used by every bundle. Drives both the wellen
# manifest's ``signals`` dict and the VCD declarations.
_AXI_ROLES = (
    "arvalid",
    "arready",
    "araddr",
    "arid",
    "arlen",
    "arsize",
    "awvalid",
    "awready",
    "awaddr",
    "awid",
    "awlen",
    "awsize",
    "rvalid",
    "rready",
    "rid",
    "rresp",
    "rlast",
    "wvalid",
    "wready",
    "wlast",
    "bvalid",
    "bready",
    "bid",
    "bresp",
)
_ONE_BIT_ROLES = frozenset(
    {
        "arvalid",
        "arready",
        "rvalid",
        "rready",
        "rlast",
        "awvalid",
        "awready",
        "wvalid",
        "wready",
        "wlast",
        "bvalid",
        "bready",
    }
)
_DEFAULT_WIDTHS = {
    "araddr": 32,
    "awaddr": 32,
    "arid": 4,
    "awid": 4,
    "rid": 4,
    "bid": 4,
    "arlen": 8,
    "awlen": 8,
    "arsize": 3,
    "awsize": 3,
    "rresp": 2,
    "bresp": 2,
}


@dataclass(frozen=True)
class BundleSpec:
    """Inputs needed to declare one bundle in the manifest + VCD."""

    name: str
    master_path: str  # hierarchical instance path holding the master ports
    slave_path: str
    data_width: int = 64
    id_width: int = 4
    signal_prefix: str = "m_axi_"
    clock_signal: str = ""  # absolute path; defaults to top.clk if empty


def make_bundle(spec: BundleSpec) -> Bundle:
    """Build a :class:`Bundle` from a :class:`BundleSpec`."""
    signals = {
        role: f"{spec.master_path}.{spec.signal_prefix}{role}" for role in _AXI_ROLES
    }
    return Bundle(
        name=spec.name,
        master_path=spec.master_path,
        slave_path=spec.slave_path,
        protocol=Protocol.AXI4,
        data_width=spec.data_width,
        id_width=spec.id_width,
        source=BundleSource.USER,
        default_view=DefaultView.PARENT,
        signals=signals,
        clock_signal=spec.clock_signal,
    )


def manifest_from(specs: list[BundleSpec], *, design_top: str) -> Manifest:
    return Manifest(
        schema_version="1.0",
        design_top=design_top,
        bundles=tuple(make_bundle(s) for s in specs),
        generated_by="tests/fixtures/e2e/_build.py",
        generated_at="2026-05-26T00:00:00Z",
    )


def declare_bundle_signals(w: VcdWriter, spec: BundleSpec) -> None:
    """Declare every AXI signal for one bundle on the writer."""
    for role in _AXI_ROLES:
        width = 1 if role in _ONE_BIT_ROLES else _DEFAULT_WIDTHS[role]
        w.declare(f"{spec.master_path}.{spec.signal_prefix}{role}", width)


def initialize_bundle_zero(w: VcdWriter, spec: BundleSpec, *, t: int = 0) -> None:
    """Drive every AXI signal to 0 at time ``t`` so wellen has a baseline."""
    for role in _AXI_ROLES:
        w.change(t, f"{spec.master_path}.{spec.signal_prefix}{role}", 0)


def emit_clock(
    w: VcdWriter, *, path: str, posedges: int, half_period_ns: int = 5
) -> None:
    """Emit a square-wave clock with ``posedges`` rising edges total.

    Toggles every ``half_period_ns`` from t=0; rising edges land at
    ``t = (2*i + 1) * half_period_ns`` for i = 0..posedges-1.
    """
    w.declare(path, 1)
    # 2*posedges + 1 transitions to cover the full window (start low,
    # end either edge).
    for i in range(2 * posedges + 1):
        w.change(i * half_period_ns, path, i % 2)


def write_manifest(manifest: Manifest, fixture_dir: Path) -> Path:
    """Emit the manifest into the fixture dir; return its path."""
    out = fixture_dir / MANIFEST_NAME
    emit_manifest(manifest, out)
    return out


def write_vcd(w: VcdWriter, fixture_dir: Path) -> Path:
    """Render and write the VCD into the fixture dir; return its path."""
    out = fixture_dir / DUMP_VCD_NAME
    out.write_text(w.render())
    return out
