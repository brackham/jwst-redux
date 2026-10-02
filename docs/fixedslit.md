# NIRSpec Fixed Slit

`NIRSPEC/NRS_FIXEDSLIT` uses the official non-TSO path:

```text
_uncal -> Detector1Pipeline -> _rate
_rate associations -> Spec2Pipeline -> _cal / _x1d (plus saved intermediates)
_cal association -> Spec3Pipeline -> Level-3 spectral products
```

Run the public example with:

```bash
jwst-redux plan configs/public-nirspec-fixedslit.yaml
jwst-redux run configs/public-nirspec-fixedslit.yaml --all
```

`query.grating` and `query.filter` optionally narrow discovery. Grating matching uses normalized
metadata because MAST's JWST mission service exposes the disperser through `opticalElements`.

The scientific dataset identity is program, observation, and visit number. Compatible execution
groups additionally preserve target, grating/filter, primary fixed slit, and subarray. A group runs
all useful detector products and nod exposures together, under `output.root/groups/`; it is not a
BOTS detector branch. STScI's `nrsfss_valid_detector` determines the useful optical paths. Stage 2
keeps detectors separate through its official associations; Stage 3 includes both useful detectors.
S200A1 and S200A2 remain separate groups in this initial implementation.

A selected-exposure `run --through stage2` or `stage3` expands to the complete compatible group
containing that exposure. Omit `stage1.selection.detector` for this mode. Review the discovery plan
first: this operation downloads all necessary science and background nod inputs in the group.

## Association policy

Stage 2 downloads the small archived observation-candidate `Asn_Lv2NRSFSS` associations listed by
MAST, loads and validates them with `jwst.associations`, and changes only member paths. Each
invocation has exactly one science exposure. Science, background, and other archived member roles
are preserved. Missing members, absent associations, and contradictory archive assignments fail
before science downloads or CRDS access; there is no science-only fallback.

The official Level-2 rule uses `EXPSPCIN`, `NUMDTHPT`, `SUBPXPTS`/`SUBPXPNS`, and `PATT_NUM` when
forming nod relationships. `EXPSPCIN` is an archive pool field absent from the public example's
primary FITS header. Consequently, constructing a pool from FITS alone with `mkpool` is insufficient
to safely reconstruct those relationships. This implementation uses archive assignments instead
of inventing the missing field or local nod/background logic.

Stage 3 builds a pool from the calibrated FITS headers using official `mkpool`, observation
candidate constraints, and `Asn_Lv3NRSFSS` generation/finalization. Association schema validation,
stable member ordering, relative local paths, and content hashes support reproducible local files.
Official source/slit placeholders in product names are retained for `Spec3Pipeline` to resolve.
Official association calls run from the association directory with absolute input/output paths,
so nested relative member paths also work with Spec2's current path handling.

Provenance includes the original archive association URI/hash, local association hash and members,
upstream run IDs, input file identities, software/git versions, CRDS context, overrides, logs,
actual outputs, timestamps, and success/failure state. TSO3 and Spec3 share manifest execution and
logging machinery. Fixed-slit Stage 1 requires `_rate`; a saved `_rateints` is optional and recorded
for retention. Stage 2 requires `_cal` and `_x1d` and records additional saved products. Stage 3
records actual files created or updated rather than requiring a fixed output list.

`retention: final` runs only after the entire group succeeds, retaining raw products, final outputs,
associations, logs, and manifests. Deleted upstream products must be regenerated if needed for a
later reduction. Time-series QA is explicitly unavailable/skipped for fixed slit.

## Overrides and authentication

Per-class overrides are available in both selected and batch configs:

```yaml
pipeline:
  stages: auto
  class_overrides:
    Spec3Pipeline:
      steps:
        extract_1d:
          save_results: true
```

Existing `overrides`, `spec2_overrides`, and `tso3_overrides` remain supported. Do not supply both
representations for the same pipeline class. Overrides still use official pipeline parameters.

Public data require no authentication. For proprietary data, create a token through
[MAST's token management](https://auth.mast.stsci.edu/) using the MyST account with program access,
and supply it through `MAST_API_TOKEN` in the process environment. Astroquery logs in with
`store_token=False`. Never place credentials in YAML, command arguments, notebooks, or git.
Credential fields in YAML are rejected; transport diagnostics, pipeline logs, and manifests are
protected against disclosure of the active token. Authentication does not grant program access
that the MyST account lacks.

## Public validation and limits

Acceptance metadata were queried on 2026-10-02 with installed `jwst` 3.0.0:

| Program | Observation / visit | Exposures | Configuration | Slit / nods |
| --- | --- | --- | --- | --- |
| Commissioning 1128 | 006 / 001 | `jw01128006001_0310c_00001`–`00003` | G235M / F170LP | S200A1 / 3-POINT-NOD |
| Commissioning 1128 | 006 / 001 | `jw01128006001_03106_00001`–`00003` | G235H / F170LP | S200A1 / 3-POINT-NOD |

The first case is the [STScI fixed-slit pipeline notebook](https://github.com/spacetelescope/jwst-pipeline-notebooks/blob/main/notebooks/NIRSPEC/FSlit/JWPipeNB-NIRSpec-FS.ipynb)
example, described there as standard star 1808347. MAST names it `TYC 4433-1800-1`. It tests three
nods and one useful detector; the high-resolution case also tests both detector inputs. Small public
MAST records and nine archive associations are regression fixtures. No science arrays are committed.

Live CLI discovery/planning and the complete three-nod G235M calibration succeeded with `jwst`
3.0.0 and resolved CRDS context `jwst_1584.pmap`. All three Stage-2 products record background
subtraction as `COMPLETE`. Spec3 created combined `_cal`, `_crf`, `_s2d`, and `_x1d` FITS files;
the final spectrum has 1,334 finite wavelength samples spanning 1.647–3.072 µm and 1,281 finite flux
samples, with standard µm/Jy units. Full pipeline logs and manifest records were checked. A repeat
run reused all Stage 1, Stage 2, and Stage 3 results without recalibration. Products remain in
ignored `work/public-nirspec-fixedslit/` and are not committed.

The G235H two-detector path, failures, overwrite, provenance, and retention are fixture-tested with
pipeline doubles and real official associations. High-resolution/S200A2 calibration, photometric
accuracy, and proprietary-account login have not been validated locally. Program 11441 was not
used; authenticated behavior is tested with an injected backend. No fixed-slit QA is generated.

Useful follow-ups are high-resolution/two-slit public calibration acceptance, fixed-slit QA, and
support for archived pools or explicitly supplied associations when MAST lacks an observation-
candidate ASN. Spectral stitching and higher-level science analysis remain outside this change.

Official references: [Level-2 rules](https://jwst-pipeline.readthedocs.io/en/latest/jwst/associations/level2_asn_rules.html),
[Spec2](https://jwst-pipeline.readthedocs.io/en/latest/jwst/pipeline/calwebb_spec2.html),
[Spec3](https://jwst-pipeline.readthedocs.io/en/latest/jwst/pipeline/calwebb_spec3.html), and
[Astroquery MAST authentication](https://astroquery.readthedocs.io/en/latest/mast/mast.html#authentication).
