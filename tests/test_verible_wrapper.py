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
