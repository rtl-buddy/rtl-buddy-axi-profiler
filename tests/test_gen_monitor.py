"""Tests for the gen-monitor SV codegen (#4).

The generated SV isn't compiled here (no Verilator in the test
environment). We assert on the rendered text: it includes per-bundle
modules, the top wrapper, signal-path cross-references, and the
timeprecision header.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rtl_buddy_axi_profiler.stages.gen_monitor.generator import (
    GenMonitorError,
    list_templates,
    render_monitor,
    write_monitor,
)


def _write_manifest(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "1.0",
        "generated_by": "test",
        "generated_at": "2026-05-21T08:00:00Z",
        "design_top": "soc_top",
        "bundles": [
            {
                "name": "cpu_to_dram",
                "master_path": "soc_top.u_cpu",
                "slave_path": "soc_top.u_dram",
                "protocol": "AXI4",
                "data_width": 64,
                "id_width": 4,
                "source": "verible-regex",
                "default_view": "parent",
                "signals": {
                    "arvalid": "soc_top.u_cpu.m_axi_arvalid",
                    "arready": "soc_top.u_cpu.m_axi_arready",
                    "araddr": "soc_top.u_cpu.m_axi_araddr",
                    "arid": "soc_top.u_cpu.m_axi_arid",
                    "arlen": "soc_top.u_cpu.m_axi_arlen",
                    "arsize": "soc_top.u_cpu.m_axi_arsize",
                    "awvalid": "soc_top.u_cpu.m_axi_awvalid",
                    "awready": "soc_top.u_cpu.m_axi_awready",
                    "awaddr": "soc_top.u_cpu.m_axi_awaddr",
                    "awid": "soc_top.u_cpu.m_axi_awid",
                    "awlen": "soc_top.u_cpu.m_axi_awlen",
                    "awsize": "soc_top.u_cpu.m_axi_awsize",
                    "rvalid": "soc_top.u_cpu.m_axi_rvalid",
                    "rready": "soc_top.u_cpu.m_axi_rready",
                    "rid": "soc_top.u_cpu.m_axi_rid",
                    "rresp": "soc_top.u_cpu.m_axi_rresp",
                    "rlast": "soc_top.u_cpu.m_axi_rlast",
                    "wvalid": "soc_top.u_cpu.m_axi_wvalid",
                    "wready": "soc_top.u_cpu.m_axi_wready",
                    "wlast": "soc_top.u_cpu.m_axi_wlast",
                    "bvalid": "soc_top.u_cpu.m_axi_bvalid",
                    "bready": "soc_top.u_cpu.m_axi_bready",
                    "bid": "soc_top.u_cpu.m_axi_bid",
                    "bresp": "soc_top.u_cpu.m_axi_bresp",
                },
            }
        ],
    }
    path = tmp_path / "axi-bundles.yaml"
    path.write_text(yaml.safe_dump(payload))
    return path


def test_render_includes_bundle_module(tmp_path: Path) -> None:
    rendered = render_monitor(_write_manifest(tmp_path))
    assert "module axi_perf_mon_cpu_to_dram" in rendered
    assert "module axi_perf_mon (" in rendered  # top wrapper
    assert "axi_perf_mon_cpu_to_dram u_cpu_to_dram" in rendered  # instantiation


def test_render_carries_timeprecision_into_timescale(tmp_path: Path) -> None:
    rendered = render_monitor(_write_manifest(tmp_path), time_precision="100ps")
    assert "1ns/100ps" in rendered


def test_render_rejects_invalid_timeprecision(tmp_path: Path) -> None:
    with pytest.raises(GenMonitorError):
        render_monitor(_write_manifest(tmp_path), time_precision="3.7ns")


def test_render_includes_bind_hint(tmp_path: Path) -> None:
    """The top wrapper documents the bind directive users paste."""
    rendered = render_monitor(_write_manifest(tmp_path))
    assert "bind soc_top axi_perf_mon" in rendered


def test_write_monitor_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "mon.sv"
    write_monitor(_write_manifest(tmp_path), out)
    assert out.is_file()
    text = out.read_text()
    assert "axi_perf_mon_cpu_to_dram" in text


def test_render_rejects_empty_bundle_list(tmp_path: Path) -> None:
    manifest = tmp_path / "axi-bundles.yaml"
    manifest.write_text(
        yaml.safe_dump({"schema_version": "1.0", "design_top": "soc", "bundles": []})
    )
    with pytest.raises(GenMonitorError):
        render_monitor(manifest)


def test_list_templates_finds_jinja_template() -> None:
    names = list_templates()
    assert "axi_perf_mon.sv.j2" in names


def test_render_handles_hierarchical_bundles(tmp_path: Path) -> None:
    """A parent bundle with children should emit modules for both."""
    payload = {
        "schema_version": "1.0",
        "design_top": "soc",
        "bundles": [
            {
                "name": "ext",
                "master_path": "soc.u_cpu",
                "slave_path": "soc.u_xbar",
                "protocol": "AXI4",
                "data_width": 64,
                "id_width": 4,
                "source": "user",
                "default_view": "parent",
                "signals": {
                    role: f"soc.u_cpu.m_axi_{role}"
                    for role in (
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
                },
                "children": [
                    {
                        "name": "inner",
                        "master_path": "soc.u_xbar",
                        "slave_path": "soc.u_dram",
                        "protocol": "AXI4",
                        "data_width": 64,
                        "id_width": 4,
                        "source": "user",
                        "signals": {
                            role: f"soc.u_xbar.s_axi_{role}"
                            for role in (
                                "arvalid",
                                "arready",
                                "araddr",
                                "awvalid",
                                "awready",
                                "awaddr",
                                "rvalid",
                                "rready",
                                "wvalid",
                                "wready",
                                "bvalid",
                                "bready",
                            )
                        },
                    }
                ],
            }
        ],
    }
    path = tmp_path / "axi-bundles.yaml"
    path.write_text(yaml.safe_dump(payload))
    rendered = render_monitor(path)
    assert "module axi_perf_mon_ext" in rendered
    assert "module axi_perf_mon_inner" in rendered
