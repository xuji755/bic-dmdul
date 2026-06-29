-- Foundational dmdul storage fixtures for DM8.
--
-- Run on a disposable database as SYSDBA or another privileged test account.
-- The script deliberately uses deterministic names and marker values so raw
-- DBF bytes can be correlated with online reference output.

drop table if exists SYSDBA.DMDUL_FIX_TINY;
drop table if exists SYSDBA.DMDUL_FIX_TYPES;
drop table if exists SYSDBA.DMDUL_FIX_NULLS;
drop table if exists SYSDBA.DMDUL_FIX_VLEN;
drop table if exists SYSDBA.DMDUL_FIX_MANY;
drop table if exists SYSDBA.DMDUL_FIX_MOD;
drop table if exists SYSDBA.DMDUL_FIX_UNDO;

create table SYSDBA.DMDUL_FIX_TINY (
  ID int primary key,
  MARKER varchar(64) not null,
  N_BIG bigint,
  V_SMALL varchar(64),
  C_FIXED char(12)
);

insert into SYSDBA.DMDUL_FIX_TINY values
  (1, 'FIX_TINY_ROW_0001', 1111111111, 'SMALL_A', 'CHAR_A'),
  (2, 'FIX_TINY_ROW_0002', -2222222222, 'SMALL_B', 'CHAR_B');

create table SYSDBA.DMDUL_FIX_TYPES (
  ID int primary key,
  N_INT int,
  N_BIG bigint,
  N_DEC decimal(18, 4),
  N_NUM numeric(30, 10),
  N_FLOAT float,
  N_DOUBLE double,
  D_DATE date,
  T_TIME time,
  TS_VAL timestamp,
  MARKER varchar(64)
);

insert into SYSDBA.DMDUL_FIX_TYPES values
  (1, 0, 0, 0.0000, 0.0000000000, 0.0, 0.0,
   date '2000-01-01', time '00:00:00', timestamp '2000-01-01 00:00:00.000000',
   'FIX_TYPES_ZERO'),
  (2, -2147483648, -9223372036854775808, -12345678901234.5678,
   -12345678901234567890.1234567890, -1.5, -2.25,
   date '2024-02-29', time '23:59:59', timestamp '2024-02-29 23:59:59.123456',
   'FIX_TYPES_NEG_BOUND'),
  (3, 2147483647, 9223372036854775807, 12345678901234.5678,
   12345678901234567890.1234567890, 1.5, 2.25,
   date '2026-06-29', time '10:11:12', timestamp '2026-06-29 10:11:12.654321',
   'FIX_TYPES_POS_BOUND');

create table SYSDBA.DMDUL_FIX_NULLS (
  ID int primary key,
  A int,
  B varchar(20),
  C bigint,
  D varchar(20),
  MARKER varchar(64)
);

insert into SYSDBA.DMDUL_FIX_NULLS values
  (1, null, 'B_ONLY', null, 'D_ONLY', 'FIX_NULLS_1010'),
  (2, 22, null, 2222, null, 'FIX_NULLS_0101'),
  (3, null, null, null, null, 'FIX_NULLS_ALL_NULL'),
  (4, 44, 'B44', 4444, 'D44', 'FIX_NULLS_NONE');

create table SYSDBA.DMDUL_FIX_VLEN (
  ID int primary key,
  V varchar(4000),
  MARKER varchar(64)
);

insert into SYSDBA.DMDUL_FIX_VLEN values (1, 'A', 'FIX_VLEN_001');
insert into SYSDBA.DMDUL_FIX_VLEN values (2, rpad('B', 2, 'B'), 'FIX_VLEN_002');
insert into SYSDBA.DMDUL_FIX_VLEN values (10, rpad('C', 10, 'C'), 'FIX_VLEN_010');
insert into SYSDBA.DMDUL_FIX_VLEN values (127, rpad('D', 127, 'D'), 'FIX_VLEN_127');
insert into SYSDBA.DMDUL_FIX_VLEN values (128, rpad('E', 128, 'E'), 'FIX_VLEN_128');
insert into SYSDBA.DMDUL_FIX_VLEN values (255, rpad('F', 255, 'F'), 'FIX_VLEN_255');
insert into SYSDBA.DMDUL_FIX_VLEN values (256, rpad('G', 256, 'G'), 'FIX_VLEN_256');
insert into SYSDBA.DMDUL_FIX_VLEN values (1000, rpad('H', 1000, 'H'), 'FIX_VLEN_1000');

create table SYSDBA.DMDUL_FIX_MANY (
  ID int primary key,
  MARKER varchar(64),
  PAD varchar(3000)
);

begin
  for I in 1..160 loop
    insert into SYSDBA.DMDUL_FIX_MANY values (
      I,
      'FIX_MANY_ROW_' || lpad(I, 4, '0'),
      rpad(chr(65 + mod(I, 26)), 3000, chr(65 + mod(I, 26)))
    );
  end loop;
end;
/

create table SYSDBA.DMDUL_FIX_MOD (
  ID int primary key,
  MARKER varchar(64),
  V varchar(128)
);

insert into SYSDBA.DMDUL_FIX_MOD values (1, 'FIX_MOD_KEEP_1', 'KEEP');
insert into SYSDBA.DMDUL_FIX_MOD values (2, 'FIX_MOD_DELETE_2', 'DELETE_ME');
insert into SYSDBA.DMDUL_FIX_MOD values (3, 'FIX_MOD_UPDATE_3_BEFORE', 'BEFORE');
commit;

delete from SYSDBA.DMDUL_FIX_MOD where ID = 2;
update SYSDBA.DMDUL_FIX_MOD
   set MARKER = 'FIX_MOD_UPDATE_3_AFTER', V = 'AFTER_LONGER_VALUE'
 where ID = 3;
commit;

create table SYSDBA.DMDUL_FIX_UNDO (
  ID int primary key,
  MARKER varchar(64),
  V varchar(128)
);

insert into SYSDBA.DMDUL_FIX_UNDO values (1, 'FIX_UNDO_BASE_1', 'BASE_ONE');
insert into SYSDBA.DMDUL_FIX_UNDO values (2, 'FIX_UNDO_BASE_2', 'BASE_TWO');
commit;

-- Manual transaction fixture:
-- 1. Start a new session.
-- 2. Run the statements below but do not commit.
-- 3. Capture data and undo/rollback files while the transaction is open.
-- 4. Then test both commit and rollback copies.
--
-- update SYSDBA.DMDUL_FIX_UNDO
--    set MARKER = 'FIX_UNDO_UPDATE_1_OPEN', V = 'OPEN_UPDATE'
--  where ID = 1;
-- delete from SYSDBA.DMDUL_FIX_UNDO where ID = 2;
-- insert into SYSDBA.DMDUL_FIX_UNDO values (3, 'FIX_UNDO_INSERT_3_OPEN', 'OPEN_INSERT');

commit;
