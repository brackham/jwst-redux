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
later reduction. Enabled fixed-slit Stage-3 QA runs before retention; a QA failure preserves
upstream products and is reported separately from successful calibration.

## Stage 3 quality assurance

Set `qa.enabled: true` for automatic QA after Spec3 completes, including on resume when the
calibration stages are skipped. `qa.enabled: false` suppresses automatic generation. The explicit
`qa` command works regardless of that automatic setting:

```bash
jwst-redux qa configs/go11441.yaml --all --stage stage3
jwst-redux qa configs/go11441.yaml --all --stage stage3 --force
```

For reductions completed before this feature, point `output.root` at the **existing** reduction
root in your YAML and run the first command. No calibration is required. The fixed-slit `--all`
path reads local group manifests and honors program, observation, visit, target-name, grating and
filter restrictions; it does not query MAST, resolve CRDS, download inputs, reconstruct associations,
or run any pipeline. It covers recorded reductions only; use `search`/`plan` separately to discover
new observations. Target filters match recorded names (and explicit archive aliases), rather than
performing online name resolution. An explicit selected-exposure QA config resolves to its existing
group without requiring a particular representative nod.

The only science inputs required are the latest successful Stage-3 run's recorded `_x1d.fits`
and same-stem `_s2d.fits` products. Raw data, Stage-1/2 products, and association members can be
absent. This supports `retention: final`. Other Stage-3 intermediates are not QA dependencies.
The fixed-slit implementation currently offers Stage-3 QA only; no nod-by-nod Stage-2 diagnostic
or time-series light curve is generated.

Each official source/slit product gets a separate directory:

```text
<output.root>/groups/<group>/stage3/qa/<full-x1d-stem>/
    spectra.png
    snr_spectrum.png
    spectrum_2d.png
    checks.json
```

- `spectra.png` shows each EXTRACT1D spectrum separately, in its actual native flux units,
  with wavelength converted from the table units to µm. Nonfinite flux/wavelength, nonpositive
  wavelength and any nonzero DQ are masked for display. Every table is inspected; scalar table
  rows are wavelength samples, while vector rows remain separate spectra. Detector products,
  source/slit identities and orders are never averaged or stitched by QA.
- `snr_spectrum.png` shows signed flux divided by the associated FLUX_ERROR after converting
  uncertainty units to flux units. Only finite, positive uncertainties with usable flux contribute.
  Missing/incompatible uncertainties produce an explicitly unavailable S/N panel and report warning;
  they do not prevent the flux/image diagnostics from being generated.
- `spectrum_2d.png` shows the official combined SCI image with an asinh normalization. Nonfinite
  values, nonzero DQ where present, and zero/invalid WHT are masked. Standard resampled SlitModel
  products normally have WHT rather than DQ. The spectral axis uses a physical wavelength grid
  only when supported by explicit units or the official GWCS and a finite, monotonic, rectified
  grid. Otherwise it is explicitly labelled in **resampled pixels**, with a report warning. The
  spatial axis always uses resampled pixels; it is not labelled as angle or detector position.
- `checks.json` records input paths, source identifiers, target/configuration metadata, units,
  usable wavelength intervals, finite/invalid/DQ counts and fractions, uncertainty coverage,
  signed S/N percentiles (16/50/84), image coverage, contributing detectors from HDRTAB when
  present, and warnings/check outcomes. This is diagnostic QA, not a photometric-accuracy test.

Display limits use finite 1st/99th percentiles bounded by the median ±20 MAD-derived standard
deviations; constant tiny fluxes retain an appropriate relative scale. This bounds pathological
samples only in the display. Invalid samples remain gaps, and coordinate jumps exceeding five
median wavelength steps break plotted lines/images. QA never interpolates across gaps or alters
science arrays. The recorded parameters document these display choices.

