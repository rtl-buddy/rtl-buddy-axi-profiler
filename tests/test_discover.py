"""Tests for the discover stage (Stage 1, #2).

v1 covers the regex detector + net-traced endpoint pairing on a
hand-crafted SV fixture. Interface-modport detection, hierarchy
resolution, and amend pass are tracked as follow-ups.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from rtl_buddy_axi_profiler.stages.discover._sv_parser import parse_files
from rtl_buddy_axi_profiler.stages.discover.verible import (
    VeribleDiscover,
    discover_to_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "discover"


def _load_axi_bundles_schema() -> dict:
    from importlib import resources

    import rtl_buddy_axi_profiler.schema as schema_pkg

    import json

    text = (resources.files(schema_pkg) / "axi_bundles_v1.json").read_text()
    return json.loads(text)


def test_sv_parser_finds_modules_and_instances() -> None:
    """Sanity: the regex SV parser picks up the three fixture modules
    and their cross-module instantiations."""
    fixture = FIXTURES / "single_pair"
    files = [fixture / "cpu.sv", fixture / "dram.sv", fixture / "soc_top.sv"]
    design = parse_files(files, top="soc_top")
    assert set(design.modules) == {"cpu", "dram", "soc_top"}
    assert len(design.modules["soc_top"].instances) == 2
    inst_names = {i.name for i in design.modules["soc_top"].instances}
    assert inst_names == {"u_cpu", "u_dram"}


def test_sv_parser_picks_up_port_directions() -> None:
    fixture = FIXTURES / "single_pair"
    design = parse_files(
        [fixture / "cpu.sv", fixture / "dram.sv", fixture / "soc_top.sv"],
        top="soc_top",
    )
    cpu = design.modules["cpu"]
    arvalid = next(p for p in cpu.ports if p.name == "m_axi_arvalid")
    assert arvalid.direction == "output"
    arready = next(p for p in cpu.ports if p.name == "m_axi_arready")
    assert arready.direction == "input"
    araddr = next(p for p in cpu.ports if p.name == "m_axi_araddr")
    assert araddr.width == 32


def test_instance_paths_walk_design() -> None:
    fixture = FIXTURES / "single_pair"
    design = parse_files(
        [fixture / "cpu.sv", fixture / "dram.sv", fixture / "soc_top.sv"],
        top="soc_top",
    )
    assert design.instance_paths_of("cpu") == ["soc_top.u_cpu"]
    assert design.instance_paths_of("dram") == ["soc_top.u_dram"]


def test_discover_single_pair_detects_one_paired_bundle(tmp_path: Path) -> None:
    fixture = FIXTURES / "single_pair"
    manifest = VeribleDiscover().run(filelist=fixture / "filelist.f", top="soc_top")
    assert len(manifest.bundles) == 1
    bundle = manifest.bundles[0]
    assert bundle.master_path == "soc_top.u_cpu"
    assert bundle.slave_path == "soc_top.u_dram"
    assert bundle.protocol.value == "AXI4"
    assert bundle.data_width == 64
    assert bundle.id_width == 4
    # Signals should be canonical-role keyed, paths fully qualified.
    assert bundle.signals["arvalid"] == "soc_top.u_cpu.m_axi_arvalid"
    assert bundle.signals["arready"] == "soc_top.u_cpu.m_axi_arready"


def test_discover_writes_schema_valid_yaml(tmp_path: Path) -> None:
    fixture = FIXTURES / "single_pair"
    out_yaml = tmp_path / "axi-bundles.yaml"
    discover_to_yaml(filelist=fixture / "filelist.f", top="soc_top", output=out_yaml)
    payload = yaml.safe_load(out_yaml.read_text())
    Draft202012Validator(_load_axi_bundles_schema()).validate(payload)
    assert payload["schema_version"] == "1.0"
    assert payload["design_top"] == "soc_top"
    assert len(payload["bundles"]) == 1


def test_discover_populates_clock_signal_from_master_port(tmp_path: Path) -> None:
    """The single_pair fixture's cpu has a ``clk`` input; discovery
    should resolve it to the master's clock_signal path."""
    fixture = FIXTURES / "single_pair"
    manifest = VeribleDiscover().run(filelist=fixture / "filelist.f", top="soc_top")
    bundle = manifest.bundles[0]
    assert bundle.clock_signal == "soc_top.u_cpu.clk"


def test_unpaired_master_marks_needs_user_input(tmp_path: Path) -> None:
    """A module that has master AXI ports but no peer slave under the
    same parent should still emit a bundle, but flag ``slave_path``
    as a user-fill field."""
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "lone_master.sv").write_text(
        """
        module lone (
            output wire [31:0] m_axi_araddr,
            output wire        m_axi_arvalid,
            input  wire        m_axi_arready,
            input  wire [31:0] m_axi_rdata,
            input  wire        m_axi_rvalid,
            output wire        m_axi_rready,
            output wire [31:0] m_axi_awaddr,
            output wire        m_axi_awvalid,
            input  wire        m_axi_awready,
            output wire [31:0] m_axi_wdata,
            output wire        m_axi_wvalid,
            input  wire        m_axi_wready,
            input  wire        m_axi_bvalid,
            output wire        m_axi_bready
        );
        endmodule

        module top;
          lone u_lone (
            .m_axi_araddr(), .m_axi_arvalid(), .m_axi_arready(),
            .m_axi_rdata(), .m_axi_rvalid(), .m_axi_rready(),
            .m_axi_awaddr(), .m_axi_awvalid(), .m_axi_awready(),
            .m_axi_wdata(), .m_axi_wvalid(), .m_axi_wready(),
            .m_axi_bvalid(), .m_axi_bready()
          );
        endmodule
        """
    )
    filelist = tmp_path / "filelist.f"
    filelist.write_text("rtl/lone_master.sv\n")
    out_yaml = tmp_path / "axi-bundles.yaml"
    discover_to_yaml(filelist=filelist, top="top", output=out_yaml)
    payload = yaml.safe_load(out_yaml.read_text())
    Draft202012Validator(_load_axi_bundles_schema()).validate(payload)
    assert len(payload["bundles"]) == 1
    bundle = payload["bundles"][0]
    assert bundle["slave_path"] == "?"
    assert "slave_path" in bundle.get("needs_user_input", [])
