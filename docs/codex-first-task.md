# First Codex task

Implement the archive-discovery and dry-run planning path for the first science example.

## Goal

The following command should query MAST and list all archival JWST NIRISS/SOSS science observations
matching TOI-3884 without downloading anything:

```bash
jwst-redux search configs/toi3884.yaml
```

Then:

```bash
jwst-redux plan configs/toi3884.yaml
```

should identify the archive products needed to start from uncalibrated data, group related exposures
or segments correctly, and print the proposed official JWST pipeline path for each dataset.

## Requirements

1. Use `astroquery.mast`; do not scrape MAST webpages.
2. Verify the current MAST field names and JWST product conventions from current documentation/API
   behavior rather than assuming them.
3. Normalize archive-specific records into the package's `Observation` and `Product` models.
4. For the first validated use case, correctly recognize NIRISS/SOSS time-series data and plan
   `Detector1Pipeline -> Spec2Pipeline -> Tso3Pipeline` when supported by the current JWST pipeline.
5. Do not download or run the calibration pipeline during this first task.
6. Add unit tests for filtering/classification logic. Mock remote archive calls in tests.
7. Print enough identifying metadata that the user can verify the discovered datasets before a large
   download.
8. Report total product count and estimated download size when MAST provides file sizes.
9. Keep TOI-3884-specific values in `configs/toi3884.yaml`.
10. Update README examples to match the implemented CLI exactly.

Stop after `search` and `plan` are working and tested. Do not proceed to bulk downloading until the
user has inspected the discovered TOI-3884 datasets and approved the plan.
