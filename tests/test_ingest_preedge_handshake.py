"""Regression for issue #56: single-cycle, combinationally-readied handshakes.

A master that asserts ``xVALID`` for a single cycle while the slave's
``xREADY`` is already high — and drops ``xREADY`` *on* the accepting
posedge as the transfer is consumed — is a perfectly normal AXI transfer.
The per-posedge sampler must count it.

Before the fix the sampler read ``value_at_time(posedge)``, i.e. the
*post*-edge value, so it saw ``valid && !ready`` and recorded 0 handshakes
/ 100% backpressure. The fix samples the pre-edge (setup) value the flop
latches. This test pins that: with the bug it would assert-fail with
``handshakes == 0`` / ``bp == 100``.
"""

from __future__ import annotations

from rtl_buddy_axi_profiler.stages.ingest.wellen import WellenIngest
from tests._vcd_helpers import VcdWriter
from tests.fixtures.e2e._build import (
    BundleSpec,
    declare_bundle_signals,
    emit_clock,
    initialize_bundle_zero,
    manifest_from,
)

# Clock posedges land at t = 5, 15, 25, ... (half_period 5ns); negedges at
# 0, 10, 20, ... See emit_clock.
def _single_cycle_handshake_vcd(tmp_path) -> tuple[str, object]:
    spec = BundleSpec(
        name="m",
        master_path="top.u_m",
        slave_path="top.u_s",
        clock_signal="top.clk",
    )
    manifest = manifest_from([spec], design_top="top")

    w = VcdWriter(timescale="1ns")
    emit_clock(w, path="top.clk", posedges=6)
    declare_bundle_signals(w, spec)
    initialize_bundle_zero(w, spec)

    pre = f"{spec.master_path}.{spec.signal_prefix}"

    def drive(role: str, t: int, v: int) -> None:
        w.change(t, f"{pre}{role}", v)

    # AW: ready high before the edge, valid pulses for exactly one cycle
    # [10,20); ready drops *on* the accepting posedge (t=15).
    drive("awready", 2, 1)
    drive("awvalid", 10, 1)
    drive("awready", 15, 0)   # consumed at the posedge -> post-edge reads 0
    drive("awvalid", 20, 0)

    # AR: same shape one cycle later (accept at posedge t=25).
    drive("arready", 2, 1)
    drive("arvalid", 20, 1)
    drive("arready", 25, 0)
    drive("arvalid", 30, 0)

    # B: same shape (accept at posedge t=35).
    drive("bvalid", 30, 1)
    drive("bready", 2, 1)
    drive("bvalid", 35, 0)
    drive("bready", 35, 0)

    vcd_path = tmp_path / "dump.vcd"
    vcd_path.write_text(w.render())
    return str(vcd_path), manifest


def test_single_cycle_handshake_counted(tmp_path):
    vcd, manifest = _single_cycle_handshake_vcd(tmp_path)

    ingest = WellenIngest()
    list(ingest.run(vcd, manifest))  # drain to populate channel_cycle_stats

    acc = ingest.channel_cycle_stats["m"]
    for ch in ("aw", "ar", "b"):
        stats = acc.get(ch, {"handshakes": 0, "stall": 0, "active": 0})
        assert stats["handshakes"] == 1, (
            f"{ch}: single-cycle handshake not counted "
            f"(handshakes={stats['handshakes']}, stall={stats['stall']}); "
            f"sampler is reading the post-edge value (issue #56)"
        )
        assert stats["stall"] == 0, f"{ch}: spurious backpressure {stats}"
