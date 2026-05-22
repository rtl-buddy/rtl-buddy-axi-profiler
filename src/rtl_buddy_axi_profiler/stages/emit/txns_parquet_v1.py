"""Per-transaction parquet emit stage (schema v1.1).

Writes one row per reconstructed AXI transaction to a parquet file.
Sibling artifact to ``axi-perf.json``; consumed by the marimo
notebook drill-down (umbrella #16).

The Emit Protocol in :mod:`stages.protocol` takes ``AggregateStats``
as its input — this stage takes the reconstructed ``Transaction``
stream directly, so it does not conform to that Protocol. The CLI
orchestrates: it materializes the transaction stream once and drives
both ``aggregate`` and this emit off the same list.

**v1.1**: time columns are now picoseconds (``_ps``) instead of
femtoseconds (``_fs``). ps still resolves a single cycle for
multi-GHz sim clocks (1 GHz period = 1000 ps; 5 GHz = 200 ps),
while shrinking absolute values by 1000× — meaningful for snappy
compression and for readable axis labels in downstream notebooks.
The :class:`Transaction` dataclass internally keeps femtosecond
timestamps (FST's native precision); conversion to ps happens at
emit time.

pyarrow is an optional dependency (``[parquet]`` extra). Calling
``emit_txns_parquet`` without it raises :class:`TxnsParquetError`
with a clear install hint; the CLI maps that to exit code 2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rtl_buddy_axi_profiler.types import Bundle, Manifest, Transaction


SCHEMA_VERSION = "1.1"

# 1 ps = 1000 fs. FST timestamps land in fs; we drop the last three
# digits to get ps. Sub-ps precision isn't observable at clock-edge
# sampling, so this rounding is lossless for everything the
# aggregator + notebook consume.
_FS_PER_PS = 1000


def _fs_to_ps(fs: int) -> int:
    return fs // _FS_PER_PS


class TxnsParquetError(RuntimeError):
    """Raised when the parquet emit can't run.

    Currently used for the pyarrow-not-installed case. Distinct from
    a generic ImportError so the CLI can map it to its own exit code.
    """


def emit_txns_parquet(
    txns: Iterable[Transaction],
    manifest: Manifest,
    out: Path,
    *,
    clock_period_ns: float,
    tool_version: str | None = None,
) -> None:
    """Serialize ``txns`` to ``out`` as a v1 axi-txns.parquet.

    ``clock_period_ns`` is needed to derive the per-row cycle-domain
    latency columns (``ar_to_r_first_cyc`` / ``aw_to_b_cyc``). It also
    lands in the file-level metadata so consumers can re-derive other
    cycle measures without re-running ingest.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise TxnsParquetError(
            "pyarrow is required for --emit-txns-parquet. "
            "Install with: pip install 'rtl-buddy-axi-profiler[parquet]'"
        ) from e

    bundles_by_name = _bundles_by_name(manifest.bundles)
    period_fs = clock_period_ns * 1e6

    columns: dict[str, list] = {col: [] for col in _COLUMN_NAMES}
    for t in txns:
        bundle = bundles_by_name.get(t.bundle_name)
        master_path = bundle.master_path if bundle is not None else ""
        slave_path = bundle.slave_path if bundle is not None else ""

        if t.is_read:
            # No R beat ever received → no first-data timestamp.
            t_first_ps = (
                _fs_to_ps(t.t_first_data_fs) if t.t_first_data_fs != 0 else None
            )
            ar_to_r = (
                round((t.t_first_data_fs - t.t_start_fs) / period_fs)
                if t_first_ps is not None and period_fs > 0
                else None
            )
            aw_to_b = None
        else:
            # Write txns don't have an R first-data event.
            t_first_ps = None
            ar_to_r = None
            aw_to_b = (
                round((t.t_end_fs - t.t_start_fs) / period_fs)
                if period_fs > 0
                else None
            )

        columns["bundle_name"].append(t.bundle_name)
        columns["is_read"].append(t.is_read)
        columns["txn_id"].append(t.txn_id)
        columns["addr"].append(t.addr)
        columns["len_beats"].append(t.len_beats)
        columns["size_log2"].append(t.size_log2)
        columns["t_start_ps"].append(_fs_to_ps(t.t_start_fs))
        columns["t_first_data_ps"].append(t_first_ps)
        columns["t_end_ps"].append(_fs_to_ps(t.t_end_fs))
        columns["resp"].append(t.resp)
        columns["ar_to_r_first_cyc"].append(ar_to_r)
        columns["aw_to_b_cyc"].append(aw_to_b)
        columns["master_path"].append(master_path)
        columns["slave_path"].append(slave_path)

    table = pa.Table.from_pydict(columns, schema=_arrow_schema(pa))
    table = table.replace_schema_metadata(
        _file_metadata(
            manifest=manifest,
            clock_period_ns=clock_period_ns,
            tool_version=tool_version,
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, compression="snappy")


_COLUMN_NAMES = (
    "bundle_name",
    "is_read",
    "txn_id",
    "addr",
    "len_beats",
    "size_log2",
    "t_start_ps",
    "t_first_data_ps",
    "t_end_ps",
    "resp",
    "ar_to_r_first_cyc",
    "aw_to_b_cyc",
    "master_path",
    "slave_path",
)


def _arrow_schema(pa):
    return pa.schema(
        [
            pa.field("bundle_name", pa.string(), nullable=False),
            pa.field("is_read", pa.bool_(), nullable=False),
            pa.field("txn_id", pa.int64(), nullable=False),
            pa.field("addr", pa.int64(), nullable=False),
            pa.field("len_beats", pa.int32(), nullable=False),
            pa.field("size_log2", pa.int32(), nullable=False),
            pa.field("t_start_ps", pa.int64(), nullable=False),
            pa.field("t_first_data_ps", pa.int64(), nullable=True),
            pa.field("t_end_ps", pa.int64(), nullable=False),
            pa.field("resp", pa.int8(), nullable=False),
            pa.field("ar_to_r_first_cyc", pa.int64(), nullable=True),
            pa.field("aw_to_b_cyc", pa.int64(), nullable=True),
            pa.field("master_path", pa.string(), nullable=False),
            pa.field("slave_path", pa.string(), nullable=False),
        ]
    )


def _file_metadata(
    *,
    manifest: Manifest,
    clock_period_ns: float,
    tool_version: str | None,
) -> dict[bytes, bytes]:
    return {
        b"schema_version": SCHEMA_VERSION.encode(),
        b"produced_by": f"rtl-buddy-axi-profiler v{tool_version or _read_version()}".encode(),
        b"produced_at": _now_iso().encode(),
        b"design_top": manifest.design_top.encode(),
        b"clock_period_ns": f"{clock_period_ns}".encode(),
    }


def _bundles_by_name(bundles: tuple[Bundle, ...]) -> dict[str, Bundle]:
    out: dict[str, Bundle] = {}
    for b in bundles:
        out[b.name] = b
        for child in b.children:
            out[child.name] = child
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_version() -> str:
    from importlib.metadata import version as _v

    try:
        return _v("rtl-buddy-axi-profiler")
    except Exception:
        return "0.0.0"


class TxnsParquetEmitV1:
    """Wrapper class for stage-registry callers.

    Does not conform to :class:`stages.protocol.Emit` — the signature
    differs because this stage consumes the transaction stream, not
    aggregated stats.
    """

    name = "txns-parquet-v1"

    def run(
        self,
        txns: Iterable[Transaction],
        manifest: Manifest,
        out: Path,
        *,
        clock_period_ns: float,
    ) -> None:
        emit_txns_parquet(txns, manifest, out, clock_period_ns=clock_period_ns)
