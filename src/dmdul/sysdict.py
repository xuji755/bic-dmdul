from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .extract import _build_root_page_plan
from .metadata import StorageRoot, TableMeta
from .page import ObservedPageHeader
from .row import decode_observed_var_length, iter_observed_rows_by_slots, scan_observed_row_chain
from .storage import DataFile


KNOWN_DM_TYPE_NAMES = frozenset(
    {
        "BIGINT",
        "TIME WITH TIME ZONE",
        "TIMESTAMP WITH LOCAL TIME ZONE",
        "TIMESTAMP WITH TIME ZONE",
        "DATETIME WITH TIME ZONE",
        "INTERVAL YEAR TO MONTH",
        "INTERVAL DAY TO SECOND",
        "INDTAB",
        "ROWID",
        "BYTE",
        "BINARY",
        "BLOB",
        "CHAR",
        "CLOB",
        "DATE",
        "DATETIME",
        "DEC",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "INT",
        "INTEGER",
        "NUMBER",
        "NUMERIC",
        "REAL",
        "SMALLINT",
        "TEXT",
        "TIME",
        "TIMESTAMP",
        "TINYINT",
        "VARBINARY",
        "VARCHAR",
        "VARCHAR2",
    }
)

SCAN_PROGRESS_BYTES = 16 * 1024 * 1024

SYSTEM_FILE_HEADER_ROOT_OFFSETS = {
    0: 0x80,  # SYS.SYSOBJECTS root page, observed in DM7 and DM8 SYSTEM.DBF page 0.
    1: 0x7C,  # SYS.SYSINDEXES root page, observed in DM7 and DM8 SYSTEM.DBF page 0.
}


@dataclass(frozen=True)
class SysObjectCandidate:
    name: str
    offset: int
    page_no: int
    page_offset: int
    score: int
    object_ids: tuple[int, ...]
    likely_object_ids: tuple[int, ...]
    preferred_object_ids: tuple[int, ...]
    has_schobj: bool
    has_utab: bool


@dataclass(frozen=True)
class SysColumnCandidate:
    object_id: int
    offset: int
    page_no: int
    page_offset: int
    score: int
    column_id: int | None
    length: int | None
    scale: int | None
    nullable: str | None
    name: str
    type_name: str
    name_offset: int
    type_offset: int


@dataclass(frozen=True)
class SysIndexCandidate:
    index_id: int
    offset: int
    page_no: int
    page_offset: int
    score: int
    is_unique: str | None
    group_id: int | None
    root_file: int | None
    root_page: int | None
    type_name: str | None
    xtype: int | None
    flag: int | None
    keynum: int | None = None
    keyinfo_hex: str | None = None


@dataclass(frozen=True)
class StorageRootCandidate:
    storage_id: int
    root_file: int
    root_page: int
    page_kind_raw: int
    score: int
    source: str


@dataclass(frozen=True)
class SysObjectIndexChildCandidate:
    parent_object_id: int
    index_id: int
    name: str
    offset: int
    page_no: int
    page_offset: int
    score: int
    type_name: str | None
    name_offset: int
    index_id_offset: int | None


@dataclass(frozen=True)
class SysObjectRowCandidate:
    name: str
    object_id: int | None
    schema_id: int | None
    parent_id: int | None
    type_name: str
    subtype_name: str
    offset: int
    page_no: int
    page_offset: int
    score: int
    source: str


def find_sysobject_candidates(
    system_file: Path,
    object_name: str,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
) -> list[SysObjectCandidate]:
    """Find SYSOBJECTS-like records for an object name in SYSTEM.DBF.

    This is a bootstrap heuristic. It searches raw bytes for the object name,
    checks for nearby SYSOBJECTS type markers, and extracts plausible little
    endian object-id values from the surrounding context.
    """

    name = object_name.upper()
    marker = name.encode("utf-8")
    candidates: list[SysObjectCandidate] = []
    overlap = 512
    previous = b""
    offset = 0
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            search_from = 0
            while True:
                index = window.find(marker, search_from)
                if index < 0:
                    break
                absolute = offset - len(previous) + index
                context_start = max(0, index - 160)
                context_end = min(len(window), index + len(marker) + 220)
                context = window[context_start:context_end]
                candidates.append(
                    _candidate_from_context(
                        name=name,
                        absolute=absolute,
                        page_size=page_size,
                        context=context,
                    )
                )
                search_from = index + 1
            previous = window[-overlap:]
            offset += len(chunk)
    return sorted(candidates, key=lambda item: (-item.score, item.offset))


def find_syscolumn_candidates(
    system_file: Path,
    object_id: int,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
) -> list[SysColumnCandidate]:
    """Find SYSCOLUMNS-like records for an object id in SYSTEM.DBF.

    The current DM8 samples place the owning object id, column number, declared
    length, prefixed column name, and prefixed type name close together. This
    scanner deliberately returns candidates with scores because the complete
    SYSCOLUMNS row layout is still being calibrated from raw files.
    """

    marker = object_id.to_bytes(4, "little", signed=False)
    candidates: list[SysColumnCandidate] = []
    overlap = 512
    previous = b""
    offset = 0
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            search_from = 0
            while True:
                index = window.find(marker, search_from)
                if index < 0:
                    break
                absolute = offset - len(previous) + index
                row_candidate = _syscolumn_candidate_from_row(
                    object_id=object_id,
                    absolute=absolute,
                    page_size=page_size,
                    window=window,
                    local_object_offset=index,
                )
                if row_candidate is not None:
                    candidates.append(row_candidate)
                else:
                    context_start = max(0, index - 32)
                    context_end = min(len(window), index + 180)
                    context = window[context_start:context_end]
                    local_object_offset = index - context_start
                    candidates.extend(
                        _syscolumn_candidates_from_context(
                            object_id=object_id,
                            absolute=absolute,
                            page_size=page_size,
                            context=context,
                            local_object_offset=local_object_offset,
                        )
                    )
                search_from = index + 1
            previous = window[-overlap:]
            offset += len(chunk)
    return _dedupe_syscolumn_candidates(candidates)


def _syscolumn_candidate_from_row(
    *,
    object_id: int,
    absolute: int,
    page_size: int,
    window: bytes,
    local_object_offset: int,
) -> SysColumnCandidate | None:
    """Decode the calibrated clean-row subset of SYS.SYSCOLUMNS.

    Online/offline calibration currently shows the owning object id at row
    relative offset 5 in clean SYSCOLUMNS rows:

    len/status, 3 metadata bytes, ID, COLID, LENGTH$, SCALE, NULLABLE$,
    4 nullable/control bytes, NAME, TYPE$, row-tail/control.
    """

    local_row_start = local_object_offset - 5
    if local_row_start < 0 or local_row_start + 2 > len(window):
        return None
    raw_len_flags = int.from_bytes(window[local_row_start : local_row_start + 2], "big")
    row_length = raw_len_flags & 0x7FFF
    if raw_len_flags & 0x8000:
        return None
    if row_length < 32 or row_length > 4096:
        return None
    local_row_end = local_row_start + row_length
    if local_row_end > len(window):
        return None
    row = window[local_row_start:local_row_end]
    if row[5:9] != object_id.to_bytes(4, "little", signed=False):
        return None
    column_id = int.from_bytes(row[9:11], "little", signed=False)
    length = int.from_bytes(row[11:15], "little", signed=False)
    scale = int.from_bytes(row[15:17], "little", signed=False)
    nullable = row[17:18]
    if column_id > 4096 or length > 0x7FFFFFFF or scale > 10000:
        return None
    if nullable not in {b"N", b"Y"}:
        return None
    variable_offset = 22
    try:
        name_decoded = decode_observed_var_length(row[variable_offset:])
    except ValueError:
        return None
    name_start = variable_offset + name_decoded.encoded_size
    name_end = name_start + name_decoded.length
    if name_end > len(row):
        return None
    try:
        name = row[name_start:name_end].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _is_printable_ascii_identifier(name.encode("ascii")):
        return None
    type_offset = name_end
    try:
        type_decoded = decode_observed_var_length(row[type_offset:])
    except ValueError:
        return None
    type_start = type_offset + type_decoded.encoded_size
    type_end = type_start + type_decoded.length
    if type_end > len(row):
        return None
    try:
        type_name = row[type_start:type_end].decode("ascii").upper()
    except UnicodeDecodeError:
        return None
    if not _is_known_dm_type_name(type_name):
        return None
    return SysColumnCandidate(
        object_id=object_id,
        offset=absolute,
        page_no=absolute // page_size,
        page_offset=absolute % page_size,
        score=140,
        column_id=column_id,
        length=length,
        scale=scale,
        nullable=nullable.decode("ascii"),
        name=name,
        type_name=type_name,
        name_offset=name_start,
        type_offset=type_start,
    )



