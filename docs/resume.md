# File identities and resume

Calibration records include each input/output's size, modification time, change time, and SHA-256.
The first record requires reading the file once. Later runs reuse that digest when size and both
recorded timestamps match. A timestamp change with the same size triggers checksum verification;
matching contents remain reusable. Hashes are cached by file identity and metadata within a process,
and verified reuse records the current metadata for future processes. A size or checksum mismatch
invalidates the affected stage and its downstream dependencies. Timestamp tolerances are not used.
Files changing during hashing are retried a bounded number of times, then rejected.

File identity is only one resume requirement. Archive membership, association contents and roles,
upstream successful run identities, pipeline parameters, software versions, and resolved CRDS context
must still match. An input checksum cannot compensate for changed calibration settings.
An association moved to another directory is equivalent only when both recorded document hashes
verify, both official schemas validate, and the complete JSON agrees after resolving member paths.
`overwrite: false` continues to preserve prior completed association products in revision directories.

With `retention: final`, an intact completed group can resume without deleted intermediates.
Retained raw inputs, any remaining intermediate association members, final outputs, and association
JSON files are checked. Retention verifies endpoint and candidate identities before deleting any
intermediates, in addition to its existing path and symlink checks.

## Older manifests

Historical success entries are preserved. Legacy output records containing only a path and size
retain their original size-based integrity check on first reuse; that reuse establishes a checksum
for subsequent checks. This does **not** establish byte identity with the historical output, and
cannot detect a same-size corruption that predates the first checksum record.

A legacy record with a changed timestamp and no earlier checksum is rejected. Hashing today's file
does not prove what its contents were before the change. Without independent earlier evidence,
the affected work needs regeneration. A missing checksum is never compared as if it were a digest.

If an independently recorded, trusted earlier snapshot exists, import it through the supported
command before rerunning the reduction:

```bash
jwst-redux import-checksums /path/to/manifest.json /path/to/earlier-checksums.json
```

The baseline format is:

```json
{"files": [{"path": "/absolute/path/input.fits", "size_bytes": 2880,
            "mtime_ns": 1700000000000000000,
            "sha256": "<64 lowercase hexadecimal characters>"}]}
```

Use the path, size, timestamp, and checksum recorded in the earlier snapshot. The command verifies
current file sizes and SHA-256 against that evidence, then appends an audit receipt containing the
baseline's own checksum and both file identities. Any mismatch aborts that manifest's import without
writing partial proof. Historical runs and FITS files are unchanged. Paths not referenced by that
manifest are ignored, allowing one snapshot to cover several workspaces. Imports are local and do
not query MAST or execute calibration. A newly generated snapshot after an unexplained change is
not a substitute for the missing historical evidence.
