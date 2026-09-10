# Architecture

## Boundary

`jwst-redux` orchestrates archive retrieval and execution of the official JWST pipeline. It does not
implement an alternative calibration pipeline and should not absorb downstream science analysis.

## Layers

### `mast/`
Discovers observations/products, filters archive results, and downloads required products.

### `planning/`
Converts archive/FITS metadata into an explicit reduction plan. This layer decides which official
pipeline classes are appropriate and how products are grouped for Stage 3.

### `pipeline/`
Runs the selected `jwst` Pipeline classes with standard defaults plus explicit user overrides.

### `workspace.py`
Defines where raw, Stage 1, Stage 2, Stage 3, logs, and manifests live.

### `provenance.py`
Records enough metadata to reproduce, audit, resume, or deliberately rerun a reduction.

## Non-goals for the first release

- Custom 1/f corrections
- Alternative SOSS extraction
- Transit fitting
- Light-curve analysis
- Stellar contamination correction
- Instrument-specific science analysis
