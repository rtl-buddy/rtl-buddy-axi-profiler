# rtl-buddy-axi-profiler

AXI / APB / AHB interconnect performance profiler. Consumes a simulation waveform (FST/VCD) or a packed-binary stream from a generated SV monitor, reconstructs per-bundle transactions, and emits an `axi-perf.json` that the [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view) `axi-perf` overlay renders onto the hierarchy view.

## Status

Bootstrapping. See [issue #1](https://github.com/rtl-buddy/rtl-buddy-axi-profiler/issues/1) for the scaffold-and-schemas baseline, [#2](https://github.com/rtl-buddy/rtl-buddy-axi-profiler/issues/2)–[#4](https://github.com/rtl-buddy/rtl-buddy-axi-profiler/issues/4) for the pipeline stages.

## Sibling repos

- [`rtl-buddy`](https://github.com/rtl-buddy/rtl_buddy) — wraps this tool as `rb axi-profile`.
- [`rtl-buddy-view`](https://github.com/rtl-buddy/rtl-buddy-view) — consumes the produced `axi-perf.json` via its `axi-perf` overlay (Phase 11).
- [`rtl-buddy-cdc`](https://github.com/rtl-buddy/rtl-buddy-cdc) — pair-repo precedent (CDC linter; same repo layout / release playbook).

## License

BSD 3-Clause. See [`LICENSE`](LICENSE).