Official Spec3 combines nod exposures and detectors for each slit/source before extraction.
Fixed-slit source IDs are pipeline bookkeeping: the several slit products from an ALLSLITS
exposure are **not several astrophysical stars**, and negative background/nod traces are not
independent sources. Labels show the actual slit and POINT/EXTENDED classification. Blank
`TARGNAME` falls back to `TARGPROP` for plot identification without changing either FITS keyword.
See the official [Spec3 products](https://jwst-pipeline.readthedocs.io/en/stable/jwst/pipeline/calwebb_spec3.html)
and [fixed-slit source organization](https://jwst-pipeline.readthedocs.io/en/stable/jwst/exp_to_source/main.html).

QA runs append their own manifest records, with input/output SHA-256 identities, upstream run ID,
parameters, software/git identity, original calibration CRDS context, and success/failure times.
Historical calibration entries and association membership are unchanged. Intact QA is skipped;
missing, changed or corrupt QA outputs are rebuilt. `--force` regenerates derived QA only.
Plots and reports are written to temporary files and atomically replaced, preserving an old
diagnostic if rendering fails and avoiding truncation of existing cloud placeholders.
Timestamp drift with matching content is accepted using the existing checksum verification.
Missing or altered science inputs fail QA without recalibration. Older records retain the existing
[legacy checksum limitations and import protections](resume.md#older-manifests).

Warnings about spectrum quality appear in the report, QA manifest entry, and CLI output (including
on reuse). A valid but fully masked extraction produces an explicitly empty spectrum/S/N panel,
retains its 2D diagnostic, and records `quality_status: unusable`; it can still complete QA generation.
This occurs for ancillary S200B1 extractions in Obs 007. A successful QA run means diagnostics were
generated, not that its spectrum passed quality checks. Missing/malformed required scientific
products fail QA, retain a diagnostic report, and make the explicit QA command exit nonzero.
Automatic QA failures issue a warning and record a failed QA state while
leaving successful calibration valid. Inspect `checks.json` alongside plots; a successful QA
generation is not a claim that the spectrum is science-ready.

## Targets without archive names

Fixed-slit spectra with missing or blank `TARGNAME` can proceed through Spec3 when their headers
identify one target through a consistent `TARGPROP` or positive `TARGID`/`TARGETID`. These inputs
must share a program, observation, and visit. Conflicting target identifiers or proposed coordinates
fail before association execution. Target coordinates (`TARG_RA`/`TARG_DEC`) can vary slightly
between nods and are not compared for exact equality.

The official `mkpool` and `Asn_Lv3NRSFSS` generator still construct and validate associations.
For an absent `TARGNAME` card, an empty value is supplied only to read-only headers in memory.
A checked unnamed group receives one pool-local target ID; that ID is not the archive's target
number. The identifying header values are recorded with the association. No target name is invented
and no input FITS file is edited. Inputs with complete target names follow the original path.

MAST masked values are normalized to missing values. When the archive name is missing, discovery
uses the actual proposal name (`targprop`). Earlier versions converted masked names to `0.0`.
Existing fixed-slit workspaces affected by that bug are reused only after checking their archive
membership and immutable raw FITS headers against the corrected target and observing configuration.
Historical provenance is retained, and matching Stage-1/2 successes remain reusable without overwrite.

## Proposal-wide and incremental runs

`configs/go11441.yaml` selects `proposal_id: 11441`, `instrument: NIRSPEC`, and
`exposure_type: NRS_FIXEDSLIT` without `target` or `archive_target_names`. Omitted target names
produce no MAST `targname` restriction. The same interface works for other proposals (including
`proposal_ids` lists); existing target-based YAML retains its original matching behavior. Each
exposure retains its archive target, and compatible groups remain separated by target and optical
configuration. Search follows every result page rather than stopping at MAST's default page limit.

Every invocation discovers the current archive contents. New targets get independent group
workspaces; new exposures in an existing group invalidate its completion checkpoint. Small archived
Stage-2 associations are refreshed during execution, including same-name revisions. Missing nod
members still fail closed before science downloads. Stage 1 reuses intact matching rates, Stage 2
reuses intact matching science/background dependencies, and Stage 3 checks the complete association
and upstream run identities. A changed association invocation writes revised products under
`stage2/revisions/<identity>/` or `stage3/revisions/<identity>/` when previous completed products
already occupy the official names; `overwrite: false` preserves those earlier products.

With `retention: final`, an unchanged successful group can skip directly to its recorded Stage-3
endpoint. This requires matching archive inputs/membership, freshly resolved Stage-2 association
hashes, local association hashes, all pipeline overrides, software versions, resolved CRDS context,
and intact recorded final outputs. Missing intermediates do not invalidate this endpoint. A changed
group or damaged final product regenerates intermediates as needed. A bookkeeping git commit alone
does not trigger recalibration. Older pruned group manifests are accepted only after checking
their recorded Stage-1/2 dependency chain and final retention receipt. Retention still waits for the complete group and records each
removed upstream FITS file; revised directories receive the same path and symlink safety checks.

Read-only plans assess discovery and local provenance; execution rechecks archive association
contents and resolves automatic CRDS context. A new CRDS context or archive revision can therefore
change an apparent resume into calibration. Raw inputs remain available for regeneration. This
workflow has regression coverage using synthetic program evolution and official association
fixtures; full-program GO 11441 calibration and authenticated program access remain unvalidated.

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

On 2026-10-23, GO 11441 observation 007 completed Spec3 from retained Stage-2 products for G140H,
G235H, and G395H through both S200A1 and S200A2, using `jwst` 3.0.0 and `jwst_1584.pmap`.
All six official associations contained three nods and both detectors. Each branch produced five
`_x1d` and five `_s2d` products, including a finite POINT-source spectrum in the intended slit.
The blank `TARGNAME` was retained; `TARGPROP=TIC49871464` established the target identity.
No Detector1 or Spec2 calibration ran, and all 216 existing science FITS files remained byte-identical.
Original provenance entries were preserved. Acceptance used `retention: all` to preserve intermediates.

The final Spec3 branch encountered a Google Drive read timeout and succeeded when retried directly
from its six checksum-verified calibrated inputs. Subsequent cloud synchronization changed some
file timestamps by nanoseconds without changing their contents. Resume now checks recorded content
hashes when metadata changes; [legacy checksum import](resume.md) supplies independent pre-drift
evidence for older association records that lack hashes. Repeated-run reuse is regression-tested
with timestamp changes, partial reductions, and genuine content changes.

The cloud-directory acceptance subsequently skipped all six Obs 007 branches twice with every
calibration callback forbidden. The saved pre-drift checksums were verified and imported through
the generic provenance API; historical entries were unchanged. All 336 existing science FITS files
remained byte-identical. The second run required no science-file checksum reads. This check retained
all intermediates; deletion under `retention: final` is covered by regression tests.

Failures, overwrite, provenance, and retention are fixture-tested with pipeline doubles and real
official associations. Full-program calibration, photometric accuracy, and proprietary-account login
remain unvalidated; authenticated behavior is tested with an injected backend.

### Fixed-slit QA acceptance (2026-10-25)

Stage-3 QA was inspected for Obs 007's six G140H/G235H/G395H and S200A1/S200A2 combinations,
using existing `jwst` 3.0.0 / `jwst_1584.pmap` products. The primary POINT-source products contain
both NRS1 and NRS2. The following ranges describe the official wavelength grid; the report also
records disconnected usable intervals so the detector gap is not mistaken for measured coverage.
S/N is per extracted sample, not per resolution element.

| Grating | Primary slit | Grid range [µm] | Samples | Invalid or DQ flagged | Median S/N |
| --- | --- | --- | --- | --- | --- |
| G140H | S200A1 | 0.967–1.819 | 3450 | 1.80% | 251.4 |
| G140H | S200A2 | 0.967–1.865 | 3640 | 1.70% | 249.3 |
| G235H | S200A1 | 1.655–3.051 | 3365 | 3.57% | 241.1 |
| G235H | S200A2 | 1.655–3.128 | 3555 | 3.35% | 233.2 |
| G395H | S200A1 | 2.861–5.143 | 3271 | 1.10% | 305.9 |
| G395H | S200A2 | 2.861–5.273 | 3461 | 4.16% | 307.0 |

Visual inspection of all 18 primary plots showed consistent spectral structure between the two
slits, unconnected detector gaps, and central positive/negative nod-subtraction structure in the
resampled images. The ancillary S200B1 extractions have no usable extracted flux: every channel is
flagged and flux is nonfinite. Their empty flux/S/N panels and `quality_status: unusable` reports
identify this explicitly while preserving the readable, sometimes extreme-valued 2D diagnostics.
These ancillary products should not be interpreted as additional stellar spectra.

The in-place CLI acceptance generated 90 PNGs and 30 JSON reports, skipped all 30 product pairs
on repeat, and regenerated all 30 with `--force`, with no generation failures. Detector1, Spec2,
Spec3, archive discovery, and download callbacks were forbidden throughout the QA invocations;
zero calibration calls were executed. The full regression suite passed 250 tests, including SOSS,
BOTS, final-retention backfill, missing/corrupt inputs/outputs, and timestamp drift. Ruff and
formatting checks also passed.

All 60 final `_x1d`/`_s2d` inputs were copied read-only and verified against the independent
pre-QA SHA-256 baseline. All six manifests retained every pre-existing record unchanged, with only
QA records appended; no science FITS or association file was opened for writing. A broader fresh
post-run hash scan of all 156 retained FITS and 91 associations could not finish because Drive
returned `OSError 89: Operation canceled` on retained files. Thus full byte-level revalidation of
those additional raw/source-calibrated intermediates and association files remains unconfirmed;
this did not affect the successful Stage-3 QA invocations or require recalibration.

Cloud acceptance encountered intermittent Google Drive reads and an overwrite-open stall. QA now
narrows canonical group names before hydrating unrelated manifests for an observation-filtered
query, and atomically replaces plots/reports. Marking final products available offline can reduce
cloud hydration delays. These changes do not relax checksum checks or require calibration.

Useful follow-ups include nod-by-nod QA when intermediates are available and support for
archived pools or explicitly supplied associations when MAST lacks an observation-
candidate ASN. Spectral stitching and higher-level science analysis remain outside this change.

Official references: [Level-2 rules](https://jwst-pipeline.readthedocs.io/en/latest/jwst/associations/level2_asn_rules.html),
[Spec2](https://jwst-pipeline.readthedocs.io/en/latest/jwst/pipeline/calwebb_spec2.html),
[Spec3](https://jwst-pipeline.readthedocs.io/en/latest/jwst/pipeline/calwebb_spec3.html), and
[Astroquery MAST authentication](https://astroquery.readthedocs.io/en/latest/mast/mast.html#authentication).
