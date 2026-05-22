"""Verible-driven discover stage.

v1: regex-based port-prefix detection over a simple SV text parser.
The architectural plan in #1 calls for full Verible CST walking; the
CST-based builder is tracked as a follow-up to issue #2 along with
the interface-modport detector, hierarchy resolver, and amend pass.
"""

from __future__ import annotations

from pathlib import Path

from rtl_buddy_axi_profiler.types import Manifest

from rtl_buddy_axi_profiler.stages.discover._emit import emit_manifest, now_iso
from rtl_buddy_axi_profiler.stages.discover._regex_detector import (
    detect as detect_regex,
)
from rtl_buddy_axi_profiler.stages.discover._sv_parser import parse_files


class VeribleDiscover:
    """Stage 1 implementation. Implements the ``Discover`` Protocol."""

    name = "verible"

    def run(self, filelist: Path, top: str) -> Manifest:
        files = _read_filelist(filelist)
        design = parse_files(files, top=top)
        bundles = tuple(detect_regex(design))
        return Manifest(
            schema_version="1.0",
            design_top=top,
            bundles=bundles,
            generated_by=_tool_string(),
            generated_at=now_iso(),
        )


def discover_to_yaml(filelist: Path, top: str, output: Path) -> Manifest:
    """Convenience entry point used by the CLI."""
    manifest = VeribleDiscover().run(filelist, top)
    emit_manifest(manifest, output)
    return manifest


def _read_filelist(filelist: Path) -> list[Path]:
    """Parse a simple SV filelist.

    One filename per line; ``//`` and ``#`` lines are comments;
    blank lines ignored. Filenames are resolved relative to the
    filelist's parent directory.
    """
    base = filelist.parent
    out: list[Path] = []
    for raw in filelist.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        # Strip trailing comment on the same line.
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx].strip()
                break
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = (base / p).resolve()
        out.append(p)
    return out


def _tool_string() -> str:
    from importlib.metadata import version as _v

    try:
        return f"rtl-buddy-axi-profiler {_v('rtl-buddy-axi-profiler')}"
    except Exception:
        return "rtl-buddy-axi-profiler (unknown version)"
