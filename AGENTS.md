# AGENTS.md — rtl-buddy-axi-profiler

## Role

This repo is the source-of-truth implementation of the
`rtl-buddy-axi-profiler` tool — a Python-based AXI / APB / AHB
interconnect performance profiler that consumes a simulation waveform
(FST/VCD) or a packed-binary stream from a generated SV monitor,
reconstructs per-bundle transactions, and emits an `axi-perf.json`
artifact.

It is consumed by `rtl_buddy` (sibling repo) as a subprocess via
`rb axi-profile`, and by `rtl-buddy-view`'s `axi-perf` overlay
(Phase 11) for rendering. Anything that breaks the
`axi-perf.json` schema, the `axi-bundles.yaml` schema, the
`axi-stream` binary format, or the CLI surface is a downstream-
breaking change — see [§ Cross-repo coupling](#cross-repo-coupling).

## Read first

- [`docs/pipeline.md`](docs/pipeline.md) — five-stage pipeline
  architecture (`discover → ingest → reconstruct → aggregate → emit`),
  the Protocol contracts each stage implements, and how stage
  variants are registered via entry points.
- [`docs/axi-perf-schema.md`](docs/axi-perf-schema.md) — v1 spec for
  the consumer-facing JSON output.
- [`docs/axi-bundles-schema.md`](docs/axi-bundles-schema.md) — v1 spec
  for the manifest format (bundle definitions).
- [`docs/axi-stream-format.md`](docs/axi-stream-format.md) — v1 spec
  for the packed-binary stream emitted by the generated SV monitor.
- [`README.md`](README.md) — user-facing intro and command reference.

If your task touches the wire contracts (axi-perf JSON,
axi-bundles YAML, axi-stream binary), the doc above is the
authority — update it in the same PR if you change the contract.
Schema files in `src/rtl_buddy_axi_profiler/schema/` are the formal
JSON Schemas the producer validates against before emit.

## Key files

```text
src/rtl_buddy_axi_profiler/
├── __init__.py              # exposes main()
├── cli.py                   # `axi-profiler` Typer entry point
├── types.py                 # HandshakeEvent, Transaction, Manifest,
│                              AggregateStats dataclasses
├── stages/
│   ├── __init__.py
│   ├── protocol.py          # all five stage Protocols
│   ├── discover/            # Stage 1: SV → axi-bundles.yaml
│   ├── ingest/              # Stage 2: FST/VCD/stream → HandshakeEvent
│   ├── reconstruct/         # Stage 3: HandshakeEvent → Transaction
│   ├── aggregate/           # Stage 4: Transaction → AggregateStats
│   └── emit/                # Stage 5: AggregateStats → axi-perf.json
└── schema/
    ├── axi_perf_v1.json     # JSON Schema for the JSON output
    ├── axi_bundles_v1.json  # JSON Schema for the manifest
    └── axi_stream_v1.md     # binary format spec (parser ref)
tests/
├── test_schema_validation.py    # round-trip example payloads
└── fixtures/                    # populated by #2, #3, #4
docs/
├── pipeline.md
├── axi-perf-schema.md
├── axi-bundles-schema.md
└── axi-stream-format.md
.github/workflows/
├── lint.yml             # ruff check + ruff format --check + mypy
└── test.yml             # pytest matrix (3.12, 3.13)
```

## Development rules

- Keep changes targeted. The repo is small; resist sprawling
  refactors unless the task requires them.
- Treat the three wire contracts as **public**: `axi-perf.json`,
  `axi-bundles.yaml`, and the `axi-stream` binary. Downstream
  `rtl-buddy-view` and the generated SV monitor are written against
  these — breaking any of them ripples into the ecosystem.
- The pipeline is a chain of **pure functions** with side effects
  isolated to `cli.py` (file I/O) and the emit stage (writing the
  output). Don't sneak I/O into `reconstruct.py` / `aggregate.py`.
- Stage variants register via entry points
  (`rtl_buddy_axi_profiler.stages`). Hardcoding a variant in `cli.py`
  defeats the modular-pipeline design — see issue #1.
- Frozen dataclasses by default. Mutability is for parser-built
  collections only (the per-bundle accumulators in
  `stages/aggregate/`).
- `__init__.py` stays minimal. Public modules are imported directly
  (`from rtl_buddy_axi_profiler import types, stages, ...`); don't
  re-export symbols at the top level.

### Runtime dependencies

- Default install ships **typer + pyyaml + jsonschema** only. Heavy
  deps gate behind optional extras:
  - `[fst]`: pyfst / pylibfst for FST reading (issue #3).
  - `[verible]`: Verible binding for SV parsing (issue #2).
- New top-level deps need a clear justification in the PR.

## Validation commands

```bash
# from repo root
uv sync                        # set up env (Python 3.13; see .python-version)
uv run ruff check              # lint (must pass)
uv run ruff format --check     # format check (CI enforces this)
uv run mypy                    # type check (must pass; src/ scope only)
uv run pytest -q               # full unit suite

# CLI smoke
uv run axi-profiler --help
```

CI runs ruff + mypy (`lint.yml`) and pytest (`test.yml`, matrix
3.12 / 3.13) on every PR. Run them locally before pushing.

## Cross-repo coupling

This repo's outputs feed three downstream consumers:

1. **`rtl-buddy-view` Phase 11 (`axi-perf` overlay)** — reads
   `axi-perf.json`. Schema breaks here break the overlay; coordinate
   via the schema's `schema_version` field, additive evolution only
   post-v1.
2. **`rtl-buddy` (`rb axi-profile`)** — wraps this tool's CLI. Flag
   renames / removals ripple downstream.
3. **The generated SV monitor (`axi-profiler gen-monitor`, issue
   #4)** — emits the `axi-stream` binary format consumed by
   `StreamIngest`. Producer and consumer must match exactly on
   record layout.

When making changes that affect any of these:

1. Implement and validate here (`uv run ruff check`, `uv run pytest`).
2. Bump `schema_version` if the contract changed.
3. Coordinate the downstream pin bump in a separate PR per the
   workspace release playbook.
