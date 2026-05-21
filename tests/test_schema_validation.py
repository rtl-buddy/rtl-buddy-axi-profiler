"""Round-trip the example payloads from issue #1 through the v1 schemas.

These are the trust-boundary tests for the bootstrap: anything that
emits these JSON / YAML shapes must produce payloads that pass these
validators. Schema breaks are downstream-breaking.
"""

from __future__ import annotations

import json
import struct
from importlib import resources

import pytest
import yaml
from jsonschema import Draft202012Validator

import rtl_buddy_axi_profiler.schema as schema_pkg


def _load_schema(name: str) -> dict:
    text = (resources.files(schema_pkg) / name).read_text()
    return json.loads(text)


# ---- axi-perf.json -------------------------------------------------


AXI_PERF_EXAMPLE: dict = {
    "schema_version": "1.0",
    "tool": "rtl-buddy-axi-profiler",
    "tool_version": "0.1.0",
    "produced_at": "2026-05-21T08:00:00Z",
    "design_top": "soc_top",
    "duration_cycles": 1000000,
    "clock_period_ns": 2.0,
    "bundles": [
        {
            "name": "cpu_to_soc_xbar",
            "master_path": "soc_top.u_cpu",
            "slave_path": "soc_top.u_soc_xbar",
            "protocol": "AXI4",
            "data_width": 64,
            "id_width": 4,
            "default_view": "parent",
            "channels": {
                "ar": {"util_pct": 32.1, "bp_pct": 4.2, "peak_occ": 12, "txns": 41023},
                "aw": {"util_pct": 18.7, "bp_pct": 1.1, "peak_occ": 6, "txns": 22987},
                "r": {
                    "util_pct": 71.5,
                    "bp_pct": 22.4,
                    "peak_occ": 28,
                    "beats": 328184,
                },
                "w": {"util_pct": 41.3, "bp_pct": 8.9, "peak_occ": 9, "beats": 91948},
                "b": {"util_pct": 9.8, "bp_pct": 0.3, "peak_occ": 3, "txns": 22987},
            },
            "throughput": {"read_bps": 1.31e9, "write_bps": 0.59e9},
            "outstanding": {
                "read_peak": 28,
                "read_avg": 12.4,
                "write_peak": 9,
                "write_avg": 3.7,
            },
            "latency_cycles": {
                "ar_to_r_first": {
                    "p50": 18,
                    "p95": 76,
                    "p99": 142,
                    "max": 410,
                    "hist_log2": [
                        0,
                        0,
                        12,
                        308,
                        4102,
                        28311,
                        7884,
                        412,
                        18,
                        4,
                        1,
                        0,
                        0,
                        0,
                        0,
                        0,
                    ],
                },
                "aw_to_b": {
                    "p50": 22,
                    "p95": 80,
                    "p99": 160,
                    "max": 512,
                    "hist_log2": [0] * 16,
                },
            },
            "errors": {"slverr": 0, "decerr": 2},
            "children": [
                {
                    "name": "xbar_to_dram_ctrl",
                    "master_path": "soc_top.u_soc_xbar",
                    "slave_path": "soc_top.u_dram_ctrl",
                    "protocol": "AXI4",
                    "data_width": 64,
                    "id_width": 4,
                    "channels": {
                        "ar": {
                            "util_pct": 25.0,
                            "bp_pct": 2.0,
                            "peak_occ": 8,
                            "txns": 30000,
                        },
                        "aw": {
                            "util_pct": 15.0,
                            "bp_pct": 1.0,
                            "peak_occ": 4,
                            "txns": 18000,
                        },
                        "r": {
                            "util_pct": 60.0,
                            "bp_pct": 15.0,
                            "peak_occ": 20,
                            "beats": 240000,
                        },
                        "w": {
                            "util_pct": 35.0,
                            "bp_pct": 7.0,
                            "peak_occ": 6,
                            "beats": 72000,
                        },
                        "b": {
                            "util_pct": 8.0,
                            "bp_pct": 0.2,
                            "peak_occ": 2,
                            "txns": 18000,
                        },
                    },
                    "throughput": {"read_bps": 1.0e9, "write_bps": 0.45e9},
                    "outstanding": {
                        "read_peak": 20,
                        "read_avg": 10.0,
                        "write_peak": 6,
                        "write_avg": 2.5,
                    },
                    "latency_cycles": {
                        "ar_to_r_first": {
                            "p50": 16,
                            "p95": 64,
                            "p99": 120,
                            "max": 380,
                            "hist_log2": [0] * 16,
                        },
                        "aw_to_b": {
                            "p50": 20,
                            "p95": 72,
                            "p99": 140,
                            "max": 460,
                            "hist_log2": [0] * 16,
                        },
                    },
                    "errors": {"slverr": 0, "decerr": 0},
                },
            ],
        },
    ],
    "interconnects": [
        {
            "node_path": "soc_top.u_soc_xbar",
            "total_read_bps": 4.2e9,
            "total_write_bps": 2.8e9,
            "hottest_master": "soc_top.u_cpu",
            "hottest_slave": "soc_top.u_dram_ctrl",
            "arbitration": {"fairness_jain": 0.78, "starved_masters": []},
        },
    ],
}


