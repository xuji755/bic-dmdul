# Evidence Capture Workflow

This workflow turns a DM8 storage question into reproducible evidence. Use it
before implementing parsers for row layout, type encoding, MVCC, or UNDO.

## 1. Run Controlled Fixtures

On a disposable DM8 instance, run:

```sh
disql SYSDBA/<password> `pwd`/fixtures/sql/001_foundational_storage.sql
```

If the SQL dialect on the target build differs, adjust the fixture and record
the change in this repository before using the output as evidence.

## 2. Save Online Calibration Output

Run:

```sh
disql SYSDBA/<password> `pwd`/fixtures/sql/002_reference_queries.sql
```

Save the output with the captured file set. This output is calibration evidence
only. The extractor implementation must not depend on online views.

## 3. Capture Files From A Known State

Record the copy mode:

- clean shutdown copy;
- storage snapshot while database is open;
- live copy without snapshot;
- crash-state copy;
- rollback/undo-focused open-transaction copy.

Copy all required data files together. For MVCC and UNDO research, include the
rollback/undo files and any metadata/control files needed to identify them.

## 4. Capture Raw Page Evidence

Start by summarizing the database directory:

```sh
PYTHONPATH=src python3 -m dmdul.cli summarize-database \
  /dmdata/data/DAMENG \
  --catalog-pages 64 \
  --output evidence/database_summary.json
```

The summary records DBF files, group ids, file-number hints, SYSTEM candidates,
duplicate group/file hints, file-size/page0 diagnostics, and a sampled
page-kind catalog for each file.

Use deterministic markers from the fixture SQL to locate relevant pages:

```sh
PYTHONPATH=src python3 -m dmdul.cli capture-evidence \
  /dmdata/data/DAMENG/DMDUL_TS01.DBF \
  --label dmdul_fix_clean_001 \
  --copy-state clean-shutdown \
  --pages 0,1,8,16,96-98 \
  --marker FIX_TINY_ROW_0001 \
  --marker FIX_TYPES_POS_BOUND \
  --marker FIX_UNDO_UPDATE_1_OPEN \
  --output evidence/dmdul_ts01.json
```

The JSON records:

- file size and page size;
- selected page hashes;
- observed page-header fields;
- marker offsets and page-relative positions;
- marker context as hex and formatted dump.

For broader page-layout exploration, build a page catalog for the same file:

```sh
PYTHONPATH=src python3 -m dmdul.cli catalog-pages \
  /dmdata/data/DAMENG/DMDUL_TS01.DBF \
  --start-page 0 \
  --max-pages 512 \
  --output evidence/dmdul_ts01_page_catalog.json
```

The catalog records page-kind counts, tentative page-kind labels, empty pages,
page-number mismatches, nonzero page samples, and previous/next page-reference
samples. Labels are for evidence triage only and must not be treated as final
page parser semantics.

Fill in `docs/templates/evidence_manifest.json` beside the captured DBF files
so the copy method, reference output, and evidence JSON remain tied together.

## 5. Verify The Evidence Manifest

Before using a captured sample to justify a parser, verify the manifest:

```sh
PYTHONPATH=src python3 -m dmdul.cli verify-evidence evidence/manifest.json
```

The verifier checks:

- copy-state validity;
- copied file existence;
- copied file byte size if recorded;
- copied file SHA-256 if recorded;
- referenced evidence JSON existence;
- whether each evidence JSON is a recognized `capture-evidence` or
  `catalog-pages` output;
- required top-level keys for the detected evidence type.

## 6. Promote Only Proven Fields

A field can be added to a parser only after:

- at least one fixture creates a controlled state that changes the field;
- raw bytes show the expected change;
- online output explains the logical state;
- a unit test covers the parser using captured or minimized raw bytes.

If any condition is missing, document the observation as a hypothesis instead
of implementing production behavior.
