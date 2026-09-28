# jwst-redux

`jwst-redux` is a thin orchestration layer around MAST / `astroquery.mast` and the official
STScI `jwst` calibration package.

## Status

`jwst-redux` is early-development research software (`0.1.0.dev0`). Current workflows have been
validated for selected NIRISS/SOSS and NIRSpec/BOTS datasets, and interfaces may change. Inspect a
plan before starting a large download or reduction, and validate calibrated products for your own
scientific use.

## Features

- YAML-based target and workflow configuration
- Read-only MAST discovery and reduction planning
- Metadata-based selection of official Detector1, Spec2, and TSO3 pipeline paths
- Resumable downloads and calibration runs with recorded provenance
- Derived QA products for inspecting time-series reductions
- Configurable retention of all products or only raw and final-stage products

## Installation

Python 3.11 or newer is required. The package is not yet distributed on PyPI; install it from
GitHub in an isolated environment:

```bash
git clone https://github.com/brackham/jwst-redux.git
cd jwst-redux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

The `jwst` dependency is a substantial scientific software stack. Calibration also requires CRDS
reference data and may download missing files. Configure `CRDS_PATH` and `CRDS_SERVER_URL` using the
[STScI CRDS guidance](https://jwst-pipeline.readthedocs.io/en/stable/jwst/user_documentation/reference_files_crds.html),
and keep the CRDS cache outside this repository.

## Quick start

The included TOI-3884 configuration provides a representative NIRSpec/BOTS example:

```bash
jwst-redux search configs/toi3884-bots-g395h.yaml
jwst-redux plan configs/toi3884-bots-g395h.yaml
jwst-redux run configs/toi3884-bots-g395h.yaml --all
```

`search` and `plan` are read-only: they do not download data, access CRDS, create workspaces, or run
calibration. `run` can download and process large JWST products, so review its plan and the configured
`output.root` first. Example outputs are written below `work/`, which is ignored by Git.

Pipeline products are retained by default. Set `options.retention: final` to remove only recorded
upstream-stage FITS products after the requested endpoint and all enabled QA complete successfully.
Raw inputs, endpoint products, QA, logs, associations, provenance, and unrelated files are retained.

Extracted-spectrum QA also writes `white_light_quicklook.png`, a morphology-focused view of the
jwst-redux QA-derived white-light curve after conservative isolated-integration rejection and
fixed-time binning. The default cadence is two minutes and can be changed without altering any
pipeline product:

```yaml
qa:
  enabled: true
  quicklook:
    cadence_minutes: 2.0
```

The corresponding native-cadence decisions are retained in `white_light_filtered.ecsv`, including
the original and filtered ppt values and rejection flags (plus spectral order for SOSS). This is a
quick-look QA product only: its local filter is intended to suppress obvious isolated failures, not
to detrend transits, flares, stellar variability, ramps, or instrumental baseline changes.

## Supported modes

| Mode | Current scope |
| --- | --- |
| NIRISS/SOSS | Validated workflows for selected time-series datasets through the applicable official pipeline stages and QA |
| NIRSpec/BOTS | Validated workflows for selected bright-object time-series datasets through the applicable official pipeline stages and QA |

The public example is the TOI-3884 NIRSpec/BOTS G395H configuration. See the
[architecture notes](docs/architecture.md) for more detail about design choices and current scope.

## Development

```bash
git clone https://github.com/brackham/jwst-redux.git
cd jwst-redux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
```

The project deliberately delegates calibration behavior to the official `jwst` pipelines. Changes
to orchestration behavior should include tests and preserve resumability and provenance.

## License

`jwst-redux` is available under the [MIT License](LICENSE).
