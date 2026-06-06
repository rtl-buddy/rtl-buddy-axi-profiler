"""Unit tests for the Verible subprocess wrapper.

These exercise the error paths without invoking the real binary —
the ``parse_to_json`` end-to-end test runs when a verible binary is
on PATH or in vendor/, otherwise skips.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rtl_buddy_axi_profiler import _verible
from rtl_buddy_axi_profiler._verible_install import find_binary


def test_locate_binary_raises_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear error pointing at scripts/fetch_verible.py beats a stack trace."""
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(_verible, "find_binary", lambda *args, **kwargs: None)
    with pytest.raises(_verible.VeribleUnavailable) as info:
        _verible.locate_binary()
    assert "scripts/fetch_verible.py" in str(info.value)


def test_parse_to_json_real_binary_on_minimal_module(tmp_path: Path) -> None:
    """Smoke test: if a verible binary is reachable, parsing a trivial
    SV module returns a non-empty CST dict."""
    binary = find_binary("verible-verilog-syntax")
    if binary is None and shutil.which("verible-verilog-syntax") is None:
        pytest.skip("verible-verilog-syntax not available")
    sv_file = tmp_path / "minimal.sv"
    sv_file.write_text("module m(input wire clk); endmodule\n")
    tree = _verible.parse_to_json(sv_file)
    assert isinstance(tree, dict)
    # The CST root has children — non-empty is sufficient signal.
    assert "children" in tree or "tag" in tree


def test_parse_to_json_routes_through_view_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``parse_to_json`` must delegate to view's ``get_or_compute`` so
    the subprocess result is cached under the shared XDG namespace.
    Stub the cache function and the subprocess body; assert the cache
    function is the one driving the call."""
    pytest.importorskip("rtl_buddy_view.cst_cache")

    sv_file = tmp_path / "stub.sv"
    sv_file.write_text("module m; endmodule\n")

    sentinel = {"tag": "sentinel-from-view-cache"}
    captured: dict[str, object] = {}

    def fake_get_or_compute(path, *, verible_binary, compute, cache_dir):
        captured["path"] = path
        captured["binary"] = verible_binary
        captured["compute"] = compute
        captured["cache_dir"] = cache_dir
        return sentinel

    from rtl_buddy_view import cst_cache

    monkeypatch.setattr(cst_cache, "get_or_compute", fake_get_or_compute)
    monkeypatch.setattr(
        _verible, "locate_binary", lambda *a, **k: Path("/fake/verible")
    )

    # Default: no cache_dir passed → forwarded as None (view falls back
    # to env / XDG default).
    result = _verible.parse_to_json(sv_file)
    assert result is sentinel
    assert captured["path"] == sv_file
    assert captured["binary"] == Path("/fake/verible")
    # The compute callback must be our subprocess body, so cache misses
    # still invoke verible-verilog-syntax — not silently no-op.
    assert captured["compute"] is _verible._invoke_verible
    assert captured["cache_dir"] is None

    # Explicit override: caller-injected cache_dir must reach view
    # unchanged so rtl_buddy can honour the project's configured path.
    explicit = tmp_path / "shared-cst-cache"
    _verible.parse_to_json(sv_file, cache_dir=explicit)
    assert captured["cache_dir"] == explicit


def test_parse_to_json_falls_back_when_view_too_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A too-old rtl-buddy-view (below the [verible] floor) must bypass
    the shared cache and parse directly, rather than calling a stale
    ``get_or_compute`` surface. The git/editable install path slips past
    the resolve-time ``>=`` floor, so the runtime guard backstops it."""
    pytest.importorskip("rtl_buddy_view.cst_cache")

    sv_file = tmp_path / "stale.sv"
    sv_file.write_text("module m; endmodule\n")

    from rtl_buddy_view import cst_cache

    def exploding_get_or_compute(*args, **kwargs):
        raise AssertionError("cache must not be used when view is too old")

    monkeypatch.setattr(cst_cache, "get_or_compute", exploding_get_or_compute)
    monkeypatch.setattr(
        _verible, "locate_binary", lambda *a, **k: Path("/fake/verible")
    )
    # Pretend an ancient view is installed; the guard must trip.
    monkeypatch.setattr(_verible, "_view_cache_too_old", lambda: True)

    sentinel = {"tag": "direct-subprocess"}
    monkeypatch.setattr(_verible, "_invoke_verible", lambda binary, path: sentinel)

    assert _verible.parse_to_json(sv_file) is sentinel


def test_view_cache_too_old_compares_against_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_view_cache_too_old`` must trip below the floor, clear at/above
    it, and stay clear when the version metadata is unreadable."""
    import importlib.metadata as md

    monkeypatch.setattr(md, "version", lambda name: "0.2.0")
    assert _verible._view_cache_too_old() is True

    monkeypatch.setattr(md, "version", lambda name: "0.2.1")
    assert _verible._view_cache_too_old() is False

    monkeypatch.setattr(md, "version", lambda name: "0.3.0")
    assert _verible._view_cache_too_old() is False

    def _raise(name):
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", _raise)
    assert _verible._view_cache_too_old() is False
