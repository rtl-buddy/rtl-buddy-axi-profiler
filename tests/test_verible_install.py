"""Unit tests for the Verible installer + wrapper.

Network-free: nothing here downloads from upstream. The install
itself is exercised in CI by ``scripts/fetch_verible.py`` invoked
from the test workflow.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from rtl_buddy_axi_profiler import _verible_install as vi


def test_pinned_version_format() -> None:
    """The pinned version follows the upstream tag scheme."""
    assert vi.VERIBLE_PINNED_VERSION.startswith("v0.0-")
    assert "-g" in vi.VERIBLE_PINNED_VERSION


def test_checksum_table_has_supported_platforms() -> None:
    """The CHECKSUMS table covers the three platforms we care about."""
    expected = {("Darwin", "*"), ("Linux", "x86_64"), ("Linux", "aarch64")}
    assert set(vi.CHECKSUMS) == expected


def test_macos_and_linux_x86_have_pinned_checksums() -> None:
    """Supported platforms must not fall through the verify-skipped branch."""
    assert vi.CHECKSUMS[("Darwin", "*")].sha256 is not None
    assert vi.CHECKSUMS[("Linux", "x86_64")].sha256 is not None


def test_arm64_linux_checksum_is_intentionally_pending() -> None:
    """ARM64 Linux is the one platform with no pinned checksum."""
    assert vi.CHECKSUMS[("Linux", "aarch64")].sha256 is None


def test_asset_filename_uses_pinned_version() -> None:
    """The default filename embeds the pinned version."""
    filename = vi.asset_filename()
    assert vi.VERIBLE_PINNED_VERSION in filename
    assert filename.endswith(".tar.gz")


def test_download_url_points_at_chipsalliance() -> None:
    url = vi.download_url()
    assert url.startswith("https://github.com/chipsalliance/verible/releases/download/")
    assert vi.VERIBLE_PINNED_VERSION in url


def test_default_vendor_root_is_repo_local() -> None:
    """Default install path is under the repo, not a system dir."""
    root = vi.default_vendor_root()
    assert root.name == "verible"
    assert root.parent.name == "vendor"


def test_resolve_asset_known_platform_does_not_raise() -> None:
    """Whatever platform we're on, _resolve_asset should succeed
    (or this CI environment is unsupported and the test should mark
    that explicitly)."""
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin" or (system == "Linux" and machine in ("x86_64", "aarch64")):
        asset = vi._resolve_asset()
        assert asset.asset_suffix.endswith(".tar.gz")
    else:
        with pytest.raises(vi.VeribleInstallError):
            vi._resolve_asset()


def test_find_binary_returns_none_when_no_path_and_no_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_binary should gracefully return None — never crash."""
    monkeypatch.setenv("PATH", str(tmp_path))
    result = vi.find_binary(vendor_dir=tmp_path / "nope")
    assert result is None


def test_find_binary_prefers_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary on PATH wins over a vendored install."""
    fake_bin = tmp_path / "verible-verilog-syntax"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    found = vi.find_binary("verible-verilog-syntax")
    assert found == fake_bin


def test_find_binary_uses_vendor_when_path_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PATH lookup fails, the vendored install is the fallback."""
    monkeypatch.setenv("PATH", "")
    try:
        asset = vi._resolve_asset()
    except vi.VeribleInstallError:
        pytest.skip("Unsupported platform; no vendored layout to test against.")
        return  # for type-narrowing; pytest.skip raises but Pyright can't tell
    version = vi.VERIBLE_PINNED_VERSION
    inner = asset.inner_dir_template.format(version=version)
    bin_dir = tmp_path / version / inner / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "verible-verilog-syntax"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    found = vi.find_binary(vendor_dir=tmp_path)
    assert found == fake
