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
