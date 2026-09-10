# jwst-redux

`jwst-redux` is intended to be a thin, reproducible orchestration layer around two existing systems:

1. MAST / `astroquery.mast` for discovering and retrieving JWST data.
2. The official `jwst` package for calibration.

It should **not** reimplement JWST calibration algorithms. Its job is to make the common workflow

> discover → plan → download → calibrate → record provenance

fast, repeatable, resumable, and easy to apply to another JWST dataset.

The first end-to-end example will be all archival NIRISS/SOSS observations of TOI-3884.

## Intended CLI

```bash
jwst-redux search configs/toi3884.yaml
jwst-redux plan configs/toi3884.yaml
jwst-redux download configs/toi3884.yaml
jwst-redux run configs/toi3884.yaml
jwst-redux status configs/toi3884.yaml
jwst-redux all configs/toi3884.yaml
```

The package is currently a scaffold. See `docs/codex-first-task.md` for the first implementation task.

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
