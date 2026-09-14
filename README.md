# jwst-redux

`jwst-redux` is a thin orchestration layer around MAST / `astroquery.mast` and the official
STScI `jwst` calibration package. It discovers archival observations, plans the appropriate
pipeline path from metadata, retrieves inputs, runs the official pipelines, records provenance,
and produces inspection QA.

It does not reimplement JWST calibration. Its purpose is to make the workflow

> discover → plan → download → calibrate → record provenance → inspect QA

reproducible, resumable, and easier to apply to JWST time-series observations.

## Status

`jwst-redux` is early-development research software (`0.1.0.dev0`). Current workflows have been
validated for selected NIRISS/SOSS and NIRSpec/BOTS datasets, and interfaces may change. Inspect a
plan before starting a large download or reduction, and validate calibrated products for your own
scientific use.

## Features

- YAML-based target and workflow configuration
- Read-only MAST discovery and reduction planning
- Metadata-aware selection of official Detector1, Spec2, and TSO3 pipeline paths
- Resumable downloads and calibration runs with recorded provenance
- Derived QA products for inspecting time-series reductions
- Isolated, Git-ignored workspaces for downloaded and generated science data

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

The included TOI-3884 configurations provide representative NIRISS/SOSS and NIRSpec/BOTS examples:

```bash
jwst-redux search configs/toi3884.yaml
jwst-redux plan configs/toi3884.yaml
jwst-redux run configs/toi3884-all-soss.yaml --all
jwst-redux qa configs/toi3884.yaml
```

`search` and `plan` are read-only: they do not download data, access CRDS, create workspaces, or run
calibration. `run` can download and process large JWST products, so review its plan and the configured
`output.root` first. Example outputs are written below `work/`, which is ignored by Git.

## Supported modes

| Mode | Current scope |
| --- | --- |
| NIRISS/SOSS | Validated workflows for selected time-series datasets through the applicable official pipeline stages and QA |
| NIRSpec/BOTS | Validated workflows for selected bright-object time-series datasets through the applicable official pipeline stages and QA |

Validation is currently centered on the included TOI-3884 configurations. See the
[architecture notes](docs/architecture.md) and [initial workflow notes](docs/codex-first-task.md)
for more detail about design choices and current scope.

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