def test_axi_perf_example_validates() -> None:
    Draft202012Validator(_load_schema("axi_perf_v1.json")).validate(AXI_PERF_EXAMPLE)


def test_axi_perf_rejects_bad_schema_version() -> None:
    bad = dict(AXI_PERF_EXAMPLE, schema_version="0.9")
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("axi_perf_v1.json")).validate(bad)


def test_axi_perf_rejects_wrong_hist_size() -> None:
    bad = json.loads(json.dumps(AXI_PERF_EXAMPLE))
    bad["bundles"][0]["latency_cycles"]["ar_to_r_first"]["hist_log2"] = [0] * 15
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("axi_perf_v1.json")).validate(bad)


# ---- axi-bundles.yaml ---------------------------------------------


AXI_BUNDLES_EXAMPLE_YAML = """
schema_version: "1.0"
generated_by: rtl-buddy-axi-profiler 0.1.0
generated_at: "2026-05-21T08:00:00Z"
design_top: soc_top
bundles:
  - name: cpu_to_soc_xbar
    master_path: soc_top.u_cpu
    slave_path: soc_top.u_soc_xbar
    protocol: AXI4
    data_width: 64
    id_width: 4
    source: verible-interface
    default_view: parent
    signals:
      arvalid: soc_top.u_cpu.m_axi_arvalid
      arready: soc_top.u_cpu.m_axi_arready
      araddr:  soc_top.u_cpu.m_axi_araddr
    children:
      - name: xbar_to_dram_ctrl
        master_path: soc_top.u_soc_xbar
        slave_path: soc_top.u_dram_ctrl
        protocol: AXI4
        data_width: 64
        id_width: 4
        source: verible-interface
        signals: {}
"""


def test_axi_bundles_example_validates() -> None:
    payload = yaml.safe_load(AXI_BUNDLES_EXAMPLE_YAML)
    Draft202012Validator(_load_schema("axi_bundles_v1.json")).validate(payload)


def test_axi_bundles_rejects_unknown_source() -> None:
    payload = yaml.safe_load(AXI_BUNDLES_EXAMPLE_YAML)
    payload["bundles"][0]["source"] = "magic"
    with pytest.raises(Exception):
        Draft202012Validator(_load_schema("axi_bundles_v1.json")).validate(payload)


# ---- axi-stream binary ---------------------------------------------


def test_axi_stream_header_size_matches_spec() -> None:
    """The spec says 32-byte header, 64-byte bundle entry, 24-byte record."""
    # File header (32 bytes): u32 magic, u16 version, u16 flags, u16 bundle_n,
    # u8 channel_n, u8 _pad, u32 time_unit, u64 start_time, u64 _reserved.
    assert struct.calcsize("<IHHHBBIQQ") == 32

    # Bundle table entry (64 bytes): u16 id, u16 parent, u16 data_width,
    # u8 id_width, u8 protocol, char[56] name.
    assert struct.calcsize("<HHHBB56s") == 64

    # Record (24 bytes): u32 t_delta, u16 bundle_id, u8 channel, u8 event_type,
    # u16 txn_id, u8 resp, u8 last, u64 addr, u8 len, u8 size, u16 _pad.
    assert struct.calcsize("<IHBBHBBQBBH") == 24


def test_axi_stream_magic_constant() -> None:
    """'AXIS' in little-endian must round-trip to 0x53495841."""
    assert int.from_bytes(b"AXIS", "little") == 0x53495841