def dump_syscolumn_rows(
    system_file: Path,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
    progress: Callable[[str], None] | None = None,
) -> list[SysColumnCandidate]:
    """Scan SYSTEM.DBF once for calibrated SYS.SYSCOLUMNS clean rows."""

    candidates: list[SysColumnCandidate] = []
    file_size = system_file.stat().st_size
    overlap = 4096
    previous = b""
    offset = 0
    _emit_scan_progress(progress, "SYSCOLUMNS", offset, file_size, len(candidates))
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            base_absolute = offset - len(previous)
            scan_limit = max(0, len(window) - 32)
            for local_row_start in range(scan_limit):
                candidate = _syscolumn_candidate_at_row_start(
                    absolute_row_start=base_absolute + local_row_start,
                    page_size=page_size,
                    window=window,
                    local_row_start=local_row_start,
                )
                if candidate is not None:
                    candidates.append(candidate)
            previous = window[-overlap:]
            offset += len(chunk)
            _emit_scan_progress(progress, "SYSCOLUMNS", offset, file_size, len(candidates))
    rows = _dedupe_syscolumn_candidates(candidates)
    _emit_scan_progress(progress, "SYSCOLUMNS", file_size, file_size, len(rows), done=True)
    return rows


def _syscolumn_candidate_at_row_start(
    *,
    absolute_row_start: int,
    page_size: int,
    window: bytes,
    local_row_start: int,
) -> SysColumnCandidate | None:
    if local_row_start + 22 > len(window):
        return None
    raw_len_flags = int.from_bytes(window[local_row_start : local_row_start + 2], "big")
    row_length = raw_len_flags & 0x7FFF
    if raw_len_flags & 0x8000:
        return None
    if row_length < 32 or row_length > 4096:
        return None
    local_row_end = local_row_start + row_length
    if local_row_end > len(window):
        return None
    row = window[local_row_start:local_row_end]
    object_id = int.from_bytes(row[5:9], "little", signed=False)
    if object_id < 1 or object_id > 10_000_000:
        return None
    return _syscolumn_candidate_from_row(
        object_id=object_id,
        absolute=absolute_row_start + 5,
        page_size=page_size,
        window=window,
        local_object_offset=local_row_start + 5,
    )


def find_sysindex_candidates(
    system_file: Path,
    index_id: int,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
) -> list[SysIndexCandidate]:
    """Find SYSINDEXES-like records for an index/storage object id.

    Controlled DM8 samples show a compact row fragment:

    `id u32le, isunique char, groupid u16le, rootfile u16le, rootpage u32le,
    type char(2), xtype u32le, flag u32le`.
    """

    marker = index_id.to_bytes(4, "little", signed=False)
    candidates: list[SysIndexCandidate] = []
    overlap = 128
    previous = b""
    offset = 0
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            search_from = 0
            while True:
                index = window.find(marker, search_from)
                if index < 0:
                    break
                absolute = offset - len(previous) + index
                context = window[index : min(len(window), index + 64)]
                candidate = _sysindex_candidate_from_context(
                    index_id=index_id,
                    absolute=absolute,
                    page_size=page_size,
                    context=context,
                )
                if candidate is not None:
                    candidates.append(candidate)
                search_from = index + 1
            previous = window[-overlap:]
            offset += len(chunk)
    return _dedupe_sysindex_candidates(candidates)





