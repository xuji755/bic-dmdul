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

- [Project goal](docs/PROJECT_GOAL.md)
- [Foundational research plan](docs/FOUNDATIONAL_RESEARCH_PLAN.md)
- [Exploration and implementation tasks](docs/EXPLORATION_TASKS.md)
- [Exploration plan](docs/EXPLORATION_PLAN.md)
- [Technical exploration roadmap](docs/TECHNICAL_EXPLORATION_ROADMAP.md)
- [First storage exploration](docs/STORAGE_EXPLORATION_2026-06-29.md)
- [DM8 storage architecture notes](docs/DM8_STORAGE_ARCHITECTURE_NOTES.md)
