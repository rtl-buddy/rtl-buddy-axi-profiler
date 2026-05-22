"""Verible binary installer.

Fetches a pinned ``chipsalliance/verible`` release into
``vendor/verible/`` and exposes the path to the resulting binaries.

Supports macOS (universal) and Linux x86_64 with pinned checksums.
Linux arm64 has an upstream asset but no pinned checksum yet (falls
through to a verify-skipped install with a warning).

:func:`find_binary` prefers a system install on ``PATH`` over the
vendored copy, so a Homebrew-installed Verible "just works" without
running the fetcher.

Stdlib-only by design — the package's only runtime deps are typer
+ pyyaml + jsonschema (see AGENTS.md "Runtime dependencies").

Mirrored from rtl-buddy-view's ``_verible_install.py`` so both repos
ship the same Verible plumbing without an import-time coupling
between sibling tools.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

VERIBLE_PINNED_VERSION = "v0.0-4053-g89d4d98a"
"""Pinned Verible release. Bump in lockstep with the ``CHECKSUMS`` table.

The pinned release date is 2026-03-13; track upstream at
https://github.com/chipsalliance/verible/releases. Bumping requires
re-running ``scripts/fetch_verible.py --print-checksum`` for each
supported platform and updating ``CHECKSUMS`` below.
"""

VERIBLE_RELEASE_URL_BASE = "https://github.com/chipsalliance/verible/releases/download"


@dataclass(frozen=True)
class PlatformAsset:
    """Per-platform release asset metadata.

    ``inner_dir_template`` is the directory name inside the tarball
    after extraction, with ``{version}`` as a placeholder. Upstream
    Verible is inconsistent across platforms: the macOS tarball
    extracts to ``verible-<version>-macOS/`` (with platform suffix),
    while the Linux tarballs extract to just ``verible-<version>/``
    (no suffix). The template lets each platform name its own
    extracted directory rather than assuming a uniform shape.
    """

    asset_suffix: str
    inner_dir_template: str
    sha256: str | None


# Checksums computed by running `shasum -a 256` against the upstream
# tarball. To rotate the pinned version: bump VERIBLE_PINNED_VERSION,
# then run scripts/fetch_verible.py --print-checksum on each
# supported platform and paste the resulting digest here. Verify the
# inner_dir_template matches the tarball layout — upstream has been
# inconsistent across platforms.
CHECKSUMS: dict[tuple[str, str], PlatformAsset] = {
    ("Darwin", "*"): PlatformAsset(
        asset_suffix="macOS.tar.gz",
        inner_dir_template="verible-{version}-macOS",
        sha256="6eb2ed4f443baed841159f3b23ebebd70d2fde789e64f6f3e2baa02ef73a0ddd",
    ),
    ("Linux", "x86_64"): PlatformAsset(
        asset_suffix="linux-static-x86_64.tar.gz",
        inner_dir_template="verible-{version}",
        sha256="1edc1f29c70d74213ed373e727183802d5a733e23f9ab9c74462f5b18b76f2c0",
    ),
    ("Linux", "aarch64"): PlatformAsset(
        asset_suffix="linux-static-arm64.tar.gz",
        inner_dir_template="verible-{version}",
        sha256=None,
    ),
}

VERIBLE_TOOLS: tuple[str, ...] = (
    "verible-verilog-syntax",
    "verible-verilog-project",
    "verible-verilog-format",
    "verible-verilog-lint",
)
"""Tools we know we'll call. find_binary validates one of these
exists in the resolved bin directory before returning success."""


class VeribleInstallError(RuntimeError):
    pass


def _platform_key() -> tuple[str, str]:
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        return (system, "*")
    return (system, machine)


def _resolve_asset() -> PlatformAsset:
    key = _platform_key()
    asset = CHECKSUMS.get(key)
    if asset is None:
        raise VeribleInstallError(
            f"No pinned Verible asset for platform {key[0]}/{key[1]}. "
            f"Supported: {sorted(CHECKSUMS)}. File an issue or extend "
            f"CHECKSUMS in _verible_install.py."
        )
    return asset


def asset_filename(version: str = VERIBLE_PINNED_VERSION) -> str:
    return f"verible-{version}-{_resolve_asset().asset_suffix}"


def download_url(version: str = VERIBLE_PINNED_VERSION) -> str:
    return f"{VERIBLE_RELEASE_URL_BASE}/{version}/{asset_filename(version)}"


def default_vendor_root() -> Path:
    """Default install location: ``<repo-root>/vendor/verible``.

    The repo root is inferred by walking up from this file. Callers
    can pass an explicit ``target_dir`` to :func:`install` to override.
    """
    here = Path(__file__).resolve()
    # src/rtl_buddy_axi_profiler/_verible_install.py → repo root is two up
    # from src/.
    return here.parents[2] / "vendor" / "verible"


def install(
    target_dir: Path | None = None,
    version: str = VERIBLE_PINNED_VERSION,
    *,
    force: bool = False,
    verify: bool = True,
) -> Path:
    """Ensure Verible ``version`` is available; return the ``bin/`` path.

    Behavior:
    - If the install marker is present and ``force=False``, returns
      the cached ``bin/`` path immediately (idempotent).
    - Otherwise downloads the pinned tarball, verifies SHA256 (unless
      ``verify=False`` or the platform has no pinned checksum), and
      extracts under ``target_dir/<version>/``.

    Raises :class:`VeribleInstallError` on checksum mismatch or
    unsupported platform.
    """
    target_dir = (target_dir or default_vendor_root()).resolve()
    asset = _resolve_asset()
    version_dir = target_dir / version
    inner_dir = asset.inner_dir_template.format(version=version)
    bin_dir = version_dir / inner_dir / "bin"
    marker = version_dir / ".installed"

    if marker.exists() and not force:
        if not (bin_dir / VERIBLE_TOOLS[0]).exists():
            raise VeribleInstallError(
                f"Install marker present at {marker} but the expected "
                f"binary is missing. Re-run with force=True."
            )
        return bin_dir

    version_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = version_dir / asset_filename(version)
    url = download_url(version)

    if not tarball_path.exists() or force:
        _download(url, tarball_path)

    if verify and asset.sha256 is not None:
        actual = _sha256(tarball_path)
        if actual != asset.sha256:
            tarball_path.unlink(missing_ok=True)
            raise VeribleInstallError(
                f"SHA256 mismatch for {tarball_path.name}: expected "
                f"{asset.sha256}, got {actual}. Tarball deleted; "
                f"re-run install() after investigating."
            )
    elif asset.sha256 is None:
        print(
            f"WARNING: no pinned checksum for {asset.asset_suffix}; "
            f"skipping verification.",
            file=sys.stderr,
        )

    _extract(tarball_path, version_dir)

    if not (bin_dir / VERIBLE_TOOLS[0]).exists():
        raise VeribleInstallError(
            f"Extracted Verible but {VERIBLE_TOOLS[0]} is missing under "
            f"{bin_dir}. Asset layout may have changed upstream."
        )

    marker.write_text(version + "\n")
    return bin_dir


def find_binary(
    name: str = "verible-verilog-syntax",
    *,
    vendor_dir: Path | None = None,
    version: str = VERIBLE_PINNED_VERSION,
) -> Path | None:
    """Locate a Verible binary. PATH wins; vendored install is fallback.

    Returns ``None`` if neither is available — the caller should
    surface a helpful error pointing at ``scripts/fetch_verible.py``.
    """
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)

    vendor_dir = (vendor_dir or default_vendor_root()).resolve()
    try:
        asset = _resolve_asset()
    except VeribleInstallError:
        return None
    inner_dir = asset.inner_dir_template.format(version=version)
    candidate = vendor_dir / version / inner_dir / "bin" / name
    if candidate.exists():
        return candidate
    return None


# --- internals ---------------------------------------------------------------


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(tarball: Path, dest_dir: Path) -> None:
    # Python 3.12+ honors the ``filter`` argument; explicit ``data``
    # filter rejects absolute paths, symlinks escaping the dest, and
    # device files. Avoids the tarfile CVE family.
    with tarfile.open(tarball, mode="r:gz") as tar:
        tar.extractall(dest_dir, filter="data")
