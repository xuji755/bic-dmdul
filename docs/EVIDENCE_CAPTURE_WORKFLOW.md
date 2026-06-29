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

Use deterministic markers from the fixture SQL to locate relevant pages:

```sh
PYTHONPATH=src python3 -m dmdul.cli capture-evidence \
  /dmdata/data/DAMENG/DMDUL_TS01.DBF \
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

## 5. Promote Only Proven Fields

A field can be added to a parser only after:

- at least one fixture creates a controlled state that changes the field;
- raw bytes show the expected change;
- online output explains the logical state;
- a unit test covers the parser using captured or minimized raw bytes.

If any condition is missing, document the observation as a hypothesis instead
of implementing production behavior.
