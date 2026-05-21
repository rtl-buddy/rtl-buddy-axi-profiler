"""Generated SV monitor (#4).

`axi-profiler gen-monitor axi-bundles.yaml -o axi_perf_mon.sv`
renders a SystemVerilog monitor that taps each AXI bundle's
handshake signals and writes the `axi-stream` v1 binary file
defined in :mod:`rtl_buddy_axi_profiler.schema.axi_stream_v1`.

v1 emits a ``bind``-style monitor by default — zero DUT edits, just
one bind directive at the testbench. Explicit-instantiation fallback
is tracked as a follow-up; until then users with bind-incompatible
simulators can hand-edit the generated module.
"""
