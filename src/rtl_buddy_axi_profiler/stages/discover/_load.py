"""YAML loader for the manifest.

Inverse of :mod:`._emit` — parses a checked-in ``axi-bundles.yaml``
into a :class:`Manifest` so the ingest pipeline can run against a
user-curated bundle list without re-invoking discover.

The YAML is validated against ``schema/axi_bundles_v1.json`` before
the dict-to-dataclass walk, so a malformed manifest fails with a
specific jsonschema error rather than a confusing AttributeError on
a missing field.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

import rtl_buddy_axi_profiler.schema as schema_pkg
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Manifest,
    Protocol,
)


class ManifestLoadError(ValueError):
    """Raised when an axi-bundles.yaml can't be parsed into a Manifest."""


def load_manifest(path: Path) -> Manifest:
    """Parse ``path`` into a :class:`Manifest`.

    Validates against ``axi_bundles_v1.json`` first; a malformed file
    raises :class:`ManifestLoadError` with a path-prefixed message
    that the CLI maps to a clean exit code 2.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise ManifestLoadError(f"{path}: {e}") from e
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestLoadError(f"{path}: YAML parse error: {e}") from e
    if not isinstance(payload, dict):
        raise ManifestLoadError(
            f"{path}: top-level must be a mapping, got {type(payload).__name__}"
        )

    try:
        Draft202012Validator(_load_schema()).validate(payload)
    except Exception as e:
        raise ManifestLoadError(f"{path}: schema validation failed: {e}") from e

    bundles = tuple(_dict_to_bundle(b) for b in payload.get("bundles", []))
    return Manifest(
        schema_version=payload["schema_version"],
        design_top=payload["design_top"],
        bundles=bundles,
        generated_by=payload.get("generated_by", ""),
        generated_at=payload.get("generated_at", ""),
    )


def _dict_to_bundle(payload: dict[str, Any]) -> Bundle:
    children = tuple(_dict_to_bundle(c) for c in payload.get("children", []))
    return Bundle(
        name=payload["name"],
        master_path=payload["master_path"],
        slave_path=payload["slave_path"],
        protocol=Protocol(payload["protocol"]),
        data_width=int(payload["data_width"]),
        id_width=int(payload["id_width"]),
        source=BundleSource(payload["source"]),
        default_view=DefaultView(payload.get("default_view", DefaultView.PARENT.value)),
        signals=dict(payload.get("signals", {})),
        clock_signal=payload.get("clock_signal", ""),
        children=children,
    )


def _load_schema() -> dict[str, Any]:
    text = (resources.files(schema_pkg) / "axi_bundles_v1.json").read_text()
    return json.loads(text)
