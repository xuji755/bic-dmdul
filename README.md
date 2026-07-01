# dmdul

`dmdul` is a DM8 offline data extraction project inspired by Oracle DUL.

The goal is to extract table data directly from DM data files or ASM disk
groups when the DM database instance cannot start.

Initial investigation areas:

- DM8 tablespace, segment, extent, page, and row storage layout.
- DM8 BTREE table organization and row decoding.
- DM ASM disk group metadata and file mapping.
- Catalog metadata needed to map object ids, columns, data types, and storage.
- Offline scan and recovery strategies when system catalog access is limited.

See [docs/TEST_ENVIRONMENT.md](docs/TEST_ENVIRONMENT.md) for the current test
environment notes.

Research notes:

- [中文使用手册](docs/USER_MANUAL_CN.md)
- [Project goal](docs/PROJECT_GOAL.md)
- [Foundational research plan](docs/FOUNDATIONAL_RESEARCH_PLAN.md)
- [Evidence capture workflow](docs/EVIDENCE_CAPTURE_WORKFLOW.md)
- [Evidence manifest template](docs/templates/evidence_manifest.json)
- [Exploration and implementation tasks](docs/EXPLORATION_TASKS.md)
- [Exploration plan](docs/EXPLORATION_PLAN.md)
- [Technical exploration roadmap](docs/TECHNICAL_EXPLORATION_ROADMAP.md)
- [First storage exploration](docs/STORAGE_EXPLORATION_2026-06-29.md)
- [DM8 storage architecture notes](docs/DM8_STORAGE_ARCHITECTURE_NOTES.md)
- [DM8 page structure notes](docs/DM8_PAGE_STRUCTURE_NOTES.md)

Bootstrap dictionary preprocessing:

```bash
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m dmdul.cli \
  --page-size 8192 bootstrap /path/to/offline/dbcopy \
  --output-dir tmp/bootstrap-dicts -b --json
```

The `bootstrap -b` stage scans `SYSTEM.DBF` and writes the first-stage
`control.ctl`, `file.dict`, `user.dict`, `tab.dict`, and `col.dict` artifacts.
Current SYS dictionary extraction is still marked heuristic while the complete
SYS row layouts are being calibrated.
