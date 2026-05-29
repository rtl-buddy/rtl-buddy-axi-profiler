"""Smoke test for the marimo notebook template.

We don't actually run the notebook — that needs a parquet + a
marimo session. Instead we verify the template file is valid
Python that marimo's AST machinery accepts, and that ``marimo
export --to script`` round-trips cleanly. Catches typos / cell-
decorator misuse before they trip the user mid-debug.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


pytest.importorskip("marimo")

TEMPLATE = Path(
    __import__(
        "rtl_buddy_axi_profiler.notebook.template", fromlist=["__file__"]
    ).__file__
)


def test_template_is_valid_python() -> None:
    """If the cell decorators or `app.cell` signatures drift the
    file won't even compile — fail fast."""
    compile(TEMPLATE.read_text(), str(TEMPLATE), "exec")


def test_template_uses_brush_chart_for_time_window_publish() -> None:
    """Brush-state publisher contract: the timeline cell wraps its
    chart in ``mo.ui.altair_chart`` (so the brush is reactive) and a
    downstream cell calls ``sync.publish_time_window`` from the
    chart's selected rows. Guards against accidental revert to the
    manual ``mo.ui.number + button`` form that this replaced."""
    body = TEMPLATE.read_text()
    assert "mo.ui.altair_chart" in body, "timeline must be brush-wrapped"
    assert "sync.publish_time_window" in body, "brush must drive the publisher"
    assert "publish window to SPA" not in body, "manual time-window form should be gone"


def test_template_surfaces_spa_divergence() -> None:
    """When the SPA selects a bundle this parquet doesn't carry, the
    notebook must render a divergence callout — not silently fall
    back. We string-check for the wired-in callout to guard against
    accidental removal."""
    body = TEMPLATE.read_text()
    assert "no matching bundle in" in body, "divergence callout text missing"
    assert "resolve_spa_bundle(spa_selection) is None" in body, (
        "divergence detector should fire when the SPA instance_path "
        "resolves to no bundle in this parquet"
    )


def test_template_exports_via_marimo_cli() -> None:
    """`marimo export --to script` is the bare-minimum sanity check:
    parses the file, walks the cells, emits a flat .py. If a cell
    declares an undefined symbol or the @app.cell decoration goes
    sideways this command surfaces it."""
    result = subprocess.run(
        [sys.executable, "-m", "marimo", "export", "script", str(TEMPLATE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"marimo export failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Sanity check: a few canonical symbols from the template should
    # appear in the exported script.
    assert "AXI_TXNS_PARQUET" in result.stdout
    assert "timeline" in result.stdout
