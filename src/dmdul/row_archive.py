from __future__ import annotations

import struct
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, TextIO

from .decode import LobValue, decode_observed_row_values
from .metadata import ColumnMeta, StorageRoot, TableMeta
from .row import ObservedRow, ObservedRowHeader


MAGIC = b"DMDULROW\x01\r\n"
ROW_TAG = b"R"
LOB_TAG = b"L"
END_TAG = b"E"
NONE_I32 = -1
SQL_STRING_LITERAL_CHUNK_CHARS = 1000
SQL_BLOCK_TEXT_THRESHOLD_CHARS = 2000
SQL_BLOCK_TEXT_CHUNK_CHARS = 500
SQL_BLOCK_BLOB_HEX_CHUNK_CHARS = 1000


@dataclass(frozen=True)
class RowArchiveRecord:
    row_sequence: int
    file_no: int
    page_no: int
    row_offset: int
    row_data: bytes
    lob_payloads: dict[str, bytes]


@dataclass(frozen=True)
class RowImportReport:
    input: Path
    output_sql: Path
    table: str
    rows: int
    input_format: str

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "dm-import-data",
            "input": str(self.input),
            "input_format": self.input_format,
            "output_sql": str(self.output_sql),
            "table": self.table,
            "rows": self.rows,
        }


class RowArchiveWriter:
    def __init__(self, file: BinaryIO, *, table: TableMeta) -> None:
        self.file = file
        self.table = table
        self.file.write(MAGIC)
        _write_string(file, table.owner)
        _write_string(file, table.name)
        _write_string(file, _create_table_sql(table))
        _write_u16(file, len(table.columns))
        for column in table.columns:
            _write_string(file, column.name)
            _write_string(file, column.type_name)
            _write_i32(file, NONE_I32 if column.length is None else column.length)
            _write_i32(file, NONE_I32 if column.scale is None else column.scale)
            file.write(b"\x01" if column.nullable else b"\x00")

    def write_row(
        self,
        *,
        row_sequence: int,
        file_no: int,
        page_no: int,
        row_offset: int,
        row_data: bytes,
    ) -> None:
        self.file.write(ROW_TAG)
        self.file.write(struct.pack("<QHIHI", row_sequence, file_no, page_no, row_offset, len(row_data)))
        self.file.write(row_data)

    def write_lob(
        self,
        *,
        row_sequence: int,
        column_name: str,
        type_name: str,
        payload: bytes,
    ) -> None:
        self.file.write(LOB_TAG)
        self.file.write(struct.pack("<Q", row_sequence))
        _write_string(self.file, column_name)
        _write_string(self.file, type_name)
        self.file.write(struct.pack("<I", len(payload)))
        self.file.write(payload)

    def write_end(self, *, rows: int) -> None:
        self.file.write(END_TAG)
        self.file.write(struct.pack("<Q", rows))


