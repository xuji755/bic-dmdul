# bic-dmdul End-to-End Test Plan

This document is the English index for the complete `bic-dmdul` end-to-end test plan. The executable test design is maintained in Chinese in [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md), because the current DM8 lab workflow, SQL naming, and validation notes are operated in Chinese.

## Scope

The test plan verifies the full recovery chain:

1. Create deterministic source tables, indexes, and stored procedures.
2. Capture or reuse a stable DM8 data-file snapshot.
3. Run `bic-dmdul prepare` and `bootstrap -b`.
4. Export data using DUL text, row archive, parts manifest, LOB attachments, and raw orphan modes.
5. Convert exports to SQL with `import-data`.
6. Import into a target user.
7. Compare source and target data using row counts, bidirectional `MINUS`, aggregates, samples, and LOB hashes.
8. Export stored procedures with `dump-procedures`, rebuild them in the target user, execute them, and compare outputs.
9. Export ordinary indexes with `dump-indexes`, rebuild them in the target user, and validate column order, uniqueness, and usability.
10. Confirm negative scenarios report diagnostics instead of fabricating success.

## Required Scenario Families

- Scalar type coverage table.
- Large multi-extent table.
- CLOB/BLOB table with inline, out-of-line, updated, and NULL LOBs.
- `STORAGE(USING LONG ROW)` table with long `VARCHAR` values, row archive export, SQL regeneration, import, and aggregate comparison.
- Row-migration candidate table where old physical rows outside the page slot directory must not be exported as live rows.
- Range, list, and range-hash partitioned tables.
- Compressed `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` through the internal `$RAUX` row-storage mapping.
- TRUNCATE recovery.
- DROP/orphan storage recovery.
- Stored procedure DDL extraction and rebuild.
- Ordinary and unique BTree index DDL extraction and rebuild.
- Negative diagnostics for missing files, damaged pages, unsupported indexes, unknown LOB locators, and raw orphan recovery without column metadata.

## Primary Acceptance Rule

A scenario is not considered passed merely because export completed. It passes only when the exported data is imported into a target user and compared back to the source or the saved pre-incident snapshot.

## Compressed HUGE Result

The July 4, 2026 follow-up run passed one compressed HUGE table scenario for `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'`. `SYSDBA.DMDUL_HUGE_COMP_T` was readable online with 5000 rows. Its main storage child has `ROOTFILE=-1` and `ROOTPAGE=-1`, so targeted bootstrap falls back to a SYSTEM scan, preserves the auxiliary table rows, and maps the main table columns to `$RAUX` storage `33596002` at `group=4,root_file=0,root_page=949488`. Export wrote 5000 rows, import rebuilt a target table, and bidirectional `MINUS` returned 0/0.

A separate `QUERY HIGH` test (`SYSDBA.DMDUL_HUGE_HIGH_T`, 20000 rows, `STORAGE(SECTION(1024)) COMPRESS LEVEL 9 FOR 'QUERY HIGH'`) proved that HUGE is a column-store structure, not just a row-store alias. `$AUX` contained 100 column-section rows and 95 of them had `CPR_FLAG=Y`; `$RAUX` contained only 544 tail rows. The current tool therefore marks `$RAUX` proxy mapping with `huge-raux-proxy-mapping`, and `dump-data --strict` returns `strict_ok=false` / `tables_strict_failed=1` until compressed column-section decoding is implemented.

For details, use [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md).
