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


# --- single_master_single_slave fixture ------------------------------------


def build_single_master_single_slave() -> None:
    """Minimal CPU→DRAM bundle exercising real percentile + throughput
    statistics. 100 reads + 100 writes = 200 txns total; AR→R latency
    cycles through (2, 3, 4, 5) so p50 ≠ p99; AW→B cycles through
    (3, 5, 7, 9). 5% of each direction returns SLVERR / DECERR so the
    error counts are non-zero but small.

    Lifts the txn-layer reconstruct + aggregate unit tests up to the
    FST→JSON layer in the most boring possible topology: one master,
    one slave, no hierarchy.
    """
    fixture_dir = FIXTURES_ROOT / "single_master_single_slave"
    fixture_dir.mkdir(exist_ok=True)

    spec = BundleSpec(
        name="cpu_to_dram",
        master_path="top.u_cpu",
        slave_path="top.u_dram",
        clock_signal="top.clk",
    )
    manifest = manifest_from([spec], design_top="top")

    n_reads = 100
    n_writes = 100
    ar_latencies = (2, 3, 4, 5)
    aw_latencies = (3, 5, 7, 9)
    # Cycle stride: enough room between txn starts to avoid AR/AW
    # collisions across the (variable) latency tails.
    read_stride = 8
    write_stride = 12
    read_base_cycle = 5
    write_base_cycle = read_base_cycle + n_reads * read_stride + 10
    end_cycle = write_base_cycle + n_writes * write_stride + max(aw_latencies) + 5

    posedges = end_cycle + 5
    half = 5
    w = VcdWriter(timescale="1ns")
    emit_clock(w, path="top.clk", posedges=posedges)
    declare_bundle_signals(w, spec)
    initialize_bundle_zero(w, spec)

    sigp = f"top.u_cpu.{spec.signal_prefix}"

    def _at(c: int) -> int:
        return (2 * c - 1) * half

    def _after(c: int) -> int:
        return (2 * c + 1) * half

    # --- reads ---
    for i in range(n_reads):
        cycle_ar = read_base_cycle + i * read_stride
        latency = ar_latencies[i % len(ar_latencies)]
        cycle_r = cycle_ar + latency
        txn_id = i % 16
        # Error injection: every 20th txn alternates SLVERR / DECERR.
        if i % 40 == 0:
            resp = 2  # SLVERR
        elif i % 40 == 20:
            resp = 3  # DECERR
        else:
            resp = 0

        t = _at(cycle_ar)
        w.change(t, f"{sigp}arvalid", 1)
        w.change(t, f"{sigp}arready", 1)
        w.change(t, f"{sigp}arid", txn_id)
        w.change(t, f"{sigp}araddr", 0x1000 + i * 64)
        w.change(_after(cycle_ar), f"{sigp}arvalid", 0)
        w.change(_after(cycle_ar), f"{sigp}arready", 0)

        t = _at(cycle_r)
        w.change(t, f"{sigp}rvalid", 1)
        w.change(t, f"{sigp}rready", 1)
        w.change(t, f"{sigp}rid", txn_id)
        w.change(t, f"{sigp}rresp", resp)
        w.change(t, f"{sigp}rlast", 1)
        w.change(_after(cycle_r), f"{sigp}rvalid", 0)
        w.change(_after(cycle_r), f"{sigp}rready", 0)
        w.change(_after(cycle_r), f"{sigp}rlast", 0)
        if resp != 0:
            w.change(_after(cycle_r), f"{sigp}rresp", 0)

    # --- writes ---
    for i in range(n_writes):
        cycle_aw = write_base_cycle + i * write_stride
        latency = aw_latencies[i % len(aw_latencies)]
        cycle_b = cycle_aw + latency
        txn_id = i % 16
        if i % 40 == 0:
            resp = 2  # SLVERR
        elif i % 40 == 20:
            resp = 3  # DECERR
        else:
            resp = 0

        t = _at(cycle_aw)
        w.change(t, f"{sigp}awvalid", 1)
        w.change(t, f"{sigp}awready", 1)
        w.change(t, f"{sigp}awid", txn_id)
        w.change(t, f"{sigp}awaddr", 0x4000 + i * 64)
        w.change(_after(cycle_aw), f"{sigp}awvalid", 0)
        w.change(_after(cycle_aw), f"{sigp}awready", 0)

        t = _at(cycle_aw + 1)
        w.change(t, f"{sigp}wvalid", 1)
        w.change(t, f"{sigp}wready", 1)
        w.change(t, f"{sigp}wlast", 1)
        w.change(_after(cycle_aw + 1), f"{sigp}wvalid", 0)
        w.change(_after(cycle_aw + 1), f"{sigp}wready", 0)
        w.change(_after(cycle_aw + 1), f"{sigp}wlast", 0)

        t = _at(cycle_b)
        w.change(t, f"{sigp}bvalid", 1)
        w.change(t, f"{sigp}bready", 1)
        w.change(t, f"{sigp}bid", txn_id)
        w.change(t, f"{sigp}bresp", resp)
        w.change(_after(cycle_b), f"{sigp}bvalid", 0)
        w.change(_after(cycle_b), f"{sigp}bready", 0)
        if resp != 0:
            w.change(_after(cycle_b), f"{sigp}bresp", 0)

    write_vcd(w, fixture_dir)
    write_manifest(manifest, fixture_dir)
    _write_golden(fixture_dir, manifest)