class RowArchiveReader:
    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        magic = file.read(len(MAGIC))
        if magic != MAGIC:
            raise ValueError("not a bic-dmdul row archive")
        owner = _read_string(file)
        name = _read_string(file)
        self.create_table_sql = _read_string(file)
        columns = []
        for _ in range(_read_u16(file)):
            col_name = _read_string(file)
            type_name = _read_string(file)
            length = _none_if_i32(_read_i32(file))
            scale = _none_if_i32(_read_i32(file))
            nullable_raw = file.read(1)
            if len(nullable_raw) != 1:
                raise EOFError("truncated row archive column nullable flag")
            columns.append(
                ColumnMeta(
                    name=col_name,
                    type_name=type_name,
                    length=length,
                    scale=scale,
                    nullable=nullable_raw != b"\x00",
                )
            )
        self.table = TableMeta(
            owner=owner,
            name=name,
            columns=tuple(columns),
            storage=StorageRoot(group_id=0, file_no=0, root_page=0),
        )

    def records(self) -> Iterator[RowArchiveRecord]:
        lob_payloads: dict[tuple[int, str], bytes] = {}
        while True:
            tag = self.file.read(1)
            if not tag:
                raise EOFError("row archive ended before END record")
            if tag == END_TAG:
                trailer = self.file.read(8)
                if len(trailer) != 8:
                    raise EOFError("truncated row archive END record")
                return
            if tag == LOB_TAG:
                header = self.file.read(8)
                if len(header) != 8:
                    raise EOFError("truncated row archive LOB record")
                row_sequence = struct.unpack("<Q", header)[0]
                column_name = _read_string(self.file)
                _type_name = _read_string(self.file)
                length_raw = self.file.read(4)
                if len(length_raw) != 4:
                    raise EOFError("truncated row archive LOB length")
                payload_length = struct.unpack("<I", length_raw)[0]
                payload = self.file.read(payload_length)
                if len(payload) != payload_length:
                    raise EOFError("truncated row archive LOB payload")
                lob_payloads[(row_sequence, column_name)] = payload
                continue
            if tag != ROW_TAG:
                raise ValueError(f"unsupported row archive record tag: {tag!r}")
            header = self.file.read(20)
            if len(header) != 20:
                raise EOFError("truncated row archive row record")
            row_sequence, file_no, page_no, row_offset, row_length = struct.unpack(
                "<QHIHI",
                header,
            )
            row_data = self.file.read(row_length)
            if len(row_data) != row_length:
                raise EOFError("truncated row archive row payload")
            yield RowArchiveRecord(
                row_sequence=row_sequence,
                file_no=file_no,
                page_no=page_no,
                row_offset=row_offset,
                row_data=row_data,
                lob_payloads={
                    column.name: lob_payloads.pop((row_sequence, column.name))
                    for column in self.table.columns
                    if (row_sequence, column.name) in lob_payloads
                },
            )


def import_row_archive_to_sql(
    *,
    input_path: Path,
    output_sql: Path,
    table_name: str | None = None,
    include_create_table: bool = True,
) -> RowImportReport:
    output_sql.parent.mkdir(parents=True, exist_ok=True)
    with output_sql.open("w", encoding="utf-8") as output_file:
        target_table, rows = _write_row_archive_sql(
            input_path=input_path,
            output_file=output_file,
            table_name=table_name,
            include_create_table=include_create_table,
        )
        output_file.write("COMMIT;\n")
    return RowImportReport(
        input=input_path,
        output_sql=output_sql,
        table=target_table,
        rows=rows,
        input_format="row",
    )


def import_data_to_sql(
    *,
    input_path: Path,
    output_sql: Path,
    input_format: str = "auto",
    table_name: str | None = None,
    include_create_table: bool = True,
    delimiter: str | None = None,
) -> RowImportReport:
    resolved_format = _resolve_input_format(input_path, input_format)
    if resolved_format == "parts":
        return import_parts_manifest_to_sql(
            input_path=input_path,
            output_sql=output_sql,
            table_name=table_name,
            include_create_table=include_create_table,
            delimiter=delimiter,
        )
    if resolved_format == "row":
        return import_row_archive_to_sql(
            input_path=input_path,
            output_sql=output_sql,
            table_name=table_name,
            include_create_table=include_create_table,
        )
    if resolved_format == "dul":
        return import_dul_text_to_sql(
            input_path=input_path,
            output_sql=output_sql,
            table_name=table_name,
            include_create_table=include_create_table,
            delimiter=delimiter,
        )
    raise ValueError(f"unsupported import input format: {input_format}")


def import_dul_text_to_sql(
    *,
    input_path: Path,
    output_sql: Path,
    table_name: str | None = None,
    include_create_table: bool = True,
    delimiter: str | None = None,
) -> RowImportReport:
    output_sql.parent.mkdir(parents=True, exist_ok=True)
    with output_sql.open("w", encoding="utf-8") as output_file:
        target_table, rows = _write_dul_text_sql(
            input_path=input_path,
            output_file=output_file,
            table_name=table_name,
            include_create_table=include_create_table,
            delimiter=delimiter,
        )
        output_file.write("COMMIT;\n")
    return RowImportReport(
        input=input_path,
        output_sql=output_sql,
        table=target_table,
        rows=rows,
        input_format="dul",
    )


