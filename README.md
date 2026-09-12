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
`stage1.selection`. The TOI-3884 configuration selects GO-5799 Obs 002 / Visit 001 / Exposure
04101, `jw05799002001_04101_00001`; all three validated `_uncal` segments are processed in numeric
order. Each exact selector receives its own workspace below `output.root`, so manifests, logs,
products, QA, and Stage 3 associations remain isolated from other visits:

```bash
jwst-redux run configs/toi3884.yaml
jwst-redux run configs/toi3884.yaml --through stage2
jwst-redux run configs/toi3884.yaml --through stage3
jwst-redux run configs/toi3884.yaml --overwrite
jwst-redux qa configs/toi3884.yaml
jwst-redux qa configs/toi3884.yaml --stage stage3 --force
```

`run` reuses or downloads each segment, then runs the official `jwst.pipeline.Detector1Pipeline`
with normal defaults and writes `_rate` and `_rateints` products under `stage1/`. A matching
successful manifest entry plus intact outputs skips work independently for each segment. `--through
stage2` resolves each successful `_rateints` input from the manifest and runs the official
`jwst.pipeline.Spec2Pipeline`, producing `_calints` and `_x1dints` under `stage2/`. A failure stops
at that segment and is recorded; a rerun resumes prior completed segments. `--through stage3`
resolves every selected segment's `_calints` directly from successful Stage-2 manifest records,
writes an official JWST Level-3 association under `stage3/associations/`, and calls
`jwst.pipeline.Tso3Pipeline`. Association creation and TSO3 execution are resumable from their
member/run identity and intact recorded outputs. The CRDS cache remains external to this workspace.

Successful stages automatically create derived inspection QA (unless `qa.enabled: false`). QA is independent from
official calibration: plotting failure is recorded separately and never invalidates or reruns Detector1, Spec2, or
TSO3 products. The `qa` command rebuilds missing or stale figures from existing successful manifest products only;
it does not query MAST, download data, access CRDS, or execute calibration.
For SOSS extracted products, Stage 2 and Stage 3 QA includes spectra, white-light,
spectroscopic-time-series, wavelength-dependent robust-scatter, and point-to-point-difference diagnostics.
The white-light diagnostic has a combined-order panel followed by fixed, color-consistent panels for
Orders 1, 2, and 3.
Within each configured wavelength window, SOSS QA displays all mathematically valid channels; the
stricter pathological-variation criterion is recorded as a science-quality classification rather than
used to hide noisy diagnostic data.

For a selector-free, metadata-planned sequential batch, use the 5799/5863 configuration:

```bash
jwst-redux plan configs/toi3884-all-soss.yaml
jwst-redux run configs/toi3884-all-soss.yaml --all
```

`--all` intentionally uses the endpoint resolved separately for every branch: Detector1 for F277W,
Spec2 for single-integration CLEAR SOSS, and TSO3 only where metadata supports it. It does not accept
`--through`. Before any download or calibration, it prints each dataset/exposure, segment count,
planned path, and a local run/resume assessment. Execution is sequential. The default
`options.batch_failure_policy: continue` attempts later independent branches after a failure, records
that failure in the branch manifest, returns nonzero after the batch, and lets a later invocation
resume only incomplete work. Set the policy to `stop` to leave later branches untouched.

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
