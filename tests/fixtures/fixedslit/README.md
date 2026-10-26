Public MAST JWST mission metadata and archived official Level-2 association JSON, retrieved
2026-10-02 for program 1128, observation 006, visit 001. The `0310c` activities are G235M/F170LP;
`03106` activities are G235H/F170LP. Both use S200A1 with three nod positions.

`exposures.json` contains six actual exposure records. `products.json` contains twelve raw product
records and nine small association records; the NRS2 G235M raw files are present in the archive
but are not useful science inputs according to the official fixed-slit detector rule. Association
JSON files are preserved as returned by MAST, including their original candidate rule and version.

The tests synthesize small FITS files with these optical identities and nod positions, with no
science arrays. The primary header of public `jw01128006001_0310c_00001_nrs1_uncal.fits` was inspected
separately: it lacks EXPSPCIN, and uses PATT_NUM=1, NUMDTHPT=3, SUBPXPTS=1, TSOVISIT=false.