# --- out_of_order fixture --------------------------------------------------


# (issue_cycle, response_cycle, txn_id) tuples for the out_of_order fixture.
# Eight outstanding reads issued sequentially on cycles 5..12; responses
# arrive in scrambled order across cycles 22..50 with non-uniform spacing.
# Latency = response_cycle - issue_cycle; the truth table is dropped into
# tests/fixtures/e2e/out_of_order/expected_latencies.txt for reviewer
# sanity (acceptance gate from #31).
_OOO_READS: tuple[tuple[int, int, int], ...] = (
    (5, 30, 0),
    (6, 22, 1),
    (7, 45, 2),
    (8, 28, 3),
    (9, 38, 4),
    (10, 25, 5),
    (11, 50, 6),
    (12, 34, 7),
)
_OOO_WRITES: tuple[tuple[int, int, int], ...] = (
    (60, 88, 0),
    (61, 78, 1),
    (62, 95, 2),
    (63, 82, 3),
    (64, 92, 4),
    (65, 75, 5),
    (66, 100, 6),
    (67, 86, 7),
)


def build_out_of_order() -> None:
    """Interleaved AXI IDs with scrambled responses — locks in the
    reconstruct stage's pending-table behaviour end-to-end. Per the
    AXI4 spec the master must preserve order per ID but different
    IDs may be reordered; this fixture issues all eight outstanding
    reads sequentially under unique IDs, then returns them in a
    scrambled order chosen to maximise pending-table coverage:
    out-of-order matches, interior table positions, and the tail.

    The hand-computed truth table lives next to the fixture as
    ``expected_latencies.txt`` so a reviewer can sanity-check the
    pipeline's reconstruction without rerunning the harness.
    """
    fixture_dir = FIXTURES_ROOT / "out_of_order"
    fixture_dir.mkdir(exist_ok=True)

    spec = BundleSpec(
        name="cpu_to_mem",
        master_path="top.u_cpu",
        slave_path="top.u_mem",
        clock_signal="top.clk",
    )
    manifest = manifest_from([spec], design_top="top")

    last_cycle = max(
        max(r[1] for r in _OOO_READS),
        max(w[1] for w in _OOO_WRITES),
    )
    posedges = last_cycle + 10
    half = 5

    w = VcdWriter(timescale="1ns")
    emit_clock(w, path="top.clk", posedges=posedges)
    declare_bundle_signals(w, spec)
    initialize_bundle_zero(w, spec)

    sigp = f"top.u_cpu.{spec.signal_prefix}"

    def _at(c: int) -> int:
        return (2 * c - 1) * half

    def _after(c: int) -> int:
        return (2 * c + 1) * half

    # Back-to-back AR issues — hold valid + ready high through the
    # whole burst and change ``arid`` / ``araddr`` each cycle. Pulsing
    # valid 1→0→1 at the same VCD timestamp gets read as a glitch by
    # ``signal.value_at_time`` so the second AR collapses; holding
    # high keeps each posedge sample as its own (valid && ready) hit.
    first_ar_issue = min(r[0] for r in _OOO_READS)
    last_ar_issue = max(r[0] for r in _OOO_READS)
    w.change(_at(first_ar_issue), f"{sigp}arvalid", 1)
    w.change(_at(first_ar_issue), f"{sigp}arready", 1)
    for issue, _r, txn_id in _OOO_READS:
        t = _at(issue)
        w.change(t, f"{sigp}arid", txn_id)
        w.change(t, f"{sigp}araddr", 0x8000 + txn_id * 16)
    w.change(_after(last_ar_issue), f"{sigp}arvalid", 0)
    w.change(_after(last_ar_issue), f"{sigp}arready", 0)

    for _i, resp, txn_id in _OOO_READS:
        t = _at(resp)
        w.change(t, f"{sigp}rvalid", 1)
        w.change(t, f"{sigp}rready", 1)
        w.change(t, f"{sigp}rid", txn_id)
        w.change(t, f"{sigp}rlast", 1)
        w.change(_after(resp), f"{sigp}rvalid", 0)
        w.change(_after(resp), f"{sigp}rready", 0)
        w.change(_after(resp), f"{sigp}rlast", 0)

    # Same back-to-back logic for AW: hold valid + ready high through
    # the burst, change awid / awaddr each cycle.
    first_aw_issue = min(w_[0] for w_ in _OOO_WRITES)
    last_aw_issue = max(w_[0] for w_ in _OOO_WRITES)
    w.change(_at(first_aw_issue), f"{sigp}awvalid", 1)
    w.change(_at(first_aw_issue), f"{sigp}awready", 1)
    for issue, _r, txn_id in _OOO_WRITES:
        t = _at(issue)
        w.change(t, f"{sigp}awid", txn_id)
        w.change(t, f"{sigp}awaddr", 0x9000 + txn_id * 16)
    w.change(_after(last_aw_issue), f"{sigp}awvalid", 0)
    w.change(_after(last_aw_issue), f"{sigp}awready", 0)

    # W beats follow AW by one cycle — also held high through the
    # corresponding burst window.
    w.change(_at(first_aw_issue + 1), f"{sigp}wvalid", 1)
    w.change(_at(first_aw_issue + 1), f"{sigp}wready", 1)
    w.change(_at(first_aw_issue + 1), f"{sigp}wlast", 1)
    w.change(_after(last_aw_issue + 1), f"{sigp}wvalid", 0)
    w.change(_after(last_aw_issue + 1), f"{sigp}wready", 0)
    w.change(_after(last_aw_issue + 1), f"{sigp}wlast", 0)

    for _i, resp, txn_id in _OOO_WRITES:
        t = _at(resp)
        w.change(t, f"{sigp}bvalid", 1)
        w.change(t, f"{sigp}bready", 1)
        w.change(t, f"{sigp}bid", txn_id)
        w.change(_after(resp), f"{sigp}bvalid", 0)
        w.change(_after(resp), f"{sigp}bready", 0)

    write_vcd(w, fixture_dir)
    write_manifest(manifest, fixture_dir)
    _write_expected_latencies(fixture_dir)
    _write_golden(fixture_dir, manifest)


