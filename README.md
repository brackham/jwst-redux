# jwst-redux

`jwst-redux` is intended to be a thin, reproducible orchestration layer around two existing systems:

1. MAST / `astroquery.mast` for discovering and retrieving JWST data.
2. The official `jwst` package for calibration.

It should **not** reimplement JWST calibration algorithms. Its job is to make the common workflow

> discover → plan → download → calibrate → record provenance

fast, repeatable, resumable, and easy to apply to another JWST dataset.

The first end-to-end example will be all archival NIRISS/SOSS observations of TOI-3884.

Discovery and planning remain read-only:

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

The current write milestone uses the explicit scientific-dataset and exposure selector in
`stage1.selection`. The TOI-3884 configuration selects GO-5799 Obs 001 / Visit 001,
`jw05799001001_04101_00001`; all three validated `_uncal` segments are processed in numeric order:

```bash
jwst-redux run configs/toi3884.yaml
jwst-redux run configs/toi3884.yaml --through stage2
jwst-redux run configs/toi3884.yaml --overwrite
```

`run` reuses or downloads each segment, then runs the official `jwst.pipeline.Detector1Pipeline`
with normal defaults and writes `_rate` and `_rateints` products under `stage1/`. A matching
successful manifest entry plus intact outputs skips work independently for each segment. `--through
stage2` resolves each successful `_rateints` input from the manifest and runs the official
`jwst.pipeline.Spec2Pipeline`, producing `_calints` and `_x1dints` under `stage2/`. A failure stops
at that segment and is recorded; a rerun resumes prior completed segments. Once all segments have
intact `_calints` outputs, the run reports Stage-3 readiness only: association construction and
`Tso3Pipeline` execution are still unimplemented. The CRDS cache remains external to this workspace.

The remaining workflow commands are scaffolded:

```bash
jwst-redux status configs/toi3884.yaml
jwst-redux all configs/toi3884.yaml
```

See `docs/codex-first-task.md` for the discovery and planning milestone.

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
