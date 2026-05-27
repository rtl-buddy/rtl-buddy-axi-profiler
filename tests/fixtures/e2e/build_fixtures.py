"""Regenerate every E2E trust-set fixture.

Run from the repo root::

    uv run python tests/fixtures/e2e/build_fixtures.py

Each fixture's ``dump.vcd``, ``axi-bundles.yaml``, and
``axi-perf.json.golden`` is rewritten in place. Diff the result
carefully before committing — the golden is the contract the
harness checks against.

Adding a fixture: write a ``build_<name>()`` function below that
populates ``tests/fixtures/e2e/<name>/`` and append it to
``FIXTURE_BUILDERS``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from rtl_buddy_axi_profiler.stages.aggregate.standard import aggregate
from rtl_buddy_axi_profiler.stages.emit.json_v1 import build_payload
from rtl_buddy_axi_profiler.stages.ingest.wellen import WellenIngest
from rtl_buddy_axi_profiler.stages.reconstruct.axi4 import reconstruct
from rtl_buddy_axi_profiler.types import Manifest

from tests._vcd_helpers import VcdWriter
from tests.fixtures.e2e._build import (
    GOLDEN_NAME,
    BundleSpec,
    declare_bundle_signals,
    emit_clock,
    initialize_bundle_zero,
    manifest_from,
    write_manifest,
    write_vcd,
)

FIXTURES_ROOT = Path(__file__).parent


# --- errors fixture ---------------------------------------------------------


def build_errors() -> None:
    """SLVERR + DECERR roll-up: lifts the txn-layer coverage in
    ``test_aggregate.py`` up to the FST→JSON layer.

    Layout:
    - 1 bundle ``cpu_to_mem``, master under ``top.u_cpu``, slave under
      ``top.u_mem``, global clock at ``top.clk``.
    - 2 reads with OKAY (resp=0), 2 with SLVERR (resp=2), 2 with DECERR
      (resp=3) — completed via R+RLAST.
    - 2 writes with OKAY, 2 with SLVERR, 2 with DECERR — completed via B.

    Expected (asserted by ``test_e2e_run.py`` against the golden):
    - ``errors.slverr`` = 4
    - ``errors.decerr`` = 4
    """
    fixture_dir = FIXTURES_ROOT / "errors"
    fixture_dir.mkdir(exist_ok=True)

    spec = BundleSpec(
        name="cpu_to_mem",
        master_path="top.u_cpu",
        slave_path="top.u_mem",
        clock_signal="top.clk",
    )
    manifest = manifest_from([spec], design_top="top")

    # 100 MHz clock with enough cycles for 12 txns at one txn per ~10 cycles.
    posedges = 240
    w = VcdWriter(timescale="1ns")
    emit_clock(w, path="top.clk", posedges=posedges)
    declare_bundle_signals(w, spec)
    initialize_bundle_zero(w, spec)

    # Read txns: (cycle_ar, cycle_r, txn_id, resp)
    read_txns = [
        (3, 5, 0, 0),  # OKAY
        (15, 17, 1, 0),  # OKAY
        (30, 32, 2, 2),  # SLVERR
        (45, 47, 3, 2),  # SLVERR
        (60, 62, 4, 3),  # DECERR
        (75, 77, 5, 3),  # DECERR
    ]
    # Write txns: (cycle_aw, cycle_b, txn_id, resp). W beats follow AW by 1.
    write_txns = [
        (100, 105, 0, 0),  # OKAY
        (115, 120, 1, 0),  # OKAY
        (130, 135, 2, 2),  # SLVERR
        (145, 150, 3, 2),  # SLVERR
        (160, 165, 4, 3),  # DECERR
        (175, 180, 5, 3),  # DECERR
    ]
    half = 5
    sigp = f"top.u_cpu.{spec.signal_prefix}"

    def _at_posedge(cycle: int) -> int:
        """Drive starts 5ns before the posedge so wellen samples them as 1."""
        return (2 * cycle - 1) * half

    def _after_posedge(cycle: int) -> int:
        return (2 * cycle + 1) * half

    for cycle_ar, cycle_r, txn_id, resp in read_txns:
        # AR handshake on cycle_ar's posedge.
        t = _at_posedge(cycle_ar)
        w.change(t, f"{sigp}arvalid", 1)
        w.change(t, f"{sigp}arready", 1)
        w.change(t, f"{sigp}arid", txn_id)
        w.change(t, f"{sigp}araddr", 0x1000 + txn_id * 8)
        w.change(_after_posedge(cycle_ar), f"{sigp}arvalid", 0)
        w.change(_after_posedge(cycle_ar), f"{sigp}arready", 0)

        # R handshake on cycle_r's posedge with RLAST.
        t = _at_posedge(cycle_r)
        w.change(t, f"{sigp}rvalid", 1)
        w.change(t, f"{sigp}rready", 1)
        w.change(t, f"{sigp}rid", txn_id)
        w.change(t, f"{sigp}rresp", resp)
        w.change(t, f"{sigp}rlast", 1)
        w.change(_after_posedge(cycle_r), f"{sigp}rvalid", 0)
        w.change(_after_posedge(cycle_r), f"{sigp}rready", 0)
        w.change(_after_posedge(cycle_r), f"{sigp}rlast", 0)
        w.change(_after_posedge(cycle_r), f"{sigp}rresp", 0)

    for cycle_aw, cycle_b, txn_id, resp in write_txns:
        # AW handshake.
        t = _at_posedge(cycle_aw)
        w.change(t, f"{sigp}awvalid", 1)
        w.change(t, f"{sigp}awready", 1)
        w.change(t, f"{sigp}awid", txn_id)
        w.change(t, f"{sigp}awaddr", 0x2000 + txn_id * 8)
        w.change(_after_posedge(cycle_aw), f"{sigp}awvalid", 0)
        w.change(_after_posedge(cycle_aw), f"{sigp}awready", 0)

        # W beat with WLAST one cycle after AW.
        t = _at_posedge(cycle_aw + 1)
        w.change(t, f"{sigp}wvalid", 1)
        w.change(t, f"{sigp}wready", 1)
        w.change(t, f"{sigp}wlast", 1)
        w.change(_after_posedge(cycle_aw + 1), f"{sigp}wvalid", 0)
        w.change(_after_posedge(cycle_aw + 1), f"{sigp}wready", 0)
        w.change(_after_posedge(cycle_aw + 1), f"{sigp}wlast", 0)

        # B handshake.
        t = _at_posedge(cycle_b)
        w.change(t, f"{sigp}bvalid", 1)
        w.change(t, f"{sigp}bready", 1)
        w.change(t, f"{sigp}bid", txn_id)
        w.change(t, f"{sigp}bresp", resp)
        w.change(_after_posedge(cycle_b), f"{sigp}bvalid", 0)
        w.change(_after_posedge(cycle_b), f"{sigp}bready", 0)
        w.change(_after_posedge(cycle_b), f"{sigp}bresp", 0)

    write_vcd(w, fixture_dir)
    write_manifest(manifest, fixture_dir)
    _write_golden(fixture_dir, manifest)


# --- driver ----------------------------------------------------------------


FIXTURE_BUILDERS: list[tuple[str, Callable[[], None]]] = [
    ("errors", build_errors),
]


def _write_golden(fixture_dir: Path, manifest: Manifest) -> None:
    """Run the pipeline on the fixture's dump.vcd and write the golden."""
    vcd_path = fixture_dir / "dump.vcd"
    ingest = WellenIngest()
    events = ingest.run(vcd_path, manifest)
    txns = reconstruct(events)
    clock = ingest.detected_clock
    cycles = len(clock.posedge_times) if clock else 0
    period_ns = (clock.period_fs / 1e6) if clock else 1.0
    stats = aggregate(txns, manifest, duration_cycles=cycles, clock_period_ns=period_ns)
    payload = build_payload(
        stats, manifest, tool="rtl-buddy-axi-profiler", tool_version="golden"
    )
    # Volatile fields are excluded by the diff helper but we still want
    # a stable on-disk representation.
    payload["produced_at"] = "1970-01-01T00:00:00Z"
    (fixture_dir / GOLDEN_NAME).write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    for name, builder in FIXTURE_BUILDERS:
        print(f"building {name} ...")
        builder()
    print(f"done — {len(FIXTURE_BUILDERS)} fixture(s) written.")


if __name__ == "__main__":
    main()