def dump_syscolumn_rows_from_storage(
    system_file: Path,
    *,
    root_file: int,
    root_page: int,
    storage_id: int,
    group_id: int = 0,
    page_size: int = 8192,
    failure_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[SysColumnCandidate]:
    """Read SYS.SYSCOLUMNS rows from its storage root and page slots.

    This is the dictionary-download path: locate data pages from the storage
    root, verify page headers, take rows from the page slot directory, and only
    then decode SYSCOLUMNS fields. Rows that cannot be decoded are preserved in
    the failure file instead of being silently filtered out.
    """

    data_file = DataFile(system_file, page_size=page_size)
    table = TableMeta(
        owner="SYS",
        name="SYSCOLUMNS",
        columns=(),
        storage=StorageRoot(
            group_id=group_id,
            file_no=root_file,
            root_page=root_page,
            storage_id=storage_id,
        ),
    )
    plan = _build_root_page_plan(table=table, data_files={root_file: data_file})
    _emit_storage_progress(
        progress,
        "SYSCOLUMNS storage plan: "
        f"root_file={root_file} root_page={root_page} storage_id={storage_id} "
        f"pages={len(plan.pages)} diagnostics={len(plan.diagnostics)}",
    )
    candidates: list[SysColumnCandidate] = []
    failures: list[dict[str, Any]] = []
    pages_done = 0
    rows_seen = 0
    for page_ref in plan.pages:
        pages_done += 1
        try:
            page = data_file.read_page(page_ref.page_no)
            header = ObservedPageHeader.from_page(page)
        except Exception as exc:  # pragma: no cover - defensive evidence path
            failures.append(
                _syscolumn_failure(
                    reason=f"page-read-failed:{exc}",
                    file_no=page_ref.file_no,
                    page_no=page_ref.page_no,
                )
            )
            continue
        if header.page_kind_raw != 0x14:
            failures.append(
                _syscolumn_failure(
                    reason=f"non-data-page-kind:0x{header.page_kind_raw:x}",
                    file_no=page_ref.file_no,
                    page_no=page_ref.page_no,
                    raw=page[:128],
                )
            )
            continue
        if header.storage_id_candidate != storage_id:
            failures.append(
                _syscolumn_failure(
                    reason=f"storage-id-mismatch:{header.storage_id_candidate}",
                    file_no=page_ref.file_no,
                    page_no=page_ref.page_no,
                    raw=page[:128],
                )
            )
            continue
        physical_rows = scan_observed_row_chain(page)
        slot_rows = iter_observed_rows_by_slots(page)
        if header.observed_row_count and physical_rows and not slot_rows:
            failures.append(
                _syscolumn_failure(
                    reason="slot-directory-not-decoded",
                    file_no=page_ref.file_no,
                    page_no=page_ref.page_no,
                    raw=page[:256],
                )
            )
        rows = slot_rows
        rows_seen += len(rows)
        for row in rows:
            if row.is_deleted:
                continue
            if len(row.data) < 9:
                failures.append(
                    _syscolumn_failure(
                        reason="row-too-short-for-object-id",
                        file_no=page_ref.file_no,
                        page_no=page_ref.page_no,
                        page_offset=row.page_offset,
                        row_length=row.length,
                        raw=row.data,
                    )
                )
                continue
            object_id = int.from_bytes(row.data[5:9], "little", signed=False)
            candidate = _syscolumn_candidate_from_row(
                object_id=object_id,
                absolute=(page_ref.page_no * page_size) + row.page_offset + 5,
                page_size=page_size,
                window=row.data,
                local_object_offset=5,
            )
            if candidate is None:
                failures.append(
                    _syscolumn_failure(
                        reason="row-field-decode-failed",
                        file_no=page_ref.file_no,
                        page_no=page_ref.page_no,
                        page_offset=row.page_offset,
                        row_length=row.length,
                        raw=row.data,
                    )
                )
                continue
            candidates.append(candidate)
        if pages_done == len(plan.pages) or pages_done % 64 == 0:
            _emit_storage_progress(
                progress,
                "SYSCOLUMNS storage progress: "
                f"pages={pages_done}/{len(plan.pages)} rows_seen={rows_seen} "
                f"rows_decoded={len(candidates)} failures={len(failures)}",
            )
    rows = _dedupe_syscolumn_candidates(candidates)
    if failure_path is not None:
        _write_syscolumn_failures(failure_path, failures)
    _emit_storage_progress(
        progress,
        "SYSCOLUMNS storage done: "
        f"pages={pages_done}/{len(plan.pages)} rows_seen={rows_seen} "
        f"rows_decoded={len(rows)} failures={len(failures)}"
        + (f" failure_file={failure_path}" if failure_path is not None else ""),
    )
    return rows


def dump_sysobject_rows_from_storage(
    system_file: Path,
    *,
    root_file: int,
    root_page: int,
    storage_id: int,
    group_id: int = 0,
    page_size: int = 8192,
    progress: Callable[[str], None] | None = None,
) -> list[SysObjectRowCandidate]:
    """Read SYS.SYSOBJECTS rows from its storage root and page slots."""

    storage_rows = _iter_storage_live_rows(
        system_file,
        dictionary_name="SYSOBJECTS",
        root_file=root_file,
        root_page=root_page,
        storage_id=storage_id,
        group_id=group_id,
        page_size=page_size,
        progress=progress,
    )
    candidates: list[SysObjectRowCandidate] = []
    for page_no, row in storage_rows:
        candidates.extend(
            _sysobject_rows_from_context(
                context=row.data,
                absolute_context_start=(page_no * page_size) + row.page_offset,
                page_size=page_size,
                local_marker_offset=0,
            )
        )
    rows = _dedupe_sysobject_row_candidates(candidates)
    _emit_storage_progress(
        progress,
        "SYSOBJECTS storage done: "
        f"rows_seen={len(storage_rows)} rows_decoded={len(rows)}",
    )
    return rows


def discover_storage_root_page(
    system_file: Path,
    *,
    storage_id: int,
    root_file: int = 0,
    page_size: int = 8192,
) -> StorageRootCandidate | None:
    """Find a likely BTREE storage root page by scanning page headers.

    The observed DM8 SYSTEM dictionary roots have matching page identity,
    matching page-header storage id, kind 0x15, and null prev/next references.
    Leaf pages may also have null prev on the leftmost leaf, so the kind 0x15
    predicate is intentionally preferred for root discovery.
    """

    data_file = DataFile(system_file, page_size=page_size)
    pages_total = system_file.stat().st_size // page_size
    best: StorageRootCandidate | None = None
    for page_no in range(pages_total):
        page = data_file.read_page(page_no)
        try:
            header = ObservedPageHeader.from_page(page)
        except ValueError:
            continue
        if header.page_no != page_no:
            continue
        if header.storage_id_candidate != storage_id:
            continue
        score = 0
        source_parts = ["page-header-storage-id"]
        if header.page_kind_raw == 0x15:
            score += 100
            source_parts.append("kind-0x15")
        elif header.page_kind_raw == 0x14:
            score += 20
            source_parts.append("kind-0x14")
        else:
            continue
        if header.prev_page.is_null:
            score += 10
            source_parts.append("prev-null")
        if header.next_page.is_null:
            score += 10
            source_parts.append("next-null")
        if header.page_kind_raw == 0x15 and header.prev_page.is_null and header.next_page.is_null:
            score += 100
            source_parts.append("root-shape")
        candidate = StorageRootCandidate(
            storage_id=storage_id,
            root_file=root_file,
            root_page=page_no,
            page_kind_raw=header.page_kind_raw,
            score=score,
            source="+".join(source_parts),
        )
        if best is None or (candidate.score, -candidate.root_page) > (best.score, -best.root_page):
            best = candidate
    return best


def discover_system_dictionary_root_from_file_header(
    system_file: Path,
    *,
    object_id: int,
    root_file: int = 0,
    page_size: int = 8192,
) -> StorageRootCandidate | None:
    """Discover a standard SYS dictionary root from SYSTEM file header metadata.

    DM7 and DM8 samples both store the bootstrap entry pages for SYSOBJECTS and
    SYSINDEXES in SYSTEM.DBF page 0. The root page itself then exposes the
    storage id in its normal page header.
    """

    root_offset = SYSTEM_FILE_HEADER_ROOT_OFFSETS.get(object_id)
    if root_offset is None:
        return None
    data_file = DataFile(system_file, page_size=page_size)
    page0 = data_file.read_page(0)
    if len(page0) < root_offset + 4:
        return None
    root_page = int.from_bytes(page0[root_offset : root_offset + 4], "little", signed=False)
    pages_total = system_file.stat().st_size // page_size
    if root_page <= 0 or root_page >= pages_total:
        return None
    root = data_file.read_page(root_page)
    header = ObservedPageHeader.from_page(root)
    if header.page_no != root_page:
        return None
    if header.page_kind_raw not in {0x14, 0x15}:
        return None
    storage_id = header.storage_id_candidate
    if storage_id <= 0:
        return None
    score = 200
    source_parts = [f"system-page0-offset-0x{root_offset:x}"]
    if header.page_kind_raw == 0x15:
        score += 50
        source_parts.append("kind-0x15")
    if header.prev_page.is_null:
        score += 10
        source_parts.append("prev-null")
    if header.next_page.is_null:
        score += 10
        source_parts.append("next-null")
    if header.page_kind_raw == 0x15 and header.prev_page.is_null and header.next_page.is_null:
        score += 50
        source_parts.append("root-shape")
    return StorageRootCandidate(
        storage_id=storage_id,
        root_file=root_file,
        root_page=root_page,
        page_kind_raw=header.page_kind_raw,
        score=score,
        source="+".join(source_parts),
    )


def dump_sysindex_rows_from_storage(
    system_file: Path,
    *,
    root_file: int,
    root_page: int,
    storage_id: int,
    group_id: int = 0,
    page_size: int = 8192,
    progress: Callable[[str], None] | None = None,
) -> list[SysIndexCandidate]:
    """Read SYS.SYSINDEXES rows from its storage root and page slots."""

    storage_rows = _iter_storage_live_rows(
        system_file,
        dictionary_name="SYSINDEXES",
        root_file=root_file,
        root_page=root_page,
        storage_id=storage_id,
        group_id=group_id,
        page_size=page_size,
        progress=progress,
    )
    candidates: list[SysIndexCandidate] = []
    type_markers = (b"BT", b"RT", b"HT")
    for page_no, row in storage_rows:
        seen_offsets: set[int] = set()
        for marker in type_markers:
            search_from = 0
            while True:
                type_offset = row.data.find(marker, search_from)
                if type_offset < 0:
                    break
                id_offset = type_offset - 13
                if id_offset >= 0 and id_offset not in seen_offsets:
                    seen_offsets.add(id_offset)
                    context = row.data[id_offset : min(len(row.data), id_offset + 64)]
                    if len(context) >= 23:
                        index_id = int.from_bytes(context[0:4], "little", signed=False)
                        if 1 <= index_id <= 100_000_000:
                            candidate = _sysindex_candidate_from_context(
                                index_id=index_id,
                                absolute=(page_no * page_size) + row.page_offset + id_offset,
                                page_size=page_size,
                                context=context,
                            )
                            if candidate is not None:
                                candidates.append(candidate)
                search_from = type_offset + 1
    rows = _dedupe_sysindex_candidates(candidates)
    _emit_storage_progress(
        progress,
        "SYSINDEXES storage done: "
        f"rows_seen={len(storage_rows)} rows_decoded={len(rows)}",
    )
    return rows


def _iter_storage_live_rows(
    system_file: Path,
    *,
    dictionary_name: str,
    root_file: int,
    root_page: int,
    storage_id: int,
    group_id: int,
    page_size: int,
    progress: Callable[[str], None] | None,
) -> list[tuple[int, Any]]:
    data_file = DataFile(system_file, page_size=page_size)
    table = TableMeta(
        owner="SYS",
        name=dictionary_name,
        columns=(),
        storage=StorageRoot(
            group_id=group_id,
            file_no=root_file,
            root_page=root_page,
            storage_id=storage_id,
        ),
    )
    plan = _build_root_page_plan(table=table, data_files={root_file: data_file})
    _emit_storage_progress(
        progress,
        f"{dictionary_name} storage plan: "
        f"root_file={root_file} root_page={root_page} storage_id={storage_id} "
        f"pages={len(plan.pages)} diagnostics={len(plan.diagnostics)}",
    )
    rows: list[tuple[int, Any]] = []
    pages_done = 0
    for page_ref in plan.pages:
        pages_done += 1
        page = data_file.read_page(page_ref.page_no)
        header = ObservedPageHeader.from_page(page)
        if header.page_kind_raw != 0x14:
            continue
        if header.storage_id_candidate != storage_id:
            continue
        for row in iter_observed_rows_by_slots(page):
            if not row.is_deleted:
                rows.append((page_ref.page_no, row))
        if pages_done == len(plan.pages) or pages_done % 64 == 0:
            _emit_storage_progress(
                progress,
                f"{dictionary_name} storage progress: "
                f"pages={pages_done}/{len(plan.pages)} rows_seen={len(rows)}",
            )
    return rows


def _emit_storage_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _syscolumn_failure(
    *,
    reason: str,
    file_no: int,
    page_no: int,
    page_offset: int | None = None,
    row_length: int | None = None,
    raw: bytes = b"",
) -> dict[str, Any]:
    return {
        "reason": reason,
        "file_no": file_no,
        "page_no": page_no,
        "page_offset": "" if page_offset is None else page_offset,
        "row_length": "" if row_length is None else row_length,
        "raw_hex": raw.hex(),
    }


def _write_syscolumn_failures(path: Path, failures: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        fieldnames = ("reason", "file_no", "page_no", "page_offset", "row_length", "raw_hex")
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(failures)

def dump_sysindex_rows(
    system_file: Path,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
    progress: Callable[[str], None] | None = None,
) -> list[SysIndexCandidate]:
    """Scan SYSTEM.DBF once for SYS.SYSINDEXES-like storage rows."""

    candidates: list[SysIndexCandidate] = []
    file_size = system_file.stat().st_size
    overlap = 64
    previous = b""
    offset = 0
    type_markers = (b"BT", b"RT", b"HT")
    _emit_scan_progress(progress, "SYSINDEXES", offset, file_size, len(candidates))
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            base_absolute = offset - len(previous)
            seen_offsets: set[int] = set()
            for marker in type_markers:
                search_from = 0
                while True:
                    type_offset = window.find(marker, search_from)
                    if type_offset < 0:
                        break
                    id_offset = type_offset - 13
                    if id_offset >= 0 and id_offset not in seen_offsets:
                        seen_offsets.add(id_offset)
                        context = window[id_offset : min(len(window), id_offset + 64)]
                        if len(context) >= 23:
                            index_id = int.from_bytes(context[0:4], "little", signed=False)
                            if 1 <= index_id <= 100_000_000:
                                candidate = _sysindex_candidate_from_context(
                                    index_id=index_id,
                                    absolute=base_absolute + id_offset,
                                    page_size=page_size,
                                    context=context,
                                )
                                if candidate is not None:
                                    candidates.append(candidate)
                    search_from = type_offset + 1
            previous = window[-overlap:]
            offset += len(chunk)
            _emit_scan_progress(progress, "SYSINDEXES", offset, file_size, len(candidates))
    rows = _dedupe_sysindex_candidates(candidates)
    _emit_scan_progress(progress, "SYSINDEXES", file_size, file_size, len(rows), done=True)
    return rows


def find_sysobject_index_child_candidates(
    system_file: Path,
    parent_object_id: int,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
) -> list[SysObjectIndexChildCandidate]:
    """Find SYSOBJECTS child INDEX objects for a table object id.

    Controlled samples show internal storage objects named `INDEX<id>` with
    `TYPE$ = TABOBJ`, `SUBTYPE$ = INDEX`, and `PID = <table object id>`. This
    scanner uses the parent object id as the anchor, then looks for the nearest
    following prefixed `TABOBJ` and `INDEX<digits>` strings.
    """

    marker = parent_object_id.to_bytes(4, "little", signed=False)
    candidates: list[SysObjectIndexChildCandidate] = []
    overlap = 512
    previous = b""
    offset = 0
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            search_from = 0
            while True:
                index = window.find(marker, search_from)
                if index < 0:
                    break
                absolute = offset - len(previous) + index
                context_start = max(0, index - 96)
                context_end = min(len(window), index + 192)
                context = window[context_start:context_end]
                local_parent_offset = index - context_start
                candidates.extend(
                    _sysobject_index_children_from_context(
                        parent_object_id=parent_object_id,
                        absolute=absolute,
                        page_size=page_size,
                        context=context,
                        local_parent_offset=local_parent_offset,
                    )
                )
                search_from = index + 1
            previous = window[-overlap:]
            offset += len(chunk)
    return _dedupe_sysobject_index_child_candidates(candidates)



def dump_sysobject_rows(
    system_file: Path,
    *,
    page_size: int = 8192,
    chunk_size: int = 1024 * 1024,
    progress: Callable[[str], None] | None = None,
) -> list[SysObjectRowCandidate]:
    """Scan SYSTEM.DBF for the SYSOBJECTS rows needed for table bootstrap.

    This is a first-stage offline dictionary downloader. It decodes the stable
    string markers observed in SYSOBJECTS (`SCHOBJ`/`TABOBJ`, `UTAB`/`STAB`, and
    `INDEX<storage_id>`) and recovers nearby integer ids without requiring a
    target table name from the user.
    """

    candidates: list[SysObjectRowCandidate] = []
    file_size = system_file.stat().st_size
    overlap = 512
    previous = b""
    offset = 0
    markers = (b"SCHOBJ", b"TABOBJ", b"SCH")
    _emit_scan_progress(progress, "SYSOBJECTS", offset, file_size, len(candidates))
    with system_file.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            window = previous + chunk
            seen_in_window: set[int] = set()
            for marker in markers:
                search_from = 0
                while True:
                    index = window.find(marker, search_from)
                    if index < 0:
                        break
                    if index not in seen_in_window:
                        seen_in_window.add(index)
                        context_start = max(0, index - 180)
                        context_end = min(len(window), index + 260)
                        context = window[context_start:context_end]
                        local_marker_offset = index - context_start
                        absolute_context_start = offset - len(previous) + context_start
                        candidates.extend(
                            _sysobject_rows_from_context(
                                context=context,
                                absolute_context_start=absolute_context_start,
                                page_size=page_size,
                                local_marker_offset=local_marker_offset,
                            )
                        )
                    search_from = index + 1
            previous = window[-overlap:]
            offset += len(chunk)
            _emit_scan_progress(progress, "SYSOBJECTS", offset, file_size, len(candidates))
    rows = _dedupe_sysobject_row_candidates(candidates)
    _emit_scan_progress(progress, "SYSOBJECTS", file_size, file_size, len(rows), done=True)
    return rows


def _emit_scan_progress(
    progress: Callable[[str], None] | None,
    name: str,
    scanned: int,
    total: int,
    rows: int,
    *,
    done: bool = False,
) -> None:
    if progress is None:
        return
    if not done and scanned not in {0, total} and scanned % SCAN_PROGRESS_BYTES != 0:
        return
    percent = (scanned * 100.0 / total) if total else 0.0
    status = "done" if done else "progress"
    progress(
        f"{name} scan {status}: bytes={min(scanned, total)}/{total} "
        f"percent={percent:.1f} rows={rows}"
    )


def _sysobject_rows_from_context(
    *,
    context: bytes,
    absolute_context_start: int,
    page_size: int,
    local_marker_offset: int,
) -> list[SysObjectRowCandidate]:
    strings = _prefixed_ascii_strings(context)
    rows: list[SysObjectRowCandidate] = []
    for index, (string_offset, value) in enumerate(strings):
        if value == "SCHOBJ":
            rows.extend(
                _sysobject_table_rows_from_strings(
                    strings=strings,
                    type_index=index,
                    context=context,
                    absolute_context_start=absolute_context_start,
                    page_size=page_size,
                    local_marker_offset=local_marker_offset,
                )
            )
        elif value == "TABOBJ":
            rows.extend(
                _sysobject_index_rows_from_strings(
                    strings=strings,
                    type_index=index,
                    context=context,
                    absolute_context_start=absolute_context_start,
                    page_size=page_size,
                    local_marker_offset=local_marker_offset,
                )
            )
        elif value == "SCH":
            rows.extend(
                _sysobject_schema_rows_from_strings(
                    strings=strings,
                    type_index=index,
                    context=context,
                    absolute_context_start=absolute_context_start,
                    page_size=page_size,
                    local_marker_offset=local_marker_offset,
                )
            )
    return rows


def _sysobject_schema_rows_from_strings(
    *,
    strings: list[tuple[int, str]],
    type_index: int,
    context: bytes,
    absolute_context_start: int,
    page_size: int,
    local_marker_offset: int,
) -> list[SysObjectRowCandidate]:
    type_offset, _ = strings[type_index]
    next_names = [
        (offset, value)
        for offset, value in strings[type_index + 1 : type_index + 4]
        if offset - type_offset <= 24 and _is_sysobject_name(value)
    ]
    name_after_type = bool(next_names)
    if name_after_type:
        name_offset, name = next_names[0]
        object_id = _sysobjects_schema_object_id_from_type_offset(context, type_offset)
    else:
        previous_names = [
            (offset, value)
            for offset, value in strings[max(0, type_index - 3) : type_index]
            if _is_sysobject_name(value)
        ]
        if not previous_names:
            return []
        name_offset, name = previous_names[-1]
        if type_offset - name_offset > 48:
            return []
        object_id = _sysobjects_schema_object_id_from_name_offset(context, name_offset)
    if object_id is None:
        return []
    score = 60
    if object_id is not None:
        score += 30
        if object_id >= 0x09000000:
            score += 40
        elif object_id <= 100:
            score += 20
    if name_after_type:
        score += 30
    if 0 <= type_offset - name_offset <= 16:
        score += 15
    if 0 <= name_offset - type_offset <= 16:
        score += 15
    absolute = absolute_context_start + name_offset
    return [
        SysObjectRowCandidate(
            name=name,
            object_id=object_id,
            schema_id=object_id,
            parent_id=None,
            type_name="SCH",
            subtype_name="",
            offset=absolute,
            page_no=absolute // page_size,
            page_offset=absolute % page_size,
            score=score,
            source="heuristic-system-scan",
        )
    ]


def _sysobjects_schema_object_id_from_type_offset(context: bytes, type_offset: int) -> int | None:
    # Clean schema rows observed in SYSOBJECTS store the full schema id as a
    # little-endian u32 eight bytes before the prefixed "SCH" type marker.
    offset = type_offset - 8
    if offset < 0 or offset + 4 > len(context):
        return None
    value = int.from_bytes(context[offset : offset + 4], "little", signed=False)
    normalized = _normalize_sysobjects_schema_id(value)
    if normalized is None:
        return None
    if not 0x09000000 <= normalized <= 0x09FFFFFF:
        return None
    return normalized


def _sysobject_table_rows_from_strings(
    *,
    strings: list[tuple[int, str]],
    type_index: int,
    context: bytes,
    absolute_context_start: int,
    page_size: int,
    local_marker_offset: int,
) -> list[SysObjectRowCandidate]:
    type_offset, _ = strings[type_index]
    previous_names = [
        (offset, value)
        for offset, value in strings[max(0, type_index - 4) : type_index]
        if _is_sysobject_name(value)
    ]
    if not previous_names:
        return []
    name_offset, name = previous_names[-1]
    subtype_name = None
    for subtype_offset, value in strings[type_index + 1 : type_index + 5]:
        if subtype_offset - type_offset > 48:
            break
        if value in {"UTAB", "STAB", "PROC", "PKG", "TYPE", "CLASS", "TRIG"}:
            subtype_name = value
            break
    if subtype_name is None:
        return []
    fixed = _sysobjects_table_fixed_area_from_name_offset(context, name_offset)
    has_fixed_area = fixed is not None
    if fixed is not None:
        object_id_offset, object_id, schema_id = fixed
    else:
        object_id_found = _nearest_plausible_id_before_with_offset(
            context,
            anchor_offset=name_offset,
            start=max(0, name_offset - 96),
            minimum=0,
            maximum=10_000_000,
        )
        object_id_offset = None if object_id_found is None else object_id_found[0]
        object_id = None if object_id_found is None else object_id_found[1]
        schema_id = _sysobjects_schema_id_from_fixed_area(context, object_id_offset)
    parent_id = _sysobjects_parent_id_from_fixed_area(context, object_id_offset)
    score = 70
    if has_fixed_area:
        score += 60
    if object_id is not None:
        score += 30
        if 10_000 <= object_id <= 60_000:
            score += 20
    if 0 <= type_offset - name_offset <= 80:
        score += 15
    absolute = absolute_context_start + name_offset
    return [
        SysObjectRowCandidate(
            name=name,
            object_id=object_id,
            schema_id=schema_id,
            parent_id=parent_id,
            type_name="SCHOBJ",
            subtype_name=subtype_name,
            offset=absolute,
            page_no=absolute // page_size,
            page_offset=absolute % page_size,
            score=score,
            source="heuristic-system-scan",
        )
    ]


def _sysobjects_table_fixed_area_from_name_offset(
    context: bytes,
    name_offset: int,
) -> tuple[int, int, int] | None:
    # Clean SYSOBJECTS SCHOBJ rows from slot decoding place the object id 57
    # bytes before NAME. The full schema id starts three bytes after the object
    # id in observed DM7/DM8 system-table rows.
    candidates: list[tuple[int, int, int, int, int, int, int]] = []
    for object_offset in range(name_offset - 72, name_offset - 16):
        if object_offset < 0 or object_offset + 7 > len(context):
            continue
        object_id = int.from_bytes(context[object_offset : object_offset + 4], "little", signed=False)
        if object_id > 100_000_000:
            continue
        for schema_offset in (object_offset + 3, object_offset + 4):
            if schema_offset + 4 > len(context):
                continue
            schema_id = int.from_bytes(context[schema_offset : schema_offset + 4], "little", signed=False)
            if 0x09000000 <= schema_id <= 0x09FFFFFF:
                distance = abs((name_offset - object_offset) - 58)
                if 10_000 <= object_id <= 100_000:
                    object_rank = 0
                elif 1 <= object_id <= 100:
                    object_rank = 1
                elif object_id >= 0x02000000:
                    object_rank = 2
                elif object_id == 0:
                    object_rank = 4
                else:
                    object_rank = 3
                schema_rank = 0 if schema_offset == object_offset + 4 else 1
                candidates.append((object_rank, distance, schema_rank, object_offset, object_id, schema_id, schema_offset))
    if not candidates:
        return None
    _, _, _, object_offset, object_id, schema_id, _ = min(candidates)
    return object_offset, object_id, schema_id


def _sysobject_index_rows_from_strings(
    *,
    strings: list[tuple[int, str]],
    type_index: int,
    context: bytes,
    absolute_context_start: int,
    page_size: int,
    local_marker_offset: int,
) -> list[SysObjectRowCandidate]:
    type_offset, _ = strings[type_index]
    subtype_is_index = any(
        value == "INDEX" and 0 <= offset - type_offset <= 48
        for offset, value in strings[type_index + 1 : type_index + 5]
    ) or any(
        _is_index_object_name(value) and 0 <= offset - type_offset <= 96
        for offset, value in strings[type_index + 1 : type_index + 6]
    )
    if not subtype_is_index:
        return []
    previous_names = [
        (offset, value)
        for offset, value in strings[max(0, type_index - 4) : type_index]
        if _is_sysobject_name(value) and 0 <= type_offset - offset <= 96
    ]
    for name_offset, name in reversed(previous_names):
        fixed = _sysobjects_index_fixed_area_from_name_offset(context, name_offset)
        if fixed is None:
            continue
        index_id, schema_id, parent_id = fixed
        absolute = absolute_context_start + name_offset
        return [
            SysObjectRowCandidate(
                name=name,
                object_id=index_id,
                schema_id=schema_id,
                parent_id=parent_id,
                type_name="TABOBJ",
                subtype_name="INDEX",
                offset=absolute,
                page_no=absolute // page_size,
                page_offset=absolute % page_size,
                score=150,
                source="heuristic-system-scan",
            )
        ]
    previous_index_names = [
        (offset, value)
        for offset, value in strings[max(0, type_index - 4) : type_index]
        if _is_index_object_name(value) and 0 <= type_offset - offset <= 96
    ]
    if previous_index_names:
        name_offset, name = previous_index_names[-1]
        return _sysobject_index_row_from_name(
            context=context,
            absolute_context_start=absolute_context_start,
            page_size=page_size,
            type_offset=type_offset,
            name_offset=name_offset,
            name=name,
        )
    for name_offset, name in strings[type_index + 1 : type_index + 6]:
        if name_offset - type_offset > 96:
            break
        if not _is_index_object_name(name):
            continue
        return _sysobject_index_row_from_name(
            context=context,
            absolute_context_start=absolute_context_start,
            page_size=page_size,
            type_offset=type_offset,
            name_offset=name_offset,
            name=name,
        )
    return []


def _sysobjects_index_fixed_area_from_name_offset(
    context: bytes,
    name_offset: int,
) -> tuple[int, int | None, int | None] | None:
    candidates: list[tuple[int, int, int, int | None, int | None]] = []
    for object_offset in range(name_offset - 64, name_offset - 49):
        if object_offset < 0 or object_offset + 12 > len(context):
            continue
        object_id = int.from_bytes(context[object_offset : object_offset + 4], "little", signed=False)
        if not 1 <= object_id <= 100_000_000:
            continue
        for schema_offset in (object_offset + 4, object_offset + 3):
            if schema_offset + 4 > len(context):
                continue
            raw_schema_id = int.from_bytes(context[schema_offset : schema_offset + 4], "little", signed=False)
            schema_id = raw_schema_id if 0x09000000 <= raw_schema_id <= 0x09FFFFFF else None
            if schema_id is None:
                continue
            parent_id = None
            for parent_offset in (schema_offset + 4, schema_offset + 3):
                if parent_offset + 4 > len(context):
                    continue
                value = int.from_bytes(context[parent_offset : parent_offset + 4], "little", signed=False)
                if 0 <= value <= 100_000_000:
                    parent_id = value
                    break
            if parent_id is None:
                continue
            distance = abs((name_offset - object_offset) - 57)
            object_rank = 0 if object_id >= 0x02000000 else 1
            candidates.append((object_rank, distance, object_id, schema_id, parent_id))
    if not candidates:
        return None
    _, _, object_id, schema_id, parent_id = min(candidates)
    return object_id, schema_id, parent_id


def _sysobject_index_row_from_name(
    *,
    context: bytes,
    absolute_context_start: int,
    page_size: int,
    type_offset: int,
    name_offset: int,
    name: str,
) -> list[SysObjectRowCandidate]:
    index_id = int(name[5:])
    index_id_offset = _nearest_bytes_offset(
        context,
        index_id.to_bytes(4, "little", signed=False),
        anchor_offset=type_offset,
        start=max(0, min(name_offset, type_offset) - 128),
        end=min(len(context), max(name_offset + len(name), type_offset) + 64),
    )
    schema_id = _sysobjects_schema_id_from_fixed_area(context, index_id_offset)
    parent_id = _sysobjects_parent_id_from_fixed_area(context, index_id_offset)
    if parent_id is None:
        parent_id = _nearest_plausible_id_before(
            context,
            anchor_offset=type_offset,
            start=max(0, type_offset - 96),
            minimum=1,
            maximum=10_000_000,
            exclude={index_id},
        )
    score = 65
    if parent_id is not None:
        score += 25
        if 10_000 <= parent_id <= 60_000:
            score += 20
    if index_id_offset is not None:
        score += 15
    if name_offset < type_offset:
        score += 10
    absolute = absolute_context_start + name_offset
    return [
        SysObjectRowCandidate(
            name=name,
            object_id=index_id,
            schema_id=schema_id,
            parent_id=parent_id,
            type_name="TABOBJ",
            subtype_name="INDEX",
            offset=absolute,
            page_no=absolute // page_size,
            page_offset=absolute % page_size,
            score=score,
            source="heuristic-system-scan",
        )
    ]


def _is_sysobject_name(value: str) -> bool:
    if value in {"SCH", "SCHOBJ", "TABOBJ", "UR", "UTAB", "STAB", "INDEX"}:
        return False
    if value in KNOWN_DM_TYPE_NAMES:
        return False
    return bool(value) and _is_printable_ascii_identifier(value.encode("ascii"))


def _nearest_plausible_id_before(
    context: bytes,
    *,
    anchor_offset: int,
    start: int,
    minimum: int,
    maximum: int,
    exclude: set[int] | None = None,
) -> int | None:
    found = _nearest_plausible_id_before_with_offset(
        context,
        anchor_offset=anchor_offset,
        start=start,
        minimum=minimum,
        maximum=maximum,
        exclude=exclude,
    )
    return None if found is None else found[1]


def _nearest_plausible_id_before_with_offset(
    context: bytes,
    *,
    anchor_offset: int,
    start: int,
    minimum: int,
    maximum: int,
    exclude: set[int] | None = None,
) -> tuple[int, int] | None:
    excluded = exclude or set()
    best: tuple[int, int, int, int] | None = None
    for index in range(start, max(start, anchor_offset - 3)):
        value = int.from_bytes(context[index : index + 4], "little", signed=False)
        if value in excluded or value < minimum or value > maximum:
            continue
        distance = anchor_offset - index
        preferred_rank = 0 if 10_000 <= value <= 60_000 else 1
        rank = (preferred_rank, distance, value, index)
        if best is None or rank < best:
            best = rank
    return None if best is None else (best[3], best[2])


def _sysobjects_schema_id_from_fixed_area(context: bytes, object_id_offset: int | None) -> int | None:
    if object_id_offset is None or object_id_offset + 8 > len(context):
        return None
    value = int.from_bytes(
        context[object_id_offset + 4 : object_id_offset + 8],
        "little",
        signed=False,
    )
    return _normalize_sysobjects_schema_id(value)


def _sysobjects_parent_id_from_fixed_area(context: bytes, object_id_offset: int | None) -> int | None:
    if object_id_offset is None or object_id_offset + 12 > len(context):
        return None
    value = int.from_bytes(context[object_id_offset + 8 : object_id_offset + 12], "little", signed=False)
    if value == 0xFFFFFFFF or value == 0:
        return None
    return value


def _normalize_sysobjects_schema_id(value: int) -> int | None:
    if value in {0, 0xFFFFFFFF}:
        return None
    if 1 <= value < 0x10000:
        return 0x09000000 + value
    return value


def _sysobjects_schema_object_id_from_name_offset(context: bytes, name_offset: int) -> int | None:
    # Clean SYSOBJECTS schema rows observed in SYSTEM.DBF store the schema id in
    # the fixed area shortly before NAME. Keep this deliberately narrow so it is
    # used only as an owner-name calibration signal.
    offset = name_offset - 57
    if offset < 0 or offset + 4 > len(context):
        return None
    value = int.from_bytes(context[offset : offset + 4], "little", signed=False)
    normalized = _normalize_sysobjects_schema_id(value)
    if normalized is None:
        return None
    if 0x09000000 <= normalized <= 0x09FFFFFF:
        return normalized
    return None


def _dedupe_sysobject_row_candidates(
    candidates: list[SysObjectRowCandidate],
) -> list[SysObjectRowCandidate]:
    best_by_key: dict[tuple[str, int | None, str, str, int | None], SysObjectRowCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.name,
            candidate.object_id,
            candidate.type_name,
            candidate.subtype_name,
            candidate.parent_id,
        )
        existing = best_by_key.get(key)
        if existing is None or (candidate.score, -candidate.offset) > (
            existing.score,
            -existing.offset,
        ):
            best_by_key[key] = candidate
    return sorted(
        best_by_key.values(),
        key=lambda item: (item.type_name != "SCHOBJ", -item.score, item.name, item.offset),
    )


def _candidate_from_context(
    *,
    name: str,
    absolute: int,
    page_size: int,
    context: bytes,
) -> SysObjectCandidate:
    has_schobj = b"SCHOBJ" in context
    has_utab = b"UTAB" in context
    local_marker = context.find(name.encode("utf-8"))
    marker_offset = max(0, local_marker)
    object_ids = _plausible_object_ids(context, marker_offset=marker_offset)
    likely_object_ids = _likely_object_ids_before_name(
        context,
        marker_offset=marker_offset,
    )
    preferred_object_ids = tuple(
        value for value in likely_object_ids if 10_000 <= value <= 60_000
    )
    score = 0
    if has_schobj:
        score += 20
    if has_utab:
        score += 20
    if object_ids:
        score += 10
    if likely_object_ids:
        score += 15
    if preferred_object_ids:
        score += 25
    if _has_prefixed_string(context, name.encode("utf-8")):
        score += 10
    return SysObjectCandidate(
        name=name,
        offset=absolute,
        page_no=absolute // page_size,
        page_offset=absolute % page_size,
        score=score,
        object_ids=object_ids,
        likely_object_ids=likely_object_ids,
        preferred_object_ids=preferred_object_ids,
        has_schobj=has_schobj,
        has_utab=has_utab,
    )


def _syscolumn_candidates_from_context(
    *,
    object_id: int,
    absolute: int,
    page_size: int,
    context: bytes,
    local_object_offset: int,
) -> list[SysColumnCandidate]:
    column_id, length, scale, nullable = _observed_column_id_length_scale_nullable(
        context,
        local_object_offset=local_object_offset,
    )
    strings = _prefixed_ascii_strings(context)
    useful_strings = [
        item
        for item in strings
        if item[1].isidentifier() or _is_known_dm_type_name(item[1])
    ]
    for name_index, (name_offset, name) in enumerate(useful_strings):
        if name.upper() in KNOWN_DM_TYPE_NAMES:
            continue
        if name_offset < local_object_offset:
            continue
        for type_offset, type_name in useful_strings[name_index + 1 : name_index + 5]:
            upper_type = type_name.upper()
            if not _is_known_dm_type_name(upper_type):
                continue
            score = _score_syscolumn_candidate(
                column_id=column_id,
                length=length,
                name_offset=name_offset,
                type_offset=type_offset,
                local_object_offset=local_object_offset,
            )
            return [
                SysColumnCandidate(
                    object_id=object_id,
                    offset=absolute,
                    page_no=absolute // page_size,
                    page_offset=absolute % page_size,
                    score=score,
                    column_id=column_id,
                    length=length,
                    scale=scale,
                    nullable=nullable,
                    name=name,
                    type_name=upper_type,
                    name_offset=name_offset,
                    type_offset=type_offset,
                )
            ]
    return []


def _observed_column_id_length_scale_nullable(
    context: bytes,
    *,
    local_object_offset: int,
) -> tuple[int | None, int | None, int | None, str | None]:
    if local_object_offset + 13 > len(context):
        return None, None, None, None
    column_id = int.from_bytes(
        context[local_object_offset + 4 : local_object_offset + 6],
        "little",
        signed=False,
    )
    length = int.from_bytes(
        context[local_object_offset + 6 : local_object_offset + 10],
        "little",
        signed=False,
    )
    scale = int.from_bytes(
        context[local_object_offset + 10 : local_object_offset + 12],
        "little",
        signed=False,
    )
    nullable_byte = context[local_object_offset + 12]
    nullable = chr(nullable_byte) if nullable_byte in {ord("N"), ord("Y")} else None
    if column_id > 4096:
        column_id = None
    if length > 1024 * 1024:
        length = None
    if scale > 10000:
        scale = None
    return column_id, length, scale, nullable


def _prefixed_ascii_strings(context: bytes) -> list[tuple[int, str]]:
    strings: list[tuple[int, str]] = []
    for index, first in enumerate(context):
        if first < 0x81 or first > 0xC0:
            continue
        length = first - 0x80
        start = index + 1
        end = start + length
        if end > len(context):
            continue
        raw = context[start:end]
        if not _is_printable_ascii_identifier(raw):
            continue
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        strings.append((index, value))
    return strings


def _is_known_dm_type_name(value: str) -> bool:
    upper = value.upper()
    return upper in KNOWN_DM_TYPE_NAMES or (upper.startswith("CLASS") and upper[5:].isdigit())


def _is_printable_ascii_identifier(value: bytes) -> bool:
    if not value:
        return False
    for byte in value:
        if byte not in b"_$#" and not (48 <= byte <= 57) and not (65 <= byte <= 90):
            return False
    return True


def _score_syscolumn_candidate(
    *,
    column_id: int | None,
    length: int | None,
    name_offset: int,
    type_offset: int,
    local_object_offset: int,
) -> int:
    score = 20
    if column_id is not None:
        score += 20
    if length is not None:
        score += 15
    if local_object_offset < name_offset < type_offset:
        score += 20
    distance = type_offset - local_object_offset
    if 8 <= distance <= 96:
        score += 20
    if type_offset - name_offset <= 48:
        score += 10
    return score


def _dedupe_syscolumn_candidates(
    candidates: list[SysColumnCandidate],
) -> list[SysColumnCandidate]:
    best_by_key: dict[
        tuple[int, int | None, str, str, int | None, int | None, str | None],
        SysColumnCandidate,
    ] = {}
    for candidate in candidates:
        key = (
            candidate.object_id,
            candidate.column_id,
            candidate.name,
            candidate.type_name,
            candidate.length,
            candidate.scale,
            candidate.nullable,
        )
        existing = best_by_key.get(key)
        if existing is None or (candidate.score, -candidate.offset) > (
            existing.score,
            -existing.offset,
        ):
            best_by_key[key] = candidate
    return sorted(
        best_by_key.values(),
        key=lambda item: (
            item.column_id is None,
            item.column_id if item.column_id is not None else 0,
            -item.score,
            item.offset,
        ),
    )


def _sysindex_candidate_from_context(
    *,
    index_id: int,
    absolute: int,
    page_size: int,
    context: bytes,
) -> SysIndexCandidate | None:
    if len(context) < 23:
        return None
    is_unique_byte = context[4]
    is_unique = chr(is_unique_byte) if is_unique_byte in (ord("N"), ord("Y")) else None
    group_id = int.from_bytes(context[5:7], "little", signed=False)
    root_file = int.from_bytes(context[7:9], "little", signed=False)
    root_page = int.from_bytes(context[9:13], "little", signed=False)
    type_raw = context[13:15]
    try:
        type_name = type_raw.decode("ascii")
    except UnicodeDecodeError:
        type_name = None
    xtype = int.from_bytes(context[15:19], "little", signed=False)
    flag = int.from_bytes(context[19:23], "little", signed=False)
    keynum = None
    keyinfo_hex = None
    if len(context) >= 25:
        keynum = int.from_bytes(context[23:25], "little", signed=False)
        keyinfo_hex = _sysindex_keyinfo_hex_from_context(context, keynum=keynum)
    score = 0
    if is_unique is not None:
        score += 20
    if group_id <= 65535:
        score += 10
    if root_file <= 4096:
        score += 10
    if 0 <= root_page <= 1_000_000_000:
        score += 10
    if type_name in {"BT", "RT", "HT"}:
        score += 30
    if flag in {0, 1, 3, 5}:
        score += 10
    if keynum is not None and keynum <= 4096:
        score += 5
    if score < 60:
        return None
    return SysIndexCandidate(
        index_id=index_id,
        offset=absolute,
        page_no=absolute // page_size,
        page_offset=absolute % page_size,
        score=score,
        is_unique=is_unique,
        group_id=group_id,
        root_file=root_file,
        root_page=root_page,
        type_name=type_name,
        xtype=xtype,
        flag=flag,
        keynum=keynum,
        keyinfo_hex=keyinfo_hex,
    )


def _sysindex_keyinfo_hex_from_context(context: bytes, *, keynum: int) -> str | None:
    target_length = keynum * 3
    if target_length == 0:
        return ""
    if target_length < 0 or target_length > 4096:
        return None
    for offset in range(25, min(len(context), 48)):
        try:
            decoded = decode_observed_var_length(context[offset:])
        except ValueError:
            continue
        if decoded.length != target_length:
            continue
        start = offset + decoded.encoded_size
        stop = start + decoded.length
        if stop <= len(context):
            return context[start:stop].hex()
    direct_start = 25
    direct_stop = direct_start + target_length
    if direct_stop <= len(context):
        return context[direct_start:direct_stop].hex()
    return None


def _dedupe_sysindex_candidates(
    candidates: list[SysIndexCandidate],
) -> list[SysIndexCandidate]:
    best_by_key: dict[tuple[int | None, int | None, int | None, str | None], SysIndexCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.group_id,
            candidate.root_file,
            candidate.root_page,
            candidate.type_name,
        )
        existing = best_by_key.get(key)
        if existing is None or (candidate.score, -candidate.offset) > (
            existing.score,
            -existing.offset,
        ):
            best_by_key[key] = candidate
    return sorted(best_by_key.values(), key=lambda item: (-item.score, item.offset))


def _sysobject_index_children_from_context(
    *,
    parent_object_id: int,
    absolute: int,
    page_size: int,
    context: bytes,
    local_parent_offset: int,
) -> list[SysObjectIndexChildCandidate]:
    candidates: list[SysObjectIndexChildCandidate] = []
    strings = _prefixed_ascii_strings(context)
    for item_index, (name_offset, name) in enumerate(strings):
        if name_offset < local_parent_offset:
            continue
        if not _is_index_object_name(name):
            continue
        type_name = _nearest_type_before_index_name(
            strings=strings,
            item_index=item_index,
            name_offset=name_offset,
        )
        if type_name != "TABOBJ":
            continue
        index_id = int(name[5:])
        index_id_offset = _nearest_bytes_offset(
            context,
            index_id.to_bytes(4, "little", signed=False),
            anchor_offset=local_parent_offset,
            start=max(0, local_parent_offset - 96),
            end=min(len(context), name_offset + len(name) + 32),
        )
        score = _score_sysobject_index_child(
            name_offset=name_offset,
            local_parent_offset=local_parent_offset,
            index_id_offset=index_id_offset,
        )
        candidates.append(
            SysObjectIndexChildCandidate(
                parent_object_id=parent_object_id,
                index_id=index_id,
                name=name,
                offset=absolute,
                page_no=absolute // page_size,
                page_offset=absolute % page_size,
                score=score,
                type_name=type_name,
                name_offset=name_offset,
                index_id_offset=index_id_offset,
            )
        )
    return candidates


def _is_index_object_name(value: str) -> bool:
    return value.startswith("INDEX") and value[5:].isdigit()


def _nearest_type_before_index_name(
    *,
    strings: list[tuple[int, str]],
    item_index: int,
    name_offset: int,
) -> str | None:
    for type_offset, type_name in reversed(strings[:item_index]):
        if name_offset - type_offset > 24:
            break
        if type_name in {"TABOBJ", "SCHOBJ"}:
            return type_name
    return None


def _nearest_bytes_offset(
    context: bytes,
    marker: bytes,
    *,
    anchor_offset: int,
    start: int,
    end: int,
) -> int | None:
    found: tuple[int, int] | None = None
    search_from = start
    while True:
        index = context.find(marker, search_from, end)
        if index < 0:
            break
        distance = abs(index - anchor_offset)
        if found is None or distance < found[0]:
            found = (distance, index)
        search_from = index + 1
    return None if found is None else found[1]


def _score_sysobject_index_child(
    *,
    name_offset: int,
    local_parent_offset: int,
    index_id_offset: int | None,
) -> int:
    score = 30
    distance = name_offset - local_parent_offset
    if 0 <= distance <= 64:
        score += 40
    elif 0 <= distance <= 128:
        score += 20
    if index_id_offset is not None:
        score += 20
        if index_id_offset < local_parent_offset:
            score += 10
    return score


def _dedupe_sysobject_index_child_candidates(
    candidates: list[SysObjectIndexChildCandidate],
) -> list[SysObjectIndexChildCandidate]:
    best_by_key: dict[int, SysObjectIndexChildCandidate] = {}
    for candidate in candidates:
        existing = best_by_key.get(candidate.index_id)
        if existing is None or (candidate.score, -candidate.offset) > (
            existing.score,
            -existing.offset,
        ):
            best_by_key[candidate.index_id] = candidate
    return sorted(best_by_key.values(), key=lambda item: (-item.score, item.offset))


def _has_prefixed_string(context: bytes, value: bytes) -> bool:
    marker = bytes([0x80 + len(value)]) + value if len(value) <= 127 else value
    return marker in context


def _plausible_object_ids(context: bytes, *, marker_offset: int) -> tuple[int, ...]:
    found: dict[int, int] = {}
    for index in range(0, max(0, len(context) - 3)):
        value = int.from_bytes(context[index : index + 4], "little", signed=False)
        if 1 <= value <= 10_000_000 and value not in found:
            found[value] = abs(index - marker_offset)
    ranked = sorted(found, key=lambda value: (found[value], value))
    return tuple(ranked[:24])


def _likely_object_ids_before_name(
    context: bytes,
    *,
    marker_offset: int,
    lookback: int = 96,
) -> tuple[int, ...]:
    start = max(0, marker_offset - lookback)
    found: dict[int, int] = {}
    for index in range(start, max(start, marker_offset - 3)):
        value = int.from_bytes(context[index : index + 4], "little", signed=False)
        if 1_000 <= value <= 10_000_000 and value not in found:
            found[value] = marker_offset - index
    ranked = sorted(found, key=lambda value: (found[value], value))
    return tuple(ranked[:8])
