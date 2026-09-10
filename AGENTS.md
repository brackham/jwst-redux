# AGENTS.md — jwst-redux

## Project purpose

Build a small, reliable Python package that automates retrieval of JWST observations from MAST and
execution of the official STScI `jwst` calibration pipeline.

The package is an orchestration layer, not an alternative calibration pipeline.

## First scientific use case

Find all archival JWST NIRISS/SOSS observations of TOI-3884, starting from the lowest useful archive
products, and reduce each dataset through the appropriate official JWST pipeline stages to the final
standard calibrated products.

## Design principles

- Keep target-specific information in YAML configuration, not Python source.
- Prefer official `astroquery.mast`, `jwst`, and CRDS behavior over custom reimplementations.
- Determine pipeline paths from observation metadata wherever practical.
- Treat Stage 3 associations/groups explicitly; do not assume every file is processed independently.
- Make every operation resumable and idempotent by default.
- Never overwrite or reprocess completed products unless explicitly requested.
- Provide a dry-run / plan mode before large downloads or expensive reductions.
- Record provenance: package git commit, Python version, `jwst` version, CRDS context, input products,
  parameters/overrides, outputs, timestamps, and success/failure state.
- Keep downloaded/calibrated science data outside git.
- Add tests before or alongside new behavior.
- Do not add custom astrophysical corrections or alternative extraction algorithms unless explicitly
  requested in a future task.

## Working style

Before making a large architectural change, summarize the proposed change and why it is needed.
When uncertain about current JWST pipeline behavior, consult current STScI/JWST documentation rather
than guessing. Keep commits focused and leave the repository in a testable state.

## Initial milestones

1. Query MAST for TOI-3884 and identify all NIRISS/SOSS science datasets.
2. Produce a correct reduction plan without downloading data.
3. Download one representative `_uncal` exposure/segment and run Stage 1.
4. Run the appropriate Stage 2 spectroscopy pipeline.
5. Correctly construct/use associations and run the appropriate TSO Stage 3 pipeline.
6. Run all TOI-3884 SOSS datasets end to end with resume support.
7. Generalize only after the TOI-3884 workflow is validated.
