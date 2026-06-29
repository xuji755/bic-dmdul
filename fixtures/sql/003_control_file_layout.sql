-- DM8 control-file layout fixture for dmdul.
--
-- Run on a disposable database as SYSDBA. This script is intentionally split
-- into manual phases because dmdul needs dm.ctl snapshots between operations.
-- After each phase, cleanly checkpoint/shutdown if possible, copy dm.ctl, and
-- run:
--
--   PYTHONPATH=src python3 -m dmdul.cli summarize-control-file \
--     /path/to/snapshot/dm.ctl \
--     --output evidence/dmctl_<phase>.json
--
-- Compare adjacent snapshots with:
--
--   PYTHONPATH=src python3 -m dmdul.cli compare-control-files \
--     evidence/<before>/dm.ctl \
--     evidence/<after>/dm.ctl \
--     --context-bytes 32 \
--     --output evidence/dmctl_<before>_to_<after>.json
--
-- The SQL records the intended logical operation only. Do not treat online
-- views as extractor inputs; use them only to label captured bytes.

-- Phase 0: baseline.
-- Capture dm.ctl before running any statement below.

-- Phase 1: create a one-file tablespace.
create tablespace DMDUL_CTL_TS
  datafile 'DMDUL_CTL_TS01.DBF'
  size 32;

-- Snapshot label: after_create_tablespace
-- Expected control-file changes:
--   - tablespace count/list candidate
--   - DMDUL_CTL_TS name/path candidate
--   - first data-file entry candidate

-- Phase 2: add a second data file to the same tablespace.
alter tablespace DMDUL_CTL_TS
  add datafile 'DMDUL_CTL_TS02.DBF'
  size 48;

-- Snapshot label: after_add_datafile
-- Expected control-file changes:
--   - file count/list candidate
--   - second data-file entry candidate
--   - possible checkpoint/status update fields

-- Phase 3: resize the first data file.
alter database
  datafile 'DMDUL_CTL_TS01.DBF'
  resize 64;

-- Snapshot label: after_resize_datafile
-- Expected control-file changes:
--   - size/page-count field candidate for DMDUL_CTL_TS01.DBF
--   - possible checkpoint/status update fields

-- Phase 4: create ordinary data so data-file headers can be correlated with
-- control-file file numbers and page counts.
create table SYSDBA.DMDUL_CTL_MARKER (
  ID int primary key,
  MARKER varchar(64)
) tablespace DMDUL_CTL_TS;

insert into SYSDBA.DMDUL_CTL_MARKER values (1, 'DMDUL_CTL_MARKER_0001');
commit;

-- Snapshot label: after_marker_table
-- Expected control-file changes:
--   - checkpoint/status fields only, unless allocation metadata is also stored
--     in dm.ctl for this build.

-- Optional cleanup after all snapshots are captured:
-- drop table SYSDBA.DMDUL_CTL_MARKER;
-- drop tablespace DMDUL_CTL_TS;
