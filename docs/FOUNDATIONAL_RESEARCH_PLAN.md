# Foundational DM8 Storage Research Plan

This document is the gate before building more extraction features. A table
extractor is only valuable if it can explain why a row is visible and how every
byte in the row maps to a logical value. Until the structures below are
understood and validated, extraction code must remain experimental.

## Research Principle

Every supported structure needs four artifacts:

1. Controlled SQL fixture that creates the condition.
2. Online reference output used only for calibration.
3. Raw file/page bytes captured from a known copy mode.
4. A parser test that proves the byte interpretation without querying online
   dictionary views.

No feature should be promoted from research mode to supported mode until these
four artifacts exist.

## Copy-State Classification

Before decoding rows, classify the input file set:

- clean shutdown copy;
- storage snapshot while database is open;
- live file copy without snapshot;
- crash-state files;
- incomplete file set.

Required observations:

- database identity across all files;
- per-file checkpoint or SCN/LSN fields;
- per-page checkpoint or SCN/LSN fields;
- page checksum or validation field if present;
- required undo/rollback files;
- whether redo is required to make files mutually consistent.

Validation target:

- The tool can say "safe for cold-consistent exploration", "requires MVCC/UNDO",
  "requires redo", or "unsupported copy" before reading table rows.

## Tablespace And File Structures

Questions to answer:

- Where is the canonical tablespace id stored?
- How are multi-file tablespaces represented?
- Which page 0 fields are database id, file id, page size, status,
  checkpoint/SCN/LSN, total pages, and free-space metadata?
- How are reserved pages, bitmap pages, and user-allocatable pages identified?
- How does `SYS.V$DATAFILE` map to physical dictionary or control structures?

Experiments:

- Create one-file and multi-file tablespaces with distinct sizes.
- Resize/add data files and compare page 0 plus early control pages before and
  after.
- Create clean-shutdown and live-snapshot copies and compare checkpoint fields.
- Correlate online `V$DATAFILE`, `V$TABLESPACE`, and file headers only during
  calibration.

Deliverables:

- File header field map.
- Tablespace-to-file resolver.
- Page identity validator.
- Missing or mixed-file diagnostic.

## Segment, Extent, And Allocation Structures

Questions to answer:

- Which dictionary or segment fields define a table segment's allocated pages?
- How are extents/clusters linked or mapped?
- Where are high-water marks and free page lists stored?
- How do root/header pages differ from leaf/data pages?
- How does allocation change after insert, delete, update, truncate, and extend?

Experiments:

- Tiny table in one root page.
- Multi-page table in one extent.
- Multi-extent table.
- Multi-file tablespace table.
- Table after delete and insert reuse.
- Table after truncate and reinsert.

Deliverables:

- Segment header parser.
- Extent/allocation map parser.
- Verified page traversal algorithm that neither misses nor duplicates pages.
- Explicit unsupported diagnostic for unknown allocation forms.

## Page Format

Questions to answer:

- What are all common page header fields?
- Which page kinds exist for files, bitmaps, BTREE roots, BTREE internals,
  leaves, dictionary pages, undo pages, and free pages?
- Where are row/slot counts, slot directory offsets, free-space offsets,
  object ids, and transaction/checkpoint fields stored?
- Are checksums present, and when are they updated?

Experiments:

- Compare page headers across every known page kind.
- Change row counts and free space with controlled inserts/deletes.
- Force leaf splits and compare root/internal/leaf page changes.
- Capture undo pages during active transactions.

Deliverables:

- Common page-header parser.
- Page-kind registry with evidence.
- Slot directory parser.
- Page consistency checks.

## Record Structure

Questions to answer:

- Which bytes are row length, flags, lock/MVCC metadata, column count, NULL
  bitmap, column directory, fixed values, variable values, and row trailer?
- How are deleted, updated, migrated, chained, and locked rows marked?
- Does the row contain an undo pointer? If so, how is it encoded?
- How are NULLs represented for fixed and variable columns?
- How are variable-length values addressed when columns are reordered or NULL?

Experiments:

- One-column tables for each type.
- Mixed fixed-only, variable-only, and fixed+variable tables.
- Every NULL position in a five-column table.
- Rows near page capacity.
- Updates that grow and shrink variable columns.
- Deletes followed by slot reuse.
- Long rows, overflow/chained rows, and LOB locators as negative or later-scope
  cases.

Deliverables:

- Row header parser.
- NULL bitmap and column directory parser.
- Deleted/update/slot-reuse interpretation.
- Row fixture suite with byte diagrams.

## Data Type Encoding

Questions to answer:

- Exact endian, length, bias, epoch, scale, precision, and sign encoding for
  every supported type.
- Difference between SQL `FLOAT`, `REAL`, `DOUBLE`, `DECIMAL`, `NUMBER`, and
  integer storage.
- Date/time epoch and fractional-second storage.
- Character encoding and padding rules for `CHAR`, `VARCHAR`, and long strings.
- Distinction between SQL NULL and empty string.

Experiments:

- Boundary values: min, max, zero, negative, powers of two, precision edges.
- Date/time boundaries: leap day, month/year rollover, midnight, fractional
  seconds, timezone-neutral assumptions.
- Decimal cases: positive/negative, leading/trailing zeros, scale changes,
  large precision.
- Character cases: ASCII, multibyte UTF-8/GBK if DM stores database charset,
  trailing spaces, empty strings, long values.

Deliverables:

- Type-by-type encoding table.
- Decoder tests using raw bytes and online expected values.
- Unsupported type matrix.

## MVCC And UNDO

Questions to answer:

- Where is transaction identity stored in a row?
- How does a row point to its undo record or previous image?
- Where is transaction status stored on disk?
- How are committed, uncommitted, rolled-back, deleted, and updated versions
  distinguished?
- What information is needed to choose a visibility point?
- When is redo required before undo can be trusted?

Experiments:

- Commit insert, delete, and update separately.
- Copy files before commit for insert/delete/update.
- Roll back insert/delete/update and compare data pages plus undo pages.
- Keep a transaction open while inspecting undo/rollback files.
- Crash or simulate crash-state only after ordinary undo layout is understood.

Deliverables:

- Row MVCC field map.
- Transaction status parser.
- Undo segment/page/record parser.
- PRE IMAGE reconstruction algorithm.
- Visibility decision table.
- Redo-required diagnostic rules.

## PRE IMAGE Algorithm Sketch

The final algorithm must be evidence-driven, but the expected shape is:

1. Decode the current row version and its MVCC metadata.
2. Resolve the transaction id in the transaction table or equivalent metadata.
3. If the row version is committed and visible at the chosen point, emit it.
4. If the row version is uncommitted or newer than the chosen point, follow the
   undo pointer.
5. Decode the undo record header and before-image payload.
6. Apply the before-image to reconstruct the previous row version.
7. Repeat through the undo chain until a visible version, deleted marker, or
   unsupported condition is reached.
8. Emit nothing for rows whose visible version is deleted.
9. Fail if any undo link, transaction status, or redo dependency is unknown.

This algorithm must not be implemented as production behavior until the row
MVCC fields, transaction status source, and undo record format are verified
with fixtures.

## Research Gate

Before continuing feature work, the next concrete tasks are:

- build a fixture script for controlled DM8 tables and transaction states;
- capture raw page slices for tablespace, segment, row, and undo pages;
- write byte-level notes for row header, NULL bitmap, and type encoding;
- implement parsers only for fields with direct fixture evidence;
- keep extraction commands labeled experimental until visibility semantics are
  proven.
