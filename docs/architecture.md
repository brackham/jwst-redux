# Architecture

## Boundary

`jwst-redux` orchestrates archive retrieval and execution of the official JWST pipeline. It does not
implement an alternative calibration pipeline and should not absorb downstream science analysis.

## Layers

### `mast/`
Discovers exposure-level records/products, filters archive results, and assembles scientific datasets.

### `planning/`
Converts archive/FITS metadata into an explicit reduction plan. This layer decides which official
pipeline classes are appropriate, how products form compatible reduction groups, and when a Stage 3
association is required.

The domain hierarchy is:

```text
Scientific dataset / visit
    -> exposure-level MAST record
        -> segment/product

Scientific dataset
    -> one or more compatible ReductionGroups
```

Scientific dataset identity is observing-mode dependent. For NIRISS/SOSS, it is derived from the
JWST program, observation, and visit-number metadata. Future modes must define their own explicit
identity rule; `visit_id` is not a universal grouping key. Within a dataset, reduction compatibility
is determined from the relevant observation, target, mode, TSO, optical-path, and pipeline-path
metadata. When association creation is implemented, use an appropriate official archived association
or the official `jwst.associations` machinery rather than defining a custom association format.

### `pipeline/`
Runs the selected `jwst` Pipeline classes with standard defaults plus explicit user overrides. The
implemented write path selects one explicit scientific dataset/exposure, validates its complete
starting-product segment set, and runs its products sequentially through `Detector1Pipeline` and
`Spec2Pipeline`. Resume is per segment. Stage 2 input is resolved from the intact output record of a
successful Stage 1 manifest entry, preserving the provenance link rather than reconstructing a
filename. For this TSO/SOSS mode, `_calints` is the TSO3 association input while `_x1dints` is a
per-exposure extracted product. Stage 3 resolves one intact `_calints` record per expected selected
segment, constructs a schema-validated official Level-3 association with `jwst.associations`, and
runs `Tso3Pipeline` once. Its resume key includes association content, all member paths, and upstream
Stage-2 run IDs; actual TSO3 outputs are captured rather than assumed.

### `workspace.py`
Defines where raw, Stage 1, Stage 2, Stage 3, Stage-3 associations, logs, and manifests live. It is
created only by write commands; search and plan never instantiate it. CRDS cache files remain external
infrastructure.

### `provenance.py`
Records software, archive identity, CRDS context, inputs, outputs, timing, and status in an atomic JSON
manifest. Pipeline entries also capture the resolved parameter configuration and warning/error
summary from the complete log. Resume requires a matching successful entry and intact size-checked
outputs; downstream stages explicitly reference the successful upstream run.

## Non-goals for the first release

- Custom 1/f corrections
- Alternative SOSS extraction
- Transit fitting
- Light-curve analysis
- Stellar contamination correction
- Instrument-specific science analysis
