"""JSON v1 emit stage.

Serializes :class:`AggregateStats` to a v1 ``axi-perf.json`` and
validates the payload against :mod:`schema.axi_perf_v1` before
writing — a malformed roll-up is caught here, not in the consumer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

import rtl_buddy_axi_profiler.schema as schema_pkg
from rtl_buddy_axi_profiler.types import (
    AggregateStats,
    Bundle,
    BundleStats,
    Channel,
    ChannelStats,
    InterconnectStats,
    LatencyStats,
    Manifest,
)


def emit(
    stats: AggregateStats,
    manifest: Manifest,
    out: Path,
    *,
    tool: str = "rtl-buddy-axi-profiler",
    tool_version: str | None = None,
) -> None:
    """Serialize ``stats`` to ``out`` as a v1 axi-perf.json."""
    payload = build_payload(stats, manifest, tool=tool, tool_version=tool_version)
    Draft202012Validator(_load_schema()).validate(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))


def build_payload(
    stats: AggregateStats,
    manifest: Manifest,
    *,
    tool: str = "rtl-buddy-axi-profiler",
    tool_version: str | None = None,
) -> dict[str, Any]:
    bundles_by_name = _bundles_by_name(manifest.bundles)
    return {
        "schema_version": "1.0",
        "tool": tool,
        "tool_version": tool_version or _read_version(),
        "produced_at": _now_iso(),
        "design_top": stats.design_top,
        "duration_cycles": stats.duration_cycles,
        "clock_period_ns": stats.clock_period_ns,
        "bundles": [_bundle_to_json(bs, bundles_by_name) for bs in stats.bundles],
        "interconnects": [_interconnect_to_json(ic) for ic in stats.interconnects],
    }


def _bundle_to_json(
    bs: BundleStats, manifest_bundles: dict[str, Bundle]
) -> dict[str, Any]:
    bundle = bs.bundle
    manifest_bundle = manifest_bundles.get(bundle.name, bundle)
    out: dict[str, Any] = {
        "name": bundle.name,
        "master_path": bundle.master_path,
        "slave_path": bundle.slave_path,
        "protocol": bundle.protocol.value,
        "data_width": bundle.data_width,
        "id_width": bundle.id_width,
        "default_view": bundle.default_view.value,
        "channels": _channels_to_json(bs.channels),
        "throughput": {
            "read_bps": bs.read_bps,
            "write_bps": bs.write_bps,
        },
        "outstanding": {
            "read_peak": bs.read_peak,
            "read_avg": bs.read_avg,
            "write_peak": bs.write_peak,
            "write_avg": bs.write_avg,
        },
        "latency_cycles": {
            "ar_to_r_first": _latency_to_json(bs.ar_to_r_first),
            "aw_to_b": _latency_to_json(bs.aw_to_b),
        },
        "errors": {
            "slverr": bs.slverr,
            "decerr": bs.decerr,
        },
    }
    # Children come from the manifest's hierarchy; their stats are
    # tracked in BundleStats.children.
    if bs.children:
        child_lookup = {c.bundle.name: c for c in bs.children}
        out["children"] = [
            _bundle_to_json(child_lookup[mc.name], {mc.name: mc, **manifest_bundles})
            for mc in manifest_bundle.children
            if mc.name in child_lookup
        ]
    return out


def _channels_to_json(channels: dict[Channel, ChannelStats]) -> dict[str, Any]:
    """Build the five required channel sub-objects.

    Channels missing from the stats dict emit zeros — schema-valid,
    informative once event-stream metrics are wired in.
    """
    fallback = ChannelStats()
    out: dict[str, Any] = {}
    for ch in Channel:
        cs = channels.get(ch, fallback)
        entry: dict[str, Any] = {
            "util_pct": cs.util_pct,
            "bp_pct": cs.bp_pct,
            "peak_occ": cs.peak_occ,
        }
        # AR/AW/B carry txns; R/W carry beats. Schema enforces.
        if ch in (Channel.R, Channel.W):
            entry["beats"] = cs.beats
        else:
            entry["txns"] = cs.txns
        out[ch.value] = entry
    return out


def _latency_to_json(stats: LatencyStats) -> dict[str, Any]:
    return {
        "p50": stats.p50,
        "p95": stats.p95,
        "p99": stats.p99,
        "max": stats.max,
        "hist_log2": list(stats.hist_log2),
    }


def _interconnect_to_json(ic: InterconnectStats) -> dict[str, Any]:
    return {
        "node_path": ic.node_path,
        "total_read_bps": ic.total_read_bps,
        "total_write_bps": ic.total_write_bps,
        "hottest_master": ic.hottest_master,
        "hottest_slave": ic.hottest_slave,
        "arbitration": {
            "fairness_jain": ic.fairness_jain,
            "starved_masters": list(ic.starved_masters),
        },
    }


def _bundles_by_name(bundles: tuple[Bundle, ...]) -> dict[str, Bundle]:
    out: dict[str, Bundle] = {}
    for b in bundles:
        out[b.name] = b
        for child in b.children:
            out[child.name] = child
    return out


def _load_schema() -> dict[str, Any]:
    text = (resources.files(schema_pkg) / "axi_perf_v1.json").read_text()
    return json.loads(text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_version() -> str:
    from importlib.metadata import version as _v

    try:
        return _v("rtl-buddy-axi-profiler")
    except Exception:
        return "0.0.0"


class JsonEmitV1:
    """:class:`Emit` Protocol implementation."""

    name = "json-v1"

    def run(self, stats: AggregateStats, manifest: Manifest, out: Path) -> None:
        emit(stats, manifest, out)