def import_parts_manifest_to_sql(
    *,
    input_path: Path,
    output_sql: Path,
    table_name: str | None = None,
    include_create_table: bool = True,
    delimiter: str | None = None,
) -> RowImportReport:
    manifest = _read_parts_manifest(input_path)
    target_table = table_name or manifest["table"]
    rows = 0
    output_sql.parent.mkdir(parents=True, exist_ok=True)
    with output_sql.open("w", encoding="utf-8") as output_file:
        if include_create_table:
            output_file.write(
                _create_sql_for_target(
                    str(manifest["create_sql"]),
                    target_table=target_table,
                )
                + ";\n\n"
            )
        for part_path in manifest["parts"]:
            if manifest["format"] == "row":
                _part_table, part_rows = _write_row_archive_sql(
                    input_path=part_path,
                    output_file=output_file,
                    table_name=target_table,
                    include_create_table=False,
                )
            else:
                _part_table, part_rows = _write_dul_text_sql(
                    input_path=part_path,
                    output_file=output_file,
                    table_name=target_table,
                    include_create_table=False,
                    delimiter=delimiter or manifest.get("delimiter"),
                )
            rows += part_rows
        output_file.write("COMMIT;\n")
    return RowImportReport(
        input=input_path,
        output_sql=output_sql,
        table=target_table,
        rows=rows,
        input_format="parts",
    )


def _write_row_archive_sql(
    *,
    input_path: Path,
    output_file: TextIO,
    table_name: str | None,
    include_create_table: bool,
) -> tuple[str, int]:
    rows = 0
    with input_path.open("rb") as input_file:
        reader = RowArchiveReader(input_file)
        target_table = table_name or reader.table.qualified_name
        if include_create_table:
            output_file.write(
                _create_sql_for_target(
                    reader.create_table_sql,
                    target_table=target_table,
                )
                + ";\n\n"
            )
        column_names = [column.name for column in reader.table.columns]
        for record in reader.records():
            row = ObservedRow(
                page_offset=record.row_offset,
                data=record.row_data,
                header=ObservedRowHeader.from_bytes(record.row_data),
            )
            values = decode_observed_row_values(
                row,
                reader.table.columns,
                external_lobs=True,
            )
            sql_values: list[str] = []
            block_texts: dict[int, tuple[ColumnMeta, str]] = {}
            block_blobs: dict[int, bytes] = {}
            for index, (column, value) in enumerate(zip(reader.table.columns, values)):
                blob_value = _block_blob_value(
                    column=column,
                    value=value,
                    lob_payload=record.lob_payloads.get(column.name),
                )
                if blob_value is not None and len(blob_value.hex()) > SQL_BLOCK_TEXT_THRESHOLD_CHARS:
                    sql_values.append(_block_var_name(index))
                    block_blobs[index] = blob_value
                    continue
                text_value = _block_text_value(
                    column=column,
                    value=value,
                    lob_payload=record.lob_payloads.get(column.name),
                )
                if text_value is not None and len(text_value) > SQL_BLOCK_TEXT_THRESHOLD_CHARS:
                    sql_values.append(_block_var_name(index))
                    block_texts[index] = (column, text_value)
                else:
                    sql_values.append(
                        _sql_value(
                            column=column,
                            value=value,
                            lob_payload=record.lob_payloads.get(column.name),
                        )
                    )
            _write_insert(
                output_file,
                target_table=target_table,
                column_names=column_names,
                sql_values=sql_values,
                block_texts=block_texts,
                block_blobs=block_blobs,
            )
            rows += 1
    return target_table, rows


