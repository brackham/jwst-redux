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

### Batch orchestration

Selector-free batch execution first performs the usual read-only discovery and planning, retaining
the `ScienceDataset -> Exposure -> Product -> ReductionGroup` hierarchy. It materializes the existing
selector-scoped write configuration for each planned exposure branch, then runs sequentially to that
branch's resolver-selected endpoint. The batch layer contains no target/program endpoint table: an
F277W branch can complete at Detector1, a single-integration CLEAR branch at Spec2, and a science
time-series branch at TSO3. Existing isolated workspaces/manifests are reused. The default policy
continues after an independent branch failure, preserves successful siblings, returns nonzero at the
end, and lets the next invocation resume incomplete work.

### `workspace.py`
Defines where raw, Stage 1, Stage 2, Stage 3, Stage-3 associations, logs, and manifests live. Each
write workspace is rooted below `output.root` by the complete explicit selector (program, observation,
visit, exposure number, and exposure ID), preventing selected visits from sharing products, manifests,
logs, QA, or associations. It is created only by write commands; search and plan never instantiate it.
CRDS cache files remain external infrastructure.

### `qa/`

Creates derived PNG diagnostics under `stage1/qa/`, `stage2/qa/`, and `stage3/qa/`, in product-specific directories.
This layer reads successful official products but is not itself a JWST pipeline step. Its schema version, inputs,
normalization choices, outputs, upstream run IDs, and status are independent manifest entries. QA can be rebuilt
without changing pipeline provenance or causing calibration to rerun.

For SOSS Stage 2 and Stage 3 extracted spectra, QA includes `spectra.png`, `white_light.png`,
`spectroscopic_time_series.png`, `scatter_spectrum.png`, and `point_to_point_difference.png`. The scatter
diagnostic is the per-channel `1.4826 * MAD_t(flux) / abs(median_t(flux))` in ppt. Point-to-point rows are
later-minus-earlier consecutive integrations, normalized by the temporal median and plotted at the later timestamp;
therefore it has one fewer time row than the input.
`white_light.png` uses four rows: the combined available orders, then separate fixed panels for Orders 1, 2,
and 3. Each order keeps the same color in the combined and its individual panel.

The shared `qa.common.SpectroscopicTimeSeriesConfig` holds the display-only SOSS
spectroscopic-time-series guardrails: finite coverage, relative flux floor, robust scaling percentile, 1–50 ppt
color-range bounds, and the pathological temporal-residual threshold. Its validity mask determines which channels
have a finite, stable enough normalization to display; only invalid samples render neutral gray. The stricter
pathological-variation criterion is preserved as a science-quality classification and annotated without hiding
noisy but mathematically valid channels. Configured per-order wavelength windows determine the displayed spectral
extent independently of either classification. These values are captured in QA provenance and do not alter FITS
products.

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
