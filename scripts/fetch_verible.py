"""Install the pinned Verible release into ``vendor/verible/``.

Run this once after cloning::

    uv run python scripts/fetch_verible.py

After that the verible-* binaries are available under
``vendor/verible/<version>/<platform>/bin/`` and the verible wrapper's
``find_binary`` helper picks them up automatically.

Flags:

- ``--force`` — re-download even if the install marker is present.
- ``--no-verify`` — skip the SHA256 check (useful for an unsupported
  platform where you accept TOFU).
- ``--print-checksum`` — download the platform's tarball and print
  its SHA256 without extracting; use this when rotating the pinned
  version.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from rtl_buddy_axi_profiler._verible_install import (
    VERIBLE_PINNED_VERSION,
    VeribleInstallError,
    _download,
    _resolve_asset,
    _sha256,
    asset_filename,
    download_url,
    install,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="Install location (default: <repo>/vendor/verible).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and re-extract even if already installed.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip SHA256 verification.",
    )
    parser.add_argument(
        "--print-checksum",
        action="store_true",
        help="Download to a temp file, print SHA256, then exit. "
        "Use when rotating the pinned version.",
    )
    args = parser.parse_args(argv)

    if args.print_checksum:
        return _print_checksum()

    try:
        bin_dir = install(
            target_dir=args.target_dir,
            force=args.force,
            verify=not args.no_verify,
        )
    except VeribleInstallError as e:
        print(f"verible install failed: {e}", file=sys.stderr)
        return 1
    print(f"verible installed: {bin_dir}")
    return 0


def _print_checksum() -> int:
    """Download the platform tarball, print SHA256, and exit.

    Used when rotating ``VERIBLE_PINNED_VERSION`` — paste the
    resulting digest into ``CHECKSUMS`` in ``_verible_install.py``.
    """
    try:
        asset = _resolve_asset()
    except VeribleInstallError as e:
        print(f"unsupported platform: {e}", file=sys.stderr)
        return 1
    url = download_url(VERIBLE_PINNED_VERSION)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / asset_filename(VERIBLE_PINNED_VERSION)
        _download(url, dest)
        digest = _sha256(dest)
    print(f"{VERIBLE_PINNED_VERSION} {asset.asset_suffix}: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