def _write_dul_text_sql(
    *,
    input_path: Path,
    output_file: TextIO,
    table_name: str | None,
    include_create_table: bool,
    delimiter: str | None,
) -> tuple[str, int]:
    with input_path.open("r", encoding="utf-8", newline="") as input_file:
        create_sql, data_lines = _read_dul_sections(input_file)
    table, columns = _parse_create_table_header(create_sql)
    target_table = table_name or table
    data_delimiter = delimiter or _detect_delimiter(data_lines[0] if data_lines else "")
    rows = 0
    if include_create_table:
        output_file.write(
            _create_sql_for_target(
                create_sql,
                target_table=target_table,
            )
            + ";\n\n"
        )
    if data_lines:
        reader = csv.reader(data_lines, delimiter=data_delimiter)
        csv_columns = next(reader, [])
        column_names = list(csv_columns)
        column_types = {
            column_name: type_name
            for column_name, type_name in columns
        }
        for csv_row in reader:
            if not csv_row:
                continue
            values: list[str] = []
            block_texts: dict[int, tuple[ColumnMeta, str]] = {}
            block_blobs: dict[int, bytes] = {}
            for index, (column_name, value) in enumerate(zip(csv_columns, csv_row)):
                type_name = column_types.get(column_name, "")
                blob_value = _dul_block_blob_value(
                    value=value,
                    type_name=type_name,
                    input_dir=input_path.parent,
                )
                if blob_value is not None and len(blob_value.hex()) > SQL_BLOCK_TEXT_THRESHOLD_CHARS:
                    values.append(_block_var_name(index))
                    block_blobs[index] = blob_value
                    continue
                text_value = _dul_block_text_value(
                    value=value,
                    type_name=type_name,
                    input_dir=input_path.parent,
                )
                if text_value is not None and len(text_value) > SQL_BLOCK_TEXT_THRESHOLD_CHARS:
                    values.append(_block_var_name(index))
                    block_texts[index] = (
                        ColumnMeta(name=column_name, type_name=_normalized_type_name(type_name)),
                        text_value,
                    )
                else:
                    values.append(
                        _dul_sql_value(
                            value=value,
                            type_name=type_name,
                            input_dir=input_path.parent,
                        )
                    )
            _write_insert(
                output_file,
                target_table=target_table,
                column_names=column_names,
                sql_values=values,
                block_texts=block_texts,
                block_blobs=block_blobs,
            )
            rows += 1
    return target_table, rows


def _write_insert(
    output_file: TextIO,
    *,
    target_table: str,
    column_names: list[str],
    sql_values: list[str],
    block_texts: dict[int, tuple[ColumnMeta, str]],
    block_blobs: dict[int, bytes],
) -> None:
    column_list = ", ".join(column_names)
    value_list = ", ".join(sql_values)
    if not block_texts and not block_blobs:
        output_file.write(f"INSERT INTO {target_table} ({column_list}) VALUES ({value_list});\n")
        return

    output_file.write("DECLARE\n")
    for index, (column, _text) in block_texts.items():
        output_file.write(f"  {_block_var_name(index)} {_block_var_type(column)};\n")
    for index in block_blobs:
        output_file.write(f"  {_block_var_name(index)} BLOB;\n")
    output_file.write("BEGIN\n")
    for index, (_column, text) in block_texts.items():
        variable = _block_var_name(index)
        chunks = [
            text[start : start + SQL_BLOCK_TEXT_CHUNK_CHARS]
            for start in range(0, len(text), SQL_BLOCK_TEXT_CHUNK_CHARS)
        ]
        first, *rest = chunks
        output_file.write(f"  {variable} := {_single_sql_string_literal(first)};\n")
        for chunk in rest:
            output_file.write(f"  {variable} := {variable} || {_single_sql_string_literal(chunk)};\n")
    for index, payload in block_blobs.items():
        variable = _block_var_name(index)
        output_file.write(f"  DBMS_LOB.CREATETEMPORARY({variable}, TRUE);\n")
        hex_value = payload.hex()
        for start in range(0, len(hex_value), SQL_BLOCK_BLOB_HEX_CHUNK_CHARS):
            chunk = hex_value[start : start + SQL_BLOCK_BLOB_HEX_CHUNK_CHARS]
            output_file.write(
                f"  DBMS_LOB.WRITEAPPEND({variable}, {len(chunk) // 2}, HEXTORAW('{chunk}'));\n"
            )
    output_file.write(f"  INSERT INTO {target_table} ({column_list}) VALUES ({value_list});\n")
    for index in block_blobs:
        output_file.write(f"  DBMS_LOB.FREETEMPORARY({_block_var_name(index)});\n")
    output_file.write("END;\n/\n")


