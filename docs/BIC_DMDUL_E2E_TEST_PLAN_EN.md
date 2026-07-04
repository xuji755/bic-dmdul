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
- Range, list, and range-hash partitioned tables.
- Compressed `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` as a known limitation scenario until HUGE storage-entry mapping is implemented.
- TRUNCATE recovery.
- DROP/orphan storage recovery.
- Stored procedure DDL extraction and rebuild.
- Ordinary and unique BTree index DDL extraction and rebuild.
- Negative diagnostics for missing files, damaged pages, unsupported indexes, unknown LOB locators, and raw orphan recovery without column metadata.

## Primary Acceptance Rule

A scenario is not considered passed merely because export completed. It passes only when the exported data is imported into a target user and compared back to the source or the saved pre-incident snapshot.

## Current Known Limitation

The July 4, 2026 full test run did not pass the compressed HUGE table scenario. `SYSDBA.DMDUL_HUGE_COMP_T` was readable online with 5000 rows, but offline bootstrap resolved its storage as `group=4,file=65535`; the current `file.dict` mapping cannot resolve that HUGE storage entry, so no exportable table dictionary was produced. This remains a known issue and must not be reported as supported until the HUGE storage mapping is implemented and retested through export, import, and bidirectional comparison.

For details, use [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md).
