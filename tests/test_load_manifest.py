"""Tests for the YAML manifest loader.

Two layers: a unit round-trip through ``emit_manifest`` →
``load_manifest`` proves the loader inverts the emitter and
preserves nested-child bundles + every field; a CLI smoke proves
that ``axi-profiler run --manifest <yaml>`` does **not** invoke
``VeribleDiscover`` and uses the YAML's bundle list verbatim.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from rtl_buddy_axi_profiler.cli import app
from rtl_buddy_axi_profiler.stages.discover._emit import emit_manifest
from rtl_buddy_axi_profiler.stages.discover._load import (
    ManifestLoadError,
    load_manifest,
)
from rtl_buddy_axi_profiler.types import (
    Bundle,
    BundleSource,
    DefaultView,
    Manifest,
    Protocol,
)


def _sample_manifest() -> Manifest:
    child = Bundle(
        name="xbar_to_dram",
        master_path="soc.u_xbar",
        slave_path="soc.u_dram",
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.VERIBLE_REGEX,
        default_view=DefaultView.PARENT,
        signals={"awvalid": "soc.u_xbar.m_axi_awvalid"},
        clock_signal="soc.clk",
    )
    parent = Bundle(
        name="cpu_to_xbar",
        master_path="soc.u_cpu",
        slave_path="soc.u_xbar",
        protocol=Protocol.AXI4,
        data_width=64,
        id_width=4,
        source=BundleSource.USER,
        default_view=DefaultView.BOTH,
        signals={"arvalid": "soc.u_cpu.m_axi_arvalid"},
        clock_signal="soc.clk",
        children=(child,),
    )
    return Manifest(
        schema_version="1.0",
        design_top="soc",
        bundles=(parent,),
        generated_by="test",
        generated_at="2026-05-22T08:00:00Z",
    )


def test_roundtrip_preserves_all_fields_including_children(tmp_path: Path) -> None:
    out = tmp_path / "axi-bundles.yaml"
    original = _sample_manifest()
    emit_manifest(original, out)

    loaded = load_manifest(out)

    # Top-level fields.
    assert loaded.schema_version == original.schema_version
    assert loaded.design_top == original.design_top
    assert loaded.generated_by == original.generated_by
    assert loaded.generated_at == original.generated_at
    assert len(loaded.bundles) == 1

    # Parent bundle.
    parent = loaded.bundles[0]
    expected_parent = original.bundles[0]
    for attr in (
        "name",
        "master_path",
        "slave_path",
        "protocol",
        "data_width",
        "id_width",
        "source",
        "default_view",
        "clock_signal",
    ):
        assert getattr(parent, attr) == getattr(expected_parent, attr), attr
    assert parent.signals == expected_parent.signals

    # Nested child preserved.
    assert len(parent.children) == 1
    child = parent.children[0]
    expected_child = expected_parent.children[0]
    assert child.name == expected_child.name
    assert child.master_path == expected_child.master_path
    assert child.slave_path == expected_child.slave_path
    assert child.protocol == expected_child.protocol
    assert child.source == expected_child.source


def test_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    out = tmp_path / "axi-bundles.yaml"
    out.write_text("- just a list\n")
    with pytest.raises(ManifestLoadError) as exc:
        load_manifest(out)
    assert "mapping" in str(exc.value)


def test_rejects_schema_invalid_payload(tmp_path: Path) -> None:
    out = tmp_path / "axi-bundles.yaml"
    out.write_text("schema_version: '1.0'\ndesign_top: dut\n")  # missing 'bundles'
    with pytest.raises(ManifestLoadError) as exc:
        load_manifest(out)
    assert "schema validation failed" in str(exc.value)


def test_missing_file_raises_manifest_load_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestLoadError) as exc:
        load_manifest(tmp_path / "does-not-exist.yaml")
    assert "does-not-exist.yaml" in str(exc.value)


def test_cli_run_with_manifest_does_not_invoke_verible_discover(
    tmp_path: Path,
) -> None:
    """The original bug: ``--manifest`` used to silently re-run
    discover. Lock the fix by asserting ``VeribleDiscover`` is never
    constructed when a manifest is supplied."""
    from rtl_buddy_axi_profiler.stages.ingest.wellen import WellenIngestError

    manifest_path = tmp_path / "axi-bundles.yaml"
    emit_manifest(_sample_manifest(), manifest_path)

    runner = CliRunner()

    # Stub WellenIngest at the cli import site so the run stops cleanly
    # at ingest (without that, pywellen's Rust panic on a missing FST
    # eats the captured stderr we want to assert against).
    class _FakeIngest:
        detected_clock = None

        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            raise WellenIngestError("no FST in test env")

    with (
        patch(
            "rtl_buddy_axi_profiler.stages.discover.verible.VeribleDiscover"
        ) as mock_discover,
        patch("rtl_buddy_axi_profiler.stages.ingest.wellen.WellenIngest", _FakeIngest),
    ):
        result = runner.invoke(
            app,
            [
                "run",
                "-f",
                str(tmp_path / "empty.f"),
                "-t",
                "soc",
                "-i",
                str(tmp_path / "missing.fst"),
                "-o",
                str(tmp_path / "perf.json"),
                "-m",
                str(manifest_path),
            ],
        )

    # VeribleDiscover MUST NOT have been instantiated. The class is
    # what got called in the (now-removed) buggy else-branch; if
    # the regression returns it'd show up here.
    mock_discover.assert_not_called()
    # The "loaded <manifest>" line hits stderr right after the YAML
    # load, before the (stubbed) ingest failure — proves the CLI
    # actually took the load_manifest path.
    assert "loaded" in result.stderr
    assert str(manifest_path) in result.stderr


def test_cli_run_with_invalid_manifest_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "axi-bundles.yaml"
    bad.write_text("not: a: valid: manifest")  # mapping but missing required keys

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "-f",
            str(tmp_path / "empty.f"),
            "-t",
            "dut",
            "-i",
            str(tmp_path / "any.fst"),
            "-o",
            str(tmp_path / "perf.json"),
            "-m",
            str(bad),
        ],
    )
    assert result.exit_code == 2
    assert str(bad) in result.stderr