def _block_var_name(index: int) -> str:
    return f"V_C{index + 1}"


def _block_var_type(column: ColumnMeta) -> str:
    return "VARCHAR(32767)"


def _block_text_value(
    *,
    column: ColumnMeta,
    value: object,
    lob_payload: bytes | None,
) -> str | None:
    if value is None:
        return None
    type_name = column.type_name.upper()
    if type_name in {"BYTE", "BINARY", "VARBINARY", "BLOB"} or _is_numeric_type(type_name):
        return None
    if isinstance(value, LobValue):
        if lob_payload is not None:
            return lob_payload.decode("utf-8")
        if value.inline_payload is not None:
            return value.text or ""
        return None
    return str(value)


def _block_blob_value(
    *,
    column: ColumnMeta,
    value: object,
    lob_payload: bytes | None,
) -> bytes | None:
    if column.type_name.upper() != "BLOB" or value is None:
        return None
    if isinstance(value, LobValue):
        if lob_payload is not None:
            return lob_payload
        return value.inline_payload
    return None


def _dul_block_text_value(*, value: str, type_name: str, input_dir: Path) -> str | None:
    normalized_type = _normalized_type_name(type_name)
    if value == "" or normalized_type in {"BYTE", "BINARY", "VARBINARY", "BLOB"} or _is_numeric_type(normalized_type):
        return None
    if value.startswith("@LOB:"):
        payload_path = input_dir / value[len("@LOB:") :]
        if payload_path.suffix.lower() in {".blob", ".hex"}:
            return None
        return payload_path.read_text(encoding="utf-8")
    return value


def _dul_block_blob_value(*, value: str, type_name: str, input_dir: Path) -> bytes | None:
    normalized_type = _normalized_type_name(type_name)
    if normalized_type != "BLOB" or value == "":
        return None
    if value.startswith("@LOB:"):
        return (input_dir / value[len("@LOB:") :]).read_bytes()
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def _normalized_type_name(type_name: str) -> str:
    return type_name.upper().split("(", 1)[0].strip()


def _is_numeric_type(type_name: str) -> bool:
    return type_name in {
        "TINYINT",
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "REAL",
        "FLOAT",
        "DOUBLE",
        "NUMBER",
        "NUMERIC",
        "DEC",
        "DECIMAL",
    }


def _sql_value(
    *,
    column: ColumnMeta,
    value: object,
    lob_payload: bytes | None,
) -> str:
    if value is None:
        return "NULL"
    type_name = column.type_name.upper()
    if isinstance(value, LobValue):
        if lob_payload is not None:
            if type_name == "BLOB":
                return f"HEXTORAW('{lob_payload.hex()}')"
            return _sql_string_literal(lob_payload.decode("utf-8"))
        if value.inline_payload is not None:
            if type_name == "BLOB":
                return f"HEXTORAW('{value.inline_payload.hex()}')"
            return _sql_string_literal(value.text or "")
        raise ValueError(f"LOB payload is missing for column={column.name}")
    if type_name in {"BYTE", "BINARY", "VARBINARY"}:
        return f"HEXTORAW('{value}')"
    if type_name in {
        "TINYINT",
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "REAL",
        "FLOAT",
        "DOUBLE",
        "NUMBER",
        "NUMERIC",
        "DEC",
        "DECIMAL",
    }:
        return str(value)
    return _sql_string_literal(str(value))


def _dul_sql_value(*, value: str, type_name: str, input_dir: Path) -> str:
    normalized_type = type_name.upper().split("(", 1)[0]
    if value == "":
        return "NULL"
    if value.startswith("@LOB:"):
        payload_path = input_dir / value[len("@LOB:") :]
        if payload_path.suffix.lower() in {".blob", ".hex"} or normalized_type == "BLOB":
            return f"HEXTORAW('{payload_path.read_bytes().hex()}')"
        return _sql_string_literal(payload_path.read_text(encoding="utf-8"))
    if normalized_type in {"BYTE", "BINARY", "VARBINARY"}:
        return f"HEXTORAW('{value}')"
    if normalized_type in {
        "TINYINT",
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
        "REAL",
        "FLOAT",
        "DOUBLE",
        "NUMBER",
        "NUMERIC",
        "DEC",
        "DECIMAL",
    }:
        return value
    return _sql_string_literal(value)


