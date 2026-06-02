"""End-to-end golden harness for the FST→JSON pipeline.

Each fixture under ``tests/fixtures/e2e/<name>/`` ships a paired
waveform + manifest + golden axi-perf.json. The harness drives the
full pipeline (ingest → reconstruct → aggregate → emit) on the
fixture's inputs and compares the emitted JSON to the golden using
:mod:`tests._e2e_diff`'s tolerance bands (±0.5% on floats, exact
on counts / cycles / bandwidth).

Regenerate fixtures with ``uv run python tests/fixtures/e2e/build_fixtures.py``
after a stage contract changes — review the golden diff before
committing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_axi_profiler.stages.aggregate.standard import (
    aggregate,
    fill_channel_cycle_metrics,
)
from rtl_buddy_axi_profiler.stages.discover._load import load_manifest
from rtl_buddy_axi_profiler.stages.emit.json_v1 import build_payload
from rtl_buddy_axi_profiler.stages.ingest.wellen import WellenIngest
from rtl_buddy_axi_profiler.stages.reconstruct.axi4 import reconstruct

from tests._e2e_diff import diff_axi_perf
from tests.fixtures.e2e._build import DUMP_VCD_NAME, GOLDEN_NAME, MANIFEST_NAME

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "e2e"


def _discover_fixtures() -> list[str]:
    """Return the names of every fixture directory under e2e/."""
    if not FIXTURES_ROOT.exists():
        return []
    return sorted(
        d.name
        for d in FIXTURES_ROOT.iterdir()
        if d.is_dir() and (d / GOLDEN_NAME).exists()
    )


@pytest.mark.parametrize("fixture_name", _discover_fixtures())
def test_e2e_fixture_matches_golden(fixture_name: str) -> None:
    """Drive the pipeline on the fixture and assert the emitted JSON
    matches the checked-in golden within tolerance."""
    fixture_dir = FIXTURES_ROOT / fixture_name
    vcd = fixture_dir / DUMP_VCD_NAME
    manifest = load_manifest(fixture_dir / MANIFEST_NAME)
    golden = json.loads((fixture_dir / GOLDEN_NAME).read_text())

    ingest = WellenIngest()
    events = ingest.run(vcd, manifest)
    txns = reconstruct(events)
    clock = ingest.detected_clock
    assert clock is not None, f"{fixture_name}: clock detection failed"
    cycles = len(clock.posedge_times)
    period_ns = clock.period_fs / 1e6
    stats = aggregate(txns, manifest, duration_cycles=cycles, clock_period_ns=period_ns)
    fill_channel_cycle_metrics(stats, ingest.channel_cycle_stats, cycles)
    payload = build_payload(stats, manifest, tool_version="golden")

    diff = diff_axi_perf(payload, golden)
    assert not diff, "axi-perf.json diverged from golden:\n" + "\n".join(diff)


def test_errors_fixture_locks_in_slverr_decerr_counts() -> None:
    """Acceptance gate from #31: ``errors.slverr`` + ``errors.decerr``
    in the errors fixture's golden must match the constructed FST
    exactly (4 each — 2 reads + 2 writes of each response type)."""
    golden = json.loads((FIXTURES_ROOT / "errors" / GOLDEN_NAME).read_text())
    bundles = golden["bundles"]
    assert len(bundles) == 1
    errors = bundles[0]["errors"]
    assert errors["slverr"] == 4
    assert errors["decerr"] == 4


def test_single_master_fixture_has_realistic_latency_distribution() -> None:
    """The single_master_single_slave fixture sweeps AR→R latencies
    over (2, 3, 4, 5) cycles, so p50 and max must differ — guards
    against a regression where the percentile path collapses to a
    single bucket."""
    golden = json.loads(
        (FIXTURES_ROOT / "single_master_single_slave" / GOLDEN_NAME).read_text()
    )
    bundles = golden["bundles"]
    assert len(bundles) == 1
    ar_to_r = bundles[0]["latency_cycles"]["ar_to_r_first"]
    assert ar_to_r["max"] > ar_to_r["p50"], (
        "p50 == max: latency distribution collapsed; check the fixture"
    )
    throughput = bundles[0]["throughput"]
    assert throughput["read_bps"] > 0, "read throughput should be non-zero"
    assert throughput["write_bps"] > 0, "write throughput should be non-zero"


def test_out_of_order_fixture_reconstructs_all_eight_txns() -> None:
    """The out_of_order fixture issues 8 reads + 8 writes with unique
    IDs and scrambled responses — the reconstruct stage must match
    every (AR, R) and (AW, B) pair across the pending table. A
    regression in the matching path would drop one or more txns and
    collapse the histogram below 8 samples per direction."""
    golden = json.loads((FIXTURES_ROOT / "out_of_order" / GOLDEN_NAME).read_text())
    ar_hist = golden["bundles"][0]["latency_cycles"]["ar_to_r_first"]["hist_log2"]
    aw_hist = golden["bundles"][0]["latency_cycles"]["aw_to_b"]["hist_log2"]
    assert sum(ar_hist) == 8, (
        f"expected 8 read latencies, got {sum(ar_hist)} (pending table regression?)"
    )
    assert sum(aw_hist) == 8, (
        f"expected 8 write latencies, got {sum(aw_hist)} (pending table regression?)"
    )


def test_out_of_order_truth_table_is_present() -> None:
    """The expected_latencies.txt truth table must ship next to the
    fixture per #31's acceptance — it's the reviewer's sanity check
    against the pipeline's reconstruction."""
    truth = FIXTURES_ROOT / "out_of_order" / "expected_latencies.txt"
    assert truth.exists(), "expected_latencies.txt missing from out_of_order fixture"
    text = truth.read_text()
    r_lines = [ln for ln in text.splitlines() if ln.startswith("R ")]
    b_lines = [ln for ln in text.splitlines() if ln.startswith("B ")]
    assert len(r_lines) == 8, r_lines
    assert len(b_lines) == 8, b_lines


