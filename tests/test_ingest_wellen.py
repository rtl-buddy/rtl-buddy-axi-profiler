"""Tests for the pywellen ingest stage + end-to-end pipeline.

Hand-generates VCD fixtures via :mod:`tests._vcd_helpers` so the
tests stay text-only and reproducible. The generated VCDs are parsed
by pywellen exactly like a Verilator-produced FST would be — the
ingest path is format-agnostic from the wellen layer down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_axi_profiler.stages.aggregate.standard import aggregate
from rtl_buddy_axi_profiler.stages.emit.json_v1 import emit
from rtl_buddy_axi_profiler.stages.ingest._clock_detect import (
    ClockDetectError,
    detect_global_clock,
)
from rtl_buddy_axi_profiler.stages.ingest.wellen import (
    WellenIngest,
    WellenIngestError,
    ingest,
)
from rtl_buddy_axi_profiler.stages.reconstruct.axi4 import reconstruct
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    Channel,
    DefaultView,
    Manifest,
    Protocol,
)

from tests._vcd_helpers import VcdWriter

# Canonical signal paths for a single-bundle fixture. Master = u_cpu;
# slave = u_dram; clock at top.clk; bundle named "cpu_to_dram".
MASTER = "top.u_cpu"
SLAVE = "top.u_dram"


def _make_signals(prefix: str) -> dict[str, str]:
    """Return the 10 required signal paths for a single endpoint."""
    return {
        role: f"{prefix}.m_axi_{role}"
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
    }


def _manifest() -> Manifest:
    bundle = Bundle(
        name="cpu_to_dram",
        master_path=MASTER,
        slave_path=SLAVE,
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
        signals=_make_signals(MASTER),
    )
    return Manifest(
        schema_version="1.0",
        design_top="top",
        bundles=(bundle,),
        generated_by="test",
        generated_at="2026-05-21T08:00:00Z",
    )


def _declare_bundle(w: VcdWriter, prefix: str) -> None:
    """Declare all required AXI signals for one endpoint."""
    one_bit_roles = {
        "arvalid",
        "arready",
        "rvalid",
        "rready",
        "rlast",
        "awvalid",
        "awready",
        "wvalid",
        "wready",
        "wlast",
        "bvalid",
        "bready",
    }
    width_for = {
        "araddr": 32,
        "awaddr": 32,
        "arid": 4,
        "awid": 4,
        "rid": 4,
        "bid": 4,
        "arlen": 8,
        "awlen": 8,
        "arsize": 3,
        "awsize": 3,
        "rresp": 2,
        "bresp": 2,
    }
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
    ):
        path = f"{prefix}.m_axi_{role}"
        width = 1 if role in one_bit_roles else width_for[role]
        w.declare(path, width)


def _initialize_all_zero(w: VcdWriter, prefix: str, t: int = 0) -> None:
    """Drive every AXI signal to 0 at time t so pywellen has a baseline."""
    one_bit_roles = {
        "arvalid",
        "arready",
        "rvalid",
        "rready",
        "rlast",
        "awvalid",
        "awready",
        "wvalid",
        "wready",
        "wlast",
        "bvalid",
        "bready",
    }
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
    ):
        _ = one_bit_roles  # all roles get a 0 value either way
        w.change(t, f"{prefix}.m_axi_{role}", 0)


def _build_minimal_vcd(tmp_path: Path) -> Path:
    """A 100ns trace at 100MHz: clk toggles every 5ns → 10 posedges
    at t=5,15,…,95. One read transaction handshakes on cycle 3 (AR
    at t=35) and completes on cycle 5 (R at t=55)."""
    w = VcdWriter(timescale="1ns")
    w.declare("top.clk", 1)
    _declare_bundle(w, MASTER)

    # 100MHz clock: toggle every 5ns over a 100ns window.
    for i in range(21):
        w.change(i * 5, "top.clk", i % 2)

    _initialize_all_zero(w, MASTER)

    # AR handshake observed on cycle-3 posedge (t=35). Drive both
    # valid+ready high at t=30 so wellen samples them as 1 at t=35.
    w.change(30, f"{MASTER}.m_axi_arvalid", 1)
    w.change(30, f"{MASTER}.m_axi_arready", 1)
    w.change(30, f"{MASTER}.m_axi_araddr", 0x100)
    w.change(30, f"{MASTER}.m_axi_arid", 1)
    w.change(40, f"{MASTER}.m_axi_arvalid", 0)
    w.change(40, f"{MASTER}.m_axi_arready", 0)

    # R handshake on cycle-5 posedge (t=55), one beat with RLAST.
    w.change(50, f"{MASTER}.m_axi_rvalid", 1)
    w.change(50, f"{MASTER}.m_axi_rready", 1)
    w.change(50, f"{MASTER}.m_axi_rid", 1)
    w.change(50, f"{MASTER}.m_axi_rlast", 1)
    w.change(60, f"{MASTER}.m_axi_rvalid", 0)
    w.change(60, f"{MASTER}.m_axi_rready", 0)
    w.change(60, f"{MASTER}.m_axi_rlast", 0)

    path = tmp_path / "trace.vcd"
    path.write_text(w.render())
    return path


# --- clock detector ---------------------------------------------------------


def test_detect_global_clock_finds_clk(tmp_path: Path) -> None:
    vcd = _build_minimal_vcd(tmp_path)
    import pywellen

    waveform = pywellen.Waveform(str(vcd))
    clock = detect_global_clock(waveform)
    assert clock.full_name == "top.clk"
    # Period = 10ns = 10_000_000 fs.
    assert clock.period_fs == 10_000_000
    # 10 posedges across t=5,15,…,95.
    assert len(clock.posedge_times) == 10


def test_detect_global_clock_no_signal(tmp_path: Path) -> None:
    """No toggling 1-bit signal at all → ClockDetectError."""
    w = VcdWriter(timescale="1ns")
    w.declare("top.constant", 1)
    w.change(0, "top.constant", 0)
    vcd = tmp_path / "trace.vcd"
    vcd.write_text(w.render())
    import pywellen

    waveform = pywellen.Waveform(str(vcd))
    with pytest.raises(ClockDetectError):
        detect_global_clock(waveform)


# --- ingest one-shot --------------------------------------------------------


def test_ingest_yields_ar_and_r_handshakes(tmp_path: Path) -> None:
    vcd = _build_minimal_vcd(tmp_path)
    events = list(ingest(vcd, _manifest()))
    # Expected: AR on cycle-3 posedge (t=35ns), R on cycle-5 (t=55ns).
    ar = [e for e in events if e.channel == Channel.AR]
    r = [e for e in events if e.channel == Channel.R]
    assert len(ar) == 1
    assert ar[0].txn_id == 1
    assert ar[0].addr == 0x100
    assert ar[0].t_fs == 35_000_000
    assert len(r) == 1
    assert r[0].txn_id == 1
    assert r[0].last is True
    assert r[0].t_fs == 55_000_000


def test_ingest_missing_signal_raises(tmp_path: Path) -> None:
    vcd = _build_minimal_vcd(tmp_path)
    # Manifest references a signal that wasn't declared.
    bundle = Bundle(
        name="cpu_to_dram",
        master_path=MASTER,
        slave_path=SLAVE,
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
        signals={"arvalid": "top.does_not_exist", **_make_signals(MASTER)},
    )
    manifest = Manifest(schema_version="1.0", design_top="top", bundles=(bundle,))
    bundle.signals["arvalid"] = "top.does_not_exist"
    with pytest.raises(WellenIngestError):
        list(ingest(vcd, manifest))


def test_wellen_ingest_class_exposes_detected_clock(tmp_path: Path) -> None:
    vcd = _build_minimal_vcd(tmp_path)
    stage = WellenIngest()
    events = list(stage.run(vcd, _manifest()))
    assert stage.detected_clock is not None
    assert stage.detected_clock.full_name == "top.clk"
    assert len(events) >= 2  # AR + R at minimum


# --- end-to-end -------------------------------------------------------------


def test_e2e_ingest_reconstruct_aggregate_emit(tmp_path: Path) -> None:
    """Drive the full pipeline on the synthetic VCD and verify the
    emitted axi-perf.json is schema-valid + has the expected txn count."""
    vcd = _build_minimal_vcd(tmp_path)
    manifest = _manifest()
    stage = WellenIngest()
    events = stage.run(vcd, manifest)
    txns = reconstruct(events)
    assert stage.detected_clock is not None
    cycles = len(stage.detected_clock.posedge_times)
    period_ns = stage.detected_clock.period_fs / 1e6
    stats = aggregate(txns, manifest, duration_cycles=cycles, clock_period_ns=period_ns)

    out = tmp_path / "axi-perf.json"
    emit(stats, manifest, out)
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == "1.0"
    assert payload["design_top"] == "top"
    assert len(payload["bundles"]) == 1
    # The read txn should have produced non-zero throughput.
    bundle = payload["bundles"][0]
    assert bundle["throughput"]["read_bps"] > 0