def _create_sql_for_target(create_sql: str, *, target_table: str) -> str:
    sql = create_sql.strip().rstrip(";")
    return re.sub(
        r"(?is)\A(\s*CREATE\s+TABLE\s+)([^\s(]+)",
        lambda match: match.group(1) + target_table,
        sql,
        count=1,
    )


def _read_dul_sections(file: TextIO) -> tuple[str, list[str]]:
    create_lines: list[str] = []
    data_lines: list[str] = []
    in_data = False
    for line in file:
        if line.strip() == "-- DATA":
            in_data = True
            continue
        if in_data:
            data_lines.append(line)
        else:
            create_lines.append(line)
    create_sql = "".join(create_lines).strip()
    if not create_sql:
        raise ValueError("DUL file does not contain a CREATE TABLE header")
    return create_sql, data_lines


def _parse_create_table_header(create_sql: str) -> tuple[str, list[tuple[str, str]]]:
    match = re.search(
        r"CREATE\s+TABLE\s+([^\s(]+)\s*\((.*)\)\s*;?\s*$",
        create_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("DUL CREATE TABLE header is not supported")
    table_name = match.group(1)
    column_body = match.group(2)
    columns: list[tuple[str, str]] = []
    for item in _split_ddl_columns(column_body):
        stripped = item.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"unsupported DUL column definition: {stripped}")
        columns.append((parts[0].strip('"'), parts[1].strip().rstrip(",")))
    return table_name, columns


def _split_ddl_columns(value: str) -> list[str]:
    columns: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            columns.append(value[start:index])
            start = index + 1
    columns.append(value[start:])
    return columns


def _detect_delimiter(header_line: str) -> str:
    candidates = [",", "|", "~"]
    return max(candidates, key=lambda candidate: header_line.count(candidate))


def _resolve_input_format(input_path: Path, input_format: str) -> str:
    if input_format != "auto":
        return input_format
    with input_path.open("rb") as file:
        prefix = file.read(len(MAGIC))
    if prefix == MAGIC:
        return "row"
    with input_path.open("r", encoding="utf-8", errors="ignore") as file:
        first_line = file.readline().strip()
    if first_line == "DMDUL-PARTS 1":
        return "parts"
    return "dul"


def _read_parts_manifest(input_path: Path) -> dict[str, object]:
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "DMDUL-PARTS 1":
        raise ValueError("not a bic-dmdul parts manifest")
    payload: dict[str, object] = {
        "format": "dul",
        "delimiter": "|",
        "table": "",
        "parts": [],
        "create_sql": "",
    }
    part_dir = input_path.parent
    create_lines: list[str] = []
    in_create_sql = False
    parts: list[Path] = []
    for line in lines[1:]:
        if line == "CREATE_SQL_BEGIN":
            in_create_sql = True
            continue
        if line == "CREATE_SQL_END":
            in_create_sql = False
            payload["create_sql"] = "\n".join(create_lines)
            continue
        if in_create_sql:
            create_lines.append(line)
            continue
        if line.startswith("FORMAT "):
            payload["format"] = line.split(" ", 1)[1].strip()
        elif line.startswith("TABLE "):
            payload["table"] = line.split(" ", 1)[1].strip()
        elif line.startswith("DELIMITER "):
            payload["delimiter"] = line.split(" ", 1)[1]
        elif line.startswith("PART_DIR "):
            value = line.split(" ", 1)[1].strip()
            candidate = Path(value)
            part_dir = candidate if candidate.is_absolute() else input_path.parent / candidate
        elif line.startswith("PART "):
            fields = line.split()
            if len(fields) < 3:
                raise ValueError(f"invalid parts manifest entry: {line}")
            candidate = Path(fields[2])
            parts.append(candidate if candidate.is_absolute() else part_dir / candidate)
    payload["parts"] = parts
    if payload["format"] not in {"dul", "row"}:
        raise ValueError(f"unsupported parts manifest format: {payload['format']}")
    if not payload["table"]:
        raise ValueError("parts manifest does not contain TABLE")
    if not payload["create_sql"]:
        raise ValueError("parts manifest does not contain CREATE SQL")
    return payload


