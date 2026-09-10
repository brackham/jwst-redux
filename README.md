# jwst-redux

`jwst-redux` is intended to be a thin, reproducible orchestration layer around two existing systems:

1. MAST / `astroquery.mast` for discovering and retrieving JWST data.
2. The official `jwst` package for calibration.

It should **not** reimplement JWST calibration algorithms. Its job is to make the common workflow

> discover → plan → download → calibrate → record provenance

fast, repeatable, resumable, and easy to apply to another JWST dataset.

The first end-to-end example will be all archival NIRISS/SOSS observations of TOI-3884.

The implemented first milestone is read-only with respect to MAST:

```bash
jwst-redux search configs/toi3884.yaml
jwst-redux plan configs/toi3884.yaml
```

`search` uses the target matching strategy declared in the YAML configuration and reports scientific
datasets/visits, their exposure-level MAST records, and each exposure's uncalibrated segment products.
`plan` validates segment completeness, forms one or more metadata-compatible reduction branches
within each scientific dataset, and reports the official pipeline path. Neither command downloads
data, creates a workspace or association, accesses CRDS, or executes the pipeline.

For the TOI-3884 example, `query.archive_target_names` records the target names validated in MAST
(`TOI-3884` and `TOI-3884b`) while `query.target` retains the scientific target identity. The optional
`query.proposal_ids` field may be `null` for all proposals, one proposal ID, or a YAML list of IDs.

## Intended CLI

```bash
jwst-redux download configs/toi3884.yaml
jwst-redux run configs/toi3884.yaml
jwst-redux status configs/toi3884.yaml
jwst-redux all configs/toi3884.yaml
```

The remaining commands are currently scaffolded. See `docs/codex-first-task.md` for the implemented
discovery and planning milestone.

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest
```

If another supported Python version is preferable for the current `jwst` release, use it instead and
record the choice in the repository once verified.