def test_crossbar_rollup_matches_member_sum() -> None:
    """Acceptance gate from #31: each interconnect node's
    ``total_read_bps`` + ``total_write_bps`` equal the sum of its
    contributing bundles' ``read_bps`` / ``write_bps`` within 0.1%.
    A regression in the rollup math would either drop a member or
    miscount one — both visible here."""
    golden = json.loads((FIXTURES_ROOT / "crossbar_2x2" / GOLDEN_NAME).read_text())
    by_slave: dict[str, list[dict]] = {}
    for b in golden["bundles"]:
        by_slave.setdefault(b["slave_path"], []).append(b)

    interconnects = {ic["node_path"]: ic for ic in golden["interconnects"]}
    assert len(interconnects) == 2, (
        "crossbar should produce 2 interconnect nodes (one per slave)"
    )

    for slave, members in by_slave.items():
        if len(members) < 2:
            continue  # Only multi-master slaves get rollups.
        ic = interconnects[slave]
        member_r = sum(m["throughput"]["read_bps"] for m in members)
        member_w = sum(m["throughput"]["write_bps"] for m in members)
        # ``read_bps`` is a float; 0.1% relative tolerance per the
        # acceptance criteria. Exact match expected in practice
        # because the rollup is a straight sum.
        rel_r = abs(ic["total_read_bps"] - member_r) / max(member_r, 1.0)
        rel_w = abs(ic["total_write_bps"] - member_w) / max(member_w, 1.0)
        assert rel_r < 0.001, (
            f"{slave}.total_read_bps={ic['total_read_bps']} != sum of members "
            f"({member_r}); rel diff {rel_r * 100:.3f}%"
        )
        assert rel_w < 0.001, (
            f"{slave}.total_write_bps={ic['total_write_bps']} != sum of members "
            f"({member_w}); rel diff {rel_w * 100:.3f}%"
        )
