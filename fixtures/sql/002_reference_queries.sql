-- Reference queries for foundational dmdul fixtures.
--
-- Save output from these queries beside the copied DBF evidence. The output is
-- calibration evidence only; production extraction must not query online views.

select * from SYSDBA.DMDUL_FIX_TINY order by ID;
select * from SYSDBA.DMDUL_FIX_TYPES order by ID;
select * from SYSDBA.DMDUL_FIX_NULLS order by ID;
select ID, length(V) as V_LEN, MARKER from SYSDBA.DMDUL_FIX_VLEN order by ID;
select ID, MARKER, length(PAD) as PAD_LEN from SYSDBA.DMDUL_FIX_MANY order by ID;
select * from SYSDBA.DMDUL_FIX_MOD order by ID;
select * from SYSDBA.DMDUL_FIX_UNDO order by ID;

select OWNER, OBJECT_NAME, OBJECT_ID, OBJECT_TYPE
  from DBA_OBJECTS
 where OWNER = 'SYSDBA'
   and OBJECT_NAME like 'DMDUL_FIX_%'
 order by OBJECT_NAME;

select OWNER, TABLE_NAME, COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
       DATA_PRECISION, DATA_SCALE, NULLABLE
  from DBA_TAB_COLUMNS
 where OWNER = 'SYSDBA'
   and TABLE_NAME like 'DMDUL_FIX_%'
 order by TABLE_NAME, COLUMN_ID;

select OWNER, SEGMENT_NAME, SEGMENT_TYPE, TABLESPACE_NAME, HEADER_FILE,
       HEADER_BLOCK, BYTES, BLOCKS, EXTENTS
  from DBA_SEGMENTS
 where OWNER = 'SYSDBA'
   and SEGMENT_NAME like 'DMDUL_FIX_%'
 order by SEGMENT_NAME, SEGMENT_TYPE;

select TABLESPACE_NAME, FILE_NAME, FILE_ID, RELATIVE_FNO, BYTES, BLOCKS,
       USER_BLOCKS, STATUS
  from DBA_DATA_FILES
 order by TABLESPACE_NAME, FILE_ID;

select ID, NAME, TYPE$, STATUS$, TOTAL_SIZE, FILE_NUM, USED_SIZE, FREE_EXTENTS
  from V$TABLESPACE
 order by ID;
