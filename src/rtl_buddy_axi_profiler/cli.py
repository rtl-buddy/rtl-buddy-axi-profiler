"""Typer entry point for the `axi-profiler` CLI.

Bootstrap only — stage selection flags are wired but no stage impls
are loaded (those land in issues #2, #3, #4). `run` exits with a
clear error pointing at the open phase issues until they ship.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="axi-profiler",
    help=(
        "AXI / APB / AHB interconnect performance profiler. "
        "Five-stage pipeline: discover → ingest → reconstruct → aggregate → emit. "
        "See https://github.com/rtl-buddy/rtl-buddy-axi-profiler for the schema spec."
    ),
    add_completion=False,
    no_args_is_help=True,
)


@app.command("run")
def run(
    filelist: Path = typer.Option(
        ..., "--filelist", "-f", help="Path to RTL filelist for discovery."
    ),
    top: str = typer.Option(..., "--top", "-t", help="Top module name."),
    input_path: Path = typer.Option(
        ..., "--input", "-i", help="FST/VCD waveform or .axis binary stream."
    ),
    output: Path = typer.Option(
        Path("axi-perf.json"),
        "--output",
        "-o",
        help="Destination for the v1 axi-perf.json artifact.",
    ),
    manifest: Path | None = typer.Option(
        None,
        "--manifest",
        "-m",
        help="Pre-built axi-bundles.yaml; if absent the discover stage runs.",
    ),
    discover: str = typer.Option("verible", "--discover", help="Discover stage."),
    ingest: str = typer.Option(
        "wellen", "--ingest", help="Ingest stage (wellen | stream)."
    ),
    reconstruct_stage: str = typer.Option(
        "axi4", "--reconstruct", help="Reconstruct stage."
    ),
    aggregate_stage: str = typer.Option(
        "standard", "--aggregate", help="Aggregate stage."
    ),
    emit_stage: str = typer.Option("json-v1", "--emit", help="Emit stage."),
    emit_txns_parquet: Path | None = typer.Option(
        None,
        "--emit-txns-parquet",
        help="Also emit a per-transaction parquet file. Path is optional; "
        "default is `axi-txns.parquet` next to --output. Requires the "
        "[parquet] extra (pyarrow). Consumed by the marimo notebook "
        "drill-down (umbrella #16).",
    ),
    tb_prefix: str = typer.Option(
        "",
        "--tb-prefix",
        help="Hierarchical prefix prepended to manifest signal paths "
        "when looking them up in the trace. Typical: 'tb.dut'. Empty = "
        "use manifest paths verbatim. rb axi-profile fills this from "
        "rb.yaml's testbench top.",
    ),
) -> None:
    """Run the full pipeline end-to-end.

    v1 wires discover=verible, ingest=wellen, reconstruct=axi4,
    aggregate=standard, emit=json-v1. Other stage variants land in
    follow-up PRs (#4 stream path).
    """
    from rtl_buddy_axi_profiler.stages.aggregate.standard import aggregate as _aggregate
    from rtl_buddy_axi_profiler.stages.discover.verible import (
        VeribleDiscover,
        discover_to_yaml,
    )
    from rtl_buddy_axi_profiler.stages.emit.json_v1 import emit as _emit
    from rtl_buddy_axi_profiler.stages.ingest.wellen import (
        WellenIngest,
        WellenIngestError,
    )
    from rtl_buddy_axi_profiler.stages.reconstruct.axi4 import (
        reconstruct as _reconstruct,
    )

    if discover != "verible":
        typer.echo(f"unknown discover stage {discover!r}", err=True)
        raise typer.Exit(code=2)
    if ingest != "wellen":
        typer.echo(
            f"ingest={ingest!r} not yet wired in cli.run; use 'wellen' for v1.",
            err=True,
        )
        raise typer.Exit(code=2)
    if reconstruct_stage != "axi4":
        typer.echo(f"unknown reconstruct stage {reconstruct_stage!r}", err=True)
        raise typer.Exit(code=2)
    if aggregate_stage != "standard":
        typer.echo(f"unknown aggregate stage {aggregate_stage!r}", err=True)
        raise typer.Exit(code=2)
    if emit_stage != "json-v1":
        typer.echo(f"unknown emit stage {emit_stage!r}", err=True)
        raise typer.Exit(code=2)

    if manifest is None:
        # Generate manifest on the fly into a temp path next to output.
        manifest_path = output.parent / "axi-bundles.yaml"
        manifest_obj = discover_to_yaml(
            filelist=filelist, top=top, output=manifest_path
        )
        typer.echo(
            f"discover wrote {manifest_path} ({len(manifest_obj.bundles)} bundle(s)).",
            err=True,
        )
    else:
        from rtl_buddy_axi_profiler.stages.discover._sv_parser import parse_files

        _ = parse_files  # imported to keep module hot; unused
        manifest_obj = VeribleDiscover().run(filelist=filelist, top=top)

    from typing import Iterator as _Iterator

    from rtl_buddy_axi_profiler.types import Transaction as _Transaction

    ingest_stage = WellenIngest(tb_prefix=tb_prefix)
    parquet_target: Path | None = None
    txns_list: list[_Transaction] = []
    try:
        events = ingest_stage.run(input_path, manifest_obj)
        txns_iter = _reconstruct(events)
        clock = ingest_stage.detected_clock
        cycles = len(clock.posedge_times) if clock else 0
        period_ns = (clock.period_fs / 1e6) if clock else 1.0

        # Parquet emit needs the per-row transaction stream; aggregate
        # also consumes it. Materialize once so both can read.
        txns_for_aggregate: _Iterator[_Transaction]
        if emit_txns_parquet is not None:
            txns_list = list(txns_iter)
            txns_for_aggregate = iter(txns_list)
            # Bare "--emit-txns-parquet" (no path) lands as Path(".")
            # via typer's flag-without-value behaviour → sibling of --output.
            parquet_target = (
                emit_txns_parquet
                if str(emit_txns_parquet) not in (".", "")
                else output.parent / "axi-txns.parquet"
            )
        else:
            txns_for_aggregate = txns_iter

        stats = _aggregate(
            txns_for_aggregate,
            manifest_obj,
            duration_cycles=cycles,
            clock_period_ns=period_ns,
        )
        _emit(stats, manifest_obj, output)

        if parquet_target is not None:
            from rtl_buddy_axi_profiler.stages.emit.txns_parquet_v1 import (
                TxnsParquetError,
                emit_txns_parquet as _emit_parquet,
            )

            try:
                _emit_parquet(
                    txns_list,
                    manifest_obj,
                    parquet_target,
                    clock_period_ns=period_ns,
                )
            except TxnsParquetError as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(code=2) from None
            typer.echo(
                f"wrote {parquet_target} ({len(txns_list)} txns).", err=True
            )
    except WellenIngestError as e:
        typer.echo(f"ingest failed: {e}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"wrote {output} ({len(manifest_obj.bundles)} bundles, "
        f"{cycles} cycles, clock={clock.full_name if clock else 'none'})."
    )


@app.command("discover")
def discover_cmd(
    filelist: Path = typer.Option(..., "--filelist", "-f"),
    top: str = typer.Option(..., "--top", "-t"),
    output: Path = typer.Option(Path("axi-bundles.yaml"), "--output", "-o"),
    amend: Path | None = typer.Option(
        None, "--amend", help="Existing axi-bundles.yaml to merge user edits from."
    ),
) -> None:
    """Run discovery, emitting axi-bundles.yaml.

    v1: regex-driven port-prefix detection. The interface-modport
    detector, hierarchy resolver, and amend pass are tracked as
    follow-ups to #2.
    """
    if amend is not None:
        typer.echo(
            "--amend is not yet implemented (follow-up to #2). Re-running "
            "without --amend overwrites the output.",
            err=True,
        )

    from rtl_buddy_axi_profiler.stages.discover.verible import discover_to_yaml

    manifest = discover_to_yaml(filelist=filelist, top=top, output=output)
    typer.echo(f"Wrote {output} with {len(manifest.bundles)} bundle(s) for top={top}.")


@app.command("gen-monitor")
def gen_monitor(
    manifest: Path = typer.Argument(..., help="Input axi-bundles.yaml."),
    output: Path = typer.Option(Path("axi_perf_mon.sv"), "--output", "-o"),
    time_precision: str = typer.Option(
        "1ps",
        "--time-precision",
        help="IEEE-1800 timeprecision atom (1ns / 100ps / 1ps / …). "
        "Must match the testbench's `timeprecision.",
    ),
    buffer_cap: int = typer.Option(
        65536,
        "--buffer-cap",
        help="Per-bundle FIFO depth cap. Drained only at $finish.",
    ),
) -> None:
    """Generate the SV monitor for the stream ingest path.

    Emits a ``bind``-style monitor by default — paste the printed
    bind directive into your testbench. Explicit-instantiation
    support is tracked as a follow-up to #4.
    """
    from rtl_buddy_axi_profiler.stages.gen_monitor.generator import (
        GenMonitorError,
        write_monitor,
    )

    try:
        write_monitor(
            manifest,
            output,
            time_precision=time_precision,
            buffer_cap=buffer_cap,
        )
    except GenMonitorError as e:
        typer.echo(f"gen-monitor: {e}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(
        f"wrote {output}. Bind from your testbench:\n"
        f"  bind <design_top> axi_perf_mon u_axi_perf_mon "
        f"(.clk(<clk>), .rst_n(<rst_n>));"
    )


@app.command("version")
def version() -> None:
    """Print the installed package version."""
    from importlib.metadata import version as _v

    typer.echo(_v("rtl-buddy-axi-profiler"))