def _create_table_sql(table: TableMeta) -> str:
    owner_prefix = f"{table.owner}." if table.owner else ""
    column_lines = [f"  {column.name} {_ddl_type(column)}" for column in table.columns]
    columns_sql = ",\n".join(column_lines)
    return f"CREATE TABLE {owner_prefix}{table.name} (\n{columns_sql}\n)"


def _ddl_type(column: ColumnMeta) -> str:
    type_name = column.type_name.upper()
    if (
        column.length is not None
        and type_name in {"DEC", "DECIMAL", "NUMBER", "NUMERIC"}
    ):
        if column.scale is not None and column.scale > 0:
            return f"{type_name}({column.length},{column.scale})"
        return f"{type_name}({column.length})"
    if (
        column.scale is not None
        and 0 < column.scale <= 6
        and type_name
        in {
            "TIME",
            "TIMESTAMP",
            "DATETIME",
            "TIME WITH TIME ZONE",
            "TIMESTAMP WITH TIME ZONE",
            "DATETIME WITH TIME ZONE",
            "TIMESTAMP WITH LOCAL TIME ZONE",
        }
    ):
        if type_name == "TIME WITH TIME ZONE":
            return f"TIME({column.scale}) WITH TIME ZONE"
        if type_name == "TIMESTAMP WITH TIME ZONE":
            return f"TIMESTAMP({column.scale}) WITH TIME ZONE"
        if type_name == "DATETIME WITH TIME ZONE":
            return f"DATETIME({column.scale}) WITH TIME ZONE"
        if type_name == "TIMESTAMP WITH LOCAL TIME ZONE":
            return f"TIMESTAMP({column.scale}) WITH LOCAL TIME ZONE"
        return f"{type_name}({column.scale})"
    if column.length is not None and type_name in {
        "CHAR",
        "VARCHAR",
        "VARCHAR2",
        "BINARY",
        "VARBINARY",
    }:
        return f"{type_name}({column.length})"
    return type_name


def _write_string(file: BinaryIO, value: str) -> None:
    raw = value.encode("utf-8")
    _write_u16(file, len(raw))
    file.write(raw)


def _read_string(file: BinaryIO) -> str:
    length = _read_u16(file)
    raw = file.read(length)
    if len(raw) != length:
        raise EOFError("truncated row archive string")
    return raw.decode("utf-8")


def _write_u16(file: BinaryIO, value: int) -> None:
    file.write(struct.pack("<H", value))


def _read_u16(file: BinaryIO) -> int:
    raw = file.read(2)
    if len(raw) != 2:
        raise EOFError("truncated row archive u16")
    return struct.unpack("<H", raw)[0]


def _write_i32(file: BinaryIO, value: int) -> None:
    file.write(struct.pack("<i", value))


def _read_i32(file: BinaryIO) -> int:
    raw = file.read(4)
    if len(raw) != 4:
        raise EOFError("truncated row archive i32")
    return struct.unpack("<i", raw)[0]


def _none_if_i32(value: int) -> int | None:
    return None if value == NONE_I32 else value


def _safe_lob_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in value)


def _sql_string_literal(value: str) -> str:
    if len(value) <= SQL_STRING_LITERAL_CHUNK_CHARS:
        return _single_sql_string_literal(value)
    chunks = [
        _single_sql_string_literal(value[index : index + SQL_STRING_LITERAL_CHUNK_CHARS])
        for index in range(0, len(value), SQL_STRING_LITERAL_CHUNK_CHARS)
    ]
    return "(" + " || ".join(chunks) + ")"


def _single_sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