def _write_expected_latencies(fixture_dir: Path) -> None:
    """Drop the hand-computed truth table next to the fixture.

    Per #31's acceptance: a reviewer should be able to sanity-check
    the reconstructed latencies without rerunning the pipeline.
    """
    lines = [
        "# AR->R latency truth table (cycles)",
        "# Columns: kind txn_id  issue_cycle  resp_cycle  latency_cycles",
    ]
    for issue, resp, txn_id in _OOO_READS:
        lines.append(f"R  {txn_id}  {issue:3d}  {resp:3d}  {resp - issue:3d}")
    lines.append("")
    lines.append("# AW->B latency truth table (cycles)")
    lines.append("# Columns: kind txn_id  issue_cycle  resp_cycle  latency_cycles")
    for issue, resp, txn_id in _OOO_WRITES:
        lines.append(f"B  {txn_id}  {issue:3d}  {resp:3d}  {resp - issue:3d}")
    (fixture_dir / "expected_latencies.txt").write_text("\n".join(lines) + "\n")


# --- driver ----------------------------------------------------------------


FIXTURE_BUILDERS: list[tuple[str, Callable[[], None]]] = [
    ("errors", build_errors),
    ("single_master_single_slave", build_single_master_single_slave),
    ("out_of_order", build_out_of_order),
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
