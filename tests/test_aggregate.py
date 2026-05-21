"""Tests for the standard aggregate stage.

Synthetic-Transaction fixtures only — no FST/VCD here. The
event-stream-derived metrics (channel util%, bp%, peak_occ) are
deferred until reconstruct lands; this PR exercises the
transaction-derived metrics only.
"""

from __future__ import annotations

from rtl_buddy_axi_profiler.stages.aggregate.standard import (
    StandardAggregate,
    _build_log2_hist,
    _jain_fairness,
    _percentile,
    aggregate,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Manifest,
    Protocol,
    Transaction,
)


def _bundle(
    name: str = "cpu_to_dram",
    master: str = "soc.u_cpu",
    slave: str = "soc.u_dram",
    data_width: int = 64,
) -> Bundle:
    return Bundle(
        name=name,
        master_path=master,
        slave_path=slave,
        protocol=Protocol.AXI4,
        data_width=data_width,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
    )


def _read_txn(
    *,
    bundle: str = "cpu_to_dram",
    txn_id: int = 1,
    len_beats: int = 0,
    t_start: int = 0,
    t_first: int = 10,
    t_end: int = 20,
    resp: int = 0,
) -> Transaction:
    return Transaction(
        bundle_name=bundle,
        is_read=True,
        txn_id=txn_id,
        addr=0,
        len_beats=len_beats,
        size_log2=3,
        t_start_fs=t_start,
        t_first_data_fs=t_first,
        t_end_fs=t_end,
        resp=resp,
    )


def _write_txn(
    *,
    bundle: str = "cpu_to_dram",
    txn_id: int = 1,
    len_beats: int = 0,
    t_start: int = 0,
    t_end: int = 20,
    resp: int = 0,
) -> Transaction:
    return Transaction(
        bundle_name=bundle,
        is_read=False,
        txn_id=txn_id,
        addr=0,
        len_beats=len_beats,
        size_log2=3,
        t_start_fs=t_start,
        t_first_data_fs=t_start,
        t_end_fs=t_end,
        resp=resp,
    )


def test_aggregate_empty_manifest_yields_empty_bundles() -> None:
    manifest = Manifest(schema_version="1.0", design_top="top", bundles=())
    stats = aggregate(iter([]), manifest, duration_cycles=0, clock_period_ns=1.0)
    assert stats.bundles == []
    assert stats.interconnects == []


def test_aggregate_counts_reads_and_writes() -> None:
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(_bundle(),),
    )
    txns = [
        _read_txn(txn_id=1, len_beats=0),
        _read_txn(txn_id=2, len_beats=3, t_start=10, t_first=15, t_end=30),
        _write_txn(txn_id=1, len_beats=1, t_start=5, t_end=25),
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=2.0)
    assert len(stats.bundles) == 1
    bs = stats.bundles[0]
    # 2 read txns, 1 + 4 read beats = 5 total
    assert bs.bundle.name == "cpu_to_dram"
    # 1 write txn, 2 write beats
    # Latency reservoir captured the two read latencies (t_first - t_start)
    assert bs.ar_to_r_first.max == 10 or bs.ar_to_r_first.max == 5
    # Throughput is computed from total bytes / elapsed time
    assert bs.read_bps > 0
    assert bs.write_bps > 0


def test_aggregate_counts_errors() -> None:
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [
        _read_txn(resp=2),  # SLVERR
        _read_txn(resp=3),  # DECERR
        _read_txn(resp=0),  # OKAY
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=0, clock_period_ns=1.0)
    bs = stats.bundles[0]
    assert bs.slverr == 1
    assert bs.decerr == 1


def test_interconnect_rollup_requires_two_masters() -> None:
    """A roll-up only fires when ≥2 distinct masters share the same slave."""
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(
            _bundle(name="cpu_to_dram", master="soc.u_cpu", slave="soc.u_dram"),
            _bundle(name="dma_to_dram", master="soc.u_dma", slave="soc.u_dram"),
        ),
    )
    txns = [
        _read_txn(bundle="cpu_to_dram", t_end=100),
        _write_txn(bundle="dma_to_dram", t_end=100),
    ]
    stats = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=1.0)
    assert len(stats.interconnects) == 1
    ic = stats.interconnects[0]
    assert ic.node_path == "soc.u_dram"
    assert ic.hottest_slave == "soc.u_dram"
    assert ic.hottest_master in ("soc.u_cpu", "soc.u_dma")
    assert 0.0 <= ic.fairness_jain <= 1.0


def test_interconnect_rollup_skips_lone_master() -> None:
    """A slave with only one master upstream isn't an interconnect."""
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(_bundle(),),
    )
    txns = [_read_txn()]
    stats = aggregate(iter(txns), manifest, duration_cycles=10, clock_period_ns=1.0)
    assert stats.interconnects == []


def test_unknown_bundle_in_stream_is_silently_dropped() -> None:
    """Transactions for bundles not in the manifest are ignored."""
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [_read_txn(bundle="ghost"), _read_txn(bundle="cpu_to_dram")]
    stats = aggregate(iter(txns), manifest, duration_cycles=10, clock_period_ns=1.0)
    bs = stats.bundles[0]
    # Only one of the two reads should be counted.
    assert bs.read_bps > 0


def test_percentile_is_monotonic() -> None:
    samples = sorted([1, 5, 10, 20, 50, 100, 200, 500])
    p50 = _percentile(samples, 0.50)
    p95 = _percentile(samples, 0.95)
    p99 = _percentile(samples, 0.99)
    assert p50 <= p95 <= p99
    # 8 samples, idx = 0.50 * 7 = 3.5 → interp between samples[3]=20 and samples[4]=50.
    assert p50 == 35


def test_log2_hist_shape_and_count() -> None:
    samples = [1, 2, 3, 4, 5, 100, 1000, 999_999]
    hist = _build_log2_hist(samples)
    assert len(hist) == 16
    assert sum(hist) == len(samples)
    # 1 → bucket 0, 2-3 → bucket 1, 4-5 → bucket 2
    assert hist[0] == 1
    assert hist[1] == 2
    assert hist[2] == 2


def test_jain_fairness_equal_shares_is_one() -> None:
    assert _jain_fairness([5.0, 5.0, 5.0]) == 1.0


def test_jain_fairness_one_master_dominant_drops() -> None:
    fair = _jain_fairness([100.0, 1.0, 1.0])
    assert 0.0 < fair < 1.0


def test_standard_aggregate_wrapper_matches_direct_call() -> None:
    manifest = Manifest(schema_version="1.0", design_top="soc", bundles=(_bundle(),))
    txns = [_read_txn(), _write_txn()]
    direct = aggregate(iter(txns), manifest, duration_cycles=100, clock_period_ns=2.0)
    wrapped = StandardAggregate(duration_cycles=100, clock_period_ns=2.0).run(
        iter([_read_txn(), _write_txn()]), manifest
    )
    assert wrapped.duration_cycles == direct.duration_cycles
    assert len(wrapped.bundles) == len(direct.bundles)
