"""Thin subprocess wrapper around ``verible-verilog-syntax``.

Returns the raw JSON CST. CST walking (module extraction, AXI bundle
detection) lives in :mod:`stages.discover.verible`.

The cache layer is shared with ``rtl-buddy-view`` via
``rtl_buddy_view.cst_cache.get_or_compute`` — see axi-profiler #34
and view #109. View owns the cache substrate (content-hash keying,
atomic writes, XDG layout under ``<xdg-cache>/rtl-buddy/sv-cst/``);
we still own binary location (our ``vendor/verible/`` install path
differs from view's).
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

from rtl_buddy_axi_profiler._verible_install import find_binary

# Minimum rtl-buddy-view release whose ``cst_cache.get_or_compute`` API
# we target. Kept in sync with the [verible] extra's floor in
# pyproject.toml. The extra's ``>=`` floor guards pip/uv resolves, but
# git/editable installs bypass it; ``parse_to_json`` repeats the floor at
# import time so a too-old view falls back to the subprocess path instead
# of calling a stale cache API.
_VIEW_MIN_VERSION = "0.2.1"


class VeribleUnavailable(RuntimeError):
    """Raised when the verible-verilog-syntax binary can't be found."""


class VeribleParseError(RuntimeError):
    """Raised when Verible could not produce a CST for a file."""


def _version_tuple(version: str) -> tuple[int, ...]:
    """Leading (major, minor, patch) ints of a PEP 440 version string.

    Enough for a floor comparison; non-numeric suffixes (rc/dev/+local)
    are dropped, so a pre-release of the floor compares equal to it.
    """
    parts = []
    for segment in version.split(".")[:3]:
        match = re.match(r"\d+", segment)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _view_cache_too_old() -> bool:
    """True when an installed rtl-buddy-view predates ``_VIEW_MIN_VERSION``.

    The [verible] extra's ``>=`` floor guards pip/uv resolves, but git
    and editable installs bypass it. Returning ``True`` lets the caller
    fall back to the direct subprocess path rather than driving a stale
    ``cst_cache`` surface. Treated as not-too-old when the version can't
    be read (no distribution metadata) — the import already succeeded, so
    the resolve-time floor and that successful import stand in.
    """
    try:
        installed = importlib.metadata.version("rtl-buddy-view")
    except importlib.metadata.PackageNotFoundError:
        return False
    return _version_tuple(installed) < _version_tuple(_VIEW_MIN_VERSION)


def locate_binary(name: str = "verible-verilog-syntax") -> Path:
    """Return the path to a verible-* binary or raise :class:`VeribleUnavailable`.

    Resolution order: ``PATH`` first (so Homebrew-installed Verible
    or whatever's already on the system wins), then the vendored copy
    under ``vendor/verible/``. Run ``scripts/fetch_verible.py`` to
    install the pinned release.
    """
    found = find_binary(name)
    if found is None:
        raise VeribleUnavailable(
            f"{name} not found on PATH or in vendor/verible/. "
            "Install it with `uv run python scripts/fetch_verible.py` "
            "or via Homebrew (`brew install verible`)."
        )
    return found


def parse_to_json(
    path: Path,
    *,
    binary: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    """Run ``verible-verilog-syntax --export_json --printtree`` on ``path``.

    Returns the parsed JSON tree for the file. Raises
    :class:`VeribleParseError` on syntax errors or empty output.

    Cache hits skip the subprocess entirely — view's
    ``cst_cache.get_or_compute`` keys on ``(verible-version,
    content-sha256)`` so re-parsing the same file across this tool
    and ``rtl-buddy-view`` in the same project is a single Verible
    invocation, not two.

    ``cache_dir`` is forwarded to view; ``None`` defers to view's
    env/XDG default. The caller (rtl_buddy) injects the project's
    configured cache dir per the library-boundary stance — this
    module never reads ``root_config.yaml`` itself.
    """
    bin_path = binary or locate_binary()
    try:
        from rtl_buddy_view.cst_cache import get_or_compute  # type: ignore[import-not-found]
    except ImportError:
        # rtl-buddy-view isn't installed — fall back to direct invocation.
        # Production callers should install the [verible] extra; this
        # path only matters when someone imports _verible without the
        # extra (e.g. unit tests pinned to a slim dep set).
        return _invoke_verible(bin_path, path)
    if _view_cache_too_old():
        # A too-old view (git/editable install below the [verible] floor)
        # may expose a stale ``get_or_compute`` signature. Skip the shared
        # cache and parse directly rather than risk a mid-call
        # AttributeError; upgrade rtl-buddy-view to re-enable the cache.
        return _invoke_verible(bin_path, path)
    return get_or_compute(
        path,
        verible_binary=bin_path,
        compute=_invoke_verible,
        cache_dir=cache_dir,
    )


def _invoke_verible(binary: Path, path: Path) -> dict:
    """Subprocess body used as view's ``get_or_compute`` callback."""
    proc = subprocess.run(
        [str(binary), "--export_json", "--printtree", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise VeribleParseError(
            f"verible-verilog-syntax exited {proc.returncode} for {path}: "
            f"{proc.stderr.strip()}"
        )
    payload = json.loads(proc.stdout)
    file_entry = payload.get(str(path))
    if file_entry is None:
        raise VeribleParseError(
            f"verible-verilog-syntax produced no entry for {path}; "
            f"likely a syntax error (stderr: {proc.stderr.strip()})."
        )
    tree = file_entry.get("tree")
    if tree is None:
        raise VeribleParseError(f"verible-verilog-syntax produced no tree for {path}.")
    return tree
