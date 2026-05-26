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

from rtl_buddy_axi_profiler.stages.aggregate.standard import aggregate
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
