# axi-bundles.yaml schema v1.0

Manifest format produced by the `discover` stage and consumed by every later stage. Hierarchical; bundles may declare `children[]` representing internal sub-bundles of an interconnect node.

Formal JSON Schema: [`src/rtl_buddy_axi_profiler/schema/axi_bundles_v1.json`](../src/rtl_buddy_axi_profiler/schema/axi_bundles_v1.json).

## Top-level shape

```yaml
schema_version: "1.0"
generated_by: rtl-buddy-axi-profiler 0.1.0
generated_at: "2026-05-21T08:00:00Z"
design_top: soc_top
bundles:
  - name: cpu_to_soc_xbar
    master_path: soc_top.u_cpu
    slave_path:  soc_top.u_soc_xbar
    protocol: AXI4
    data_width: 64
    id_width: 4
    source: verible-interface
    default_view: parent
    signals:
      arvalid: soc_top.u_cpu.m_axi_arvalid
      arready: soc_top.u_cpu.m_axi_arready
      araddr:  soc_top.u_cpu.m_axi_araddr
      # ... all 5 channels, all required signals
    children:
      - name: xbar_to_dram_ctrl
        master_path: soc_top.u_soc_xbar
        slave_path:  soc_top.u_dram_ctrl
        protocol: AXI4
        data_width: 64
        id_width: 4
        source: verible-interface
        signals: { /* ... */ }
```

## Per-bundle fields

| Field           | Type                                | Required | Description                              |
|-----------------|-------------------------------------|----------|------------------------------------------|
| `name`          | string                              | yes      | Bundle identity; unique within manifest  |
| `master_path`   | string                              | yes      | Hierarchical instance path of the master |
| `slave_path`    | string                              | yes      | Hierarchical instance path of the slave  |
| `protocol`      | `AXI4` \| `AXI-Lite` \| `AXI5`      | yes      | v1.0 emits `AXI4` only                   |
| `data_width`    | int (bits)                          | yes      |                                          |
| `id_width`      | int (bits)                          | yes      |                                          |
| `source`        | enum (below)                        | yes      | Confidence tag                           |
| `default_view`  | `parent` \| `children` \| `both`    | no       | Initial-view hint for the consumer       |
| `signals`       | object: role → SV signal path       | yes      | Canonical role names (`arvalid`, ...)    |
| `needs_user_input` | array<string>                    | no       | List of fields the user must amend       |
| `children`      | array<bundle>                       | no       | One level deep in v1                     |

### `source`

| Value                | Meaning                                                            |
|----------------------|--------------------------------------------------------------------|
| `verible-interface`  | Detected from an SV `interface` with AXI-typed modports. High confidence. |
| `verible-regex`      | Detected from module-port name patterns. Lower confidence; verify before relying. |
| `user`               | Added or edited by the user; preserved across `--amend` re-runs.   |

The user-amend workflow:

1. Run `axi-profiler discover --filelist f --top X -o axi-bundles.yaml`.
2. Inspect the file; edit any `verible-regex` entries that need fixes; add new `source: user` entries.
3. Re-run discovery with `--amend axi-bundles.yaml` — `source: user` entries are preserved verbatim; `source: verible-*` entries are regenerated.

### `signals` canonical roles

The role names match the AXI4 spec. Required at minimum:

```
ar: arvalid, arready, araddr, arlen, arsize
aw: awvalid, awready, awaddr, awlen, awsize
r:  rvalid, rready, rdata, rid, rresp, rlast
w:  wvalid, wready, wdata, wlast
b:  bvalid, bready, bid, bresp
```

The producer must emit all required roles. Optional roles (e.g., `arburst`, `arcache`, `arprot`) are included when present but not required.

### `needs_user_input`

When discovery cannot determine a field (typically `data_width` when parametric across hierarchy), the bundle ships with a placeholder and lists the field name here. Re-discovery preserves any user-set value but keeps the entry in `needs_user_input` until the user removes it.

Example:

```yaml
- name: cpu_to_xbar
  data_width: 0          # placeholder; user must set
  needs_user_input:
    - data_width
```

## Hierarchy semantics

Children are *independent measurements*, not roll-ups:

- The parent bundle measures the external interface to an interconnect node.
- Each child bundle measures one internal connection inside that node.
- Parent stats are NOT auto-sums of children — the consumer either renders one or the other depending on `default_view` and user state.

v1.0 supports one level of nesting only. Multi-level hierarchies must be flattened by the producer with a warning.

## Versioning

- v1.x evolution is additive only. New optional fields keep `schema_version: "1.0"`.
- Required-field additions bump to `2.0`.
- Consumers reject mismatched majors with a typed error.
