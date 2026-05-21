"""YAML serializer for the manifest.

Stable key ordering + ``source:`` per entry. The YAML schema is
defined in :mod:`rtl_buddy_axi_profiler.schema.axi_bundles_v1`; emit
ensures the output passes that schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from rtl_buddy_axi_profiler.types import Bundle, BundleSource, DefaultView, Manifest


def emit_manifest(manifest: Manifest, output: Path) -> None:
    """Serialize ``manifest`` to ``output`` as YAML."""
    payload = manifest_to_dict(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)


def manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "generated_by": manifest.generated_by,
        "generated_at": manifest.generated_at,
        "design_top": manifest.design_top,
        "bundles": [bundle_to_dict(b) for b in manifest.bundles],
    }


def bundle_to_dict(bundle: Bundle) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": bundle.name,
        "master_path": bundle.master_path,
        "slave_path": bundle.slave_path,
        "protocol": bundle.protocol.value,
        "data_width": bundle.data_width,
        "id_width": bundle.id_width,
        "source": bundle.source.value,
    }
    if bundle.default_view != DefaultView.PARENT:
        out["default_view"] = bundle.default_view.value
    else:
        out["default_view"] = DefaultView.PARENT.value
    if bundle.clock_signal:
        out["clock_signal"] = bundle.clock_signal
    out["signals"] = dict(bundle.signals)
    needs = _needs_user_input(bundle)
    if needs:
        out["needs_user_input"] = needs
    if bundle.children:
        out["children"] = [bundle_to_dict(c) for c in bundle.children]
    return out


def _needs_user_input(bundle: Bundle) -> list[str]:
    """Report fields that require the user to fill in.

    The regex detector emits placeholders for these:
    - slave_path = "?" when the slave couldn't be net-traced
    - data_width = 0 / id_width = 0 when widths are parametric
    """
    needs: list[str] = []
    if bundle.slave_path in ("?", ""):
        needs.append("slave_path")
    if bundle.data_width == 0 and bundle.source != BundleSource.USER:
        needs.append("data_width")
    if bundle.id_width == 0 and bundle.source != BundleSource.USER:
        # id_width=0 is legal for AXI-Lite; only flag for AXI4.
        needs.append("id_width")
    return needs


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
