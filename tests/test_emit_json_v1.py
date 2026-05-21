"""Tests for the JsonEmitV1 stage.

Confirm that emit produces schema-valid JSON from synthetic
AggregateStats, including hierarchical bundles and per-channel
zero defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from rtl_buddy_axi_profiler.stages.emit.json_v1 import JsonEmitV1, build_payload, emit
from rtl_buddy_axi_profiler.types import (
    AggregateStats,
    Bundle,
    BundleSource,
    BundleStats,
    Channel,
    ChannelStats,
    DefaultView,
    InterconnectStats,
    LatencyStats,
    Manifest,
    Protocol,
)


def _make_manifest_and_stats() -> tuple[Manifest, AggregateStats]:
    child = Bundle(
        name="xbar_to_dram",
        master_path="soc.u_xbar",
        slave_path="soc.u_dram",
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
    )
    parent = Bundle(
        name="cpu_to_xbar",
        master_path="soc.u_cpu",
        slave_path="soc.u_xbar",
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.BOTH,
        children=(child,),
    )
    manifest = Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(parent,),
        generated_by="test",
        generated_at="2026-05-21T08:00:00Z",
    )

    parent_stats = BundleStats(
        bundle=parent,
        read_bps=1.0e9,
        write_bps=0.5e9,
        read_peak=8,
        read_avg=4.0,
        write_peak=2,
        write_avg=1.0,
        ar_to_r_first=LatencyStats(p50=10, p95=40, p99=80, max=120, hist_log2=[1] * 16),
        aw_to_b=LatencyStats(p50=20, p95=60, p99=100, max=200, hist_log2=[0] * 16),
        slverr=0,
        decerr=1,
    )
    parent_stats.channels = {ch: ChannelStats() for ch in Channel}
    child_stats = BundleStats(
        bundle=child,
        ar_to_r_first=LatencyStats(hist_log2=[0] * 16),
        aw_to_b=LatencyStats(hist_log2=[0] * 16),
    )
    child_stats.channels = {ch: ChannelStats() for ch in Channel}
    parent_stats.children = [child_stats]

    interconnects = [
        InterconnectStats(
            node_path="soc.u_xbar",
            total_read_bps=2.0e9,
            total_write_bps=1.0e9,
            hottest_master="soc.u_cpu",
            hottest_slave="soc.u_dram",
            fairness_jain=0.85,
            starved_masters=[],
        )
    ]

    stats = AggregateStats(
        design_top="soc",
        duration_cycles=1000,
        clock_period_ns=2.0,
        bundles=[parent_stats],
        interconnects=interconnects,
    )
    return manifest, stats


def test_emit_writes_schema_valid_json(tmp_path: Path) -> None:
    manifest, stats = _make_manifest_and_stats()
    out = tmp_path / "axi-perf.json"
    emit(stats, manifest, out)
    payload = json.loads(out.read_text())
    # Schema enforcement already happened inside emit; assert payload
    # round-trips and a re-validation also passes (paranoia).
    from importlib import resources

    import rtl_buddy_axi_profiler.schema as schema_pkg

    schema = json.loads((resources.files(schema_pkg) / "axi_perf_v1.json").read_text())
    Draft202012Validator(schema).validate(payload)


def test_emit_carries_hierarchical_children() -> None:
    manifest, stats = _make_manifest_and_stats()
    payload = build_payload(stats, manifest)
    assert len(payload["bundles"]) == 1
    parent = payload["bundles"][0]
    assert parent["default_view"] == "both"
    assert len(parent["children"]) == 1
    child = parent["children"][0]
    assert child["name"] == "xbar_to_dram"
    assert child["master_path"] == "soc.u_xbar"


def test_emit_per_channel_defaults_when_channels_missing(tmp_path: Path) -> None:
    """If BundleStats.channels is empty, emit still produces all 5
    schema-required channel entries with zero metrics."""
    manifest, stats = _make_manifest_and_stats()
    stats.bundles[0].channels = {}
    out = tmp_path / "axi-perf.json"
    emit(stats, manifest, out)
    payload = json.loads(out.read_text())
    channels = payload["bundles"][0]["channels"]
    assert set(channels) == {"ar", "aw", "r", "w", "b"}
    for ch_data in channels.values():
        assert ch_data["util_pct"] == 0.0
        assert ch_data["bp_pct"] == 0.0


def test_json_emit_v1_protocol_wrapper(tmp_path: Path) -> None:
    """The JsonEmitV1 class is the entry-point-registered handle."""
    manifest, stats = _make_manifest_and_stats()
    out = tmp_path / "via-wrapper.json"
    JsonEmitV1().run(stats, manifest, out)
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == "1.0"
    assert payload["design_top"] == "soc"
