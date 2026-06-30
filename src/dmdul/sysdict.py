from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .row import decode_observed_var_length


KNOWN_DM_TYPE_NAMES = frozenset(
    {
        "BIGINT",
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
    flag: int | None


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
    if type_name not in KNOWN_DM_TYPE_NAMES:
        return None
    return SysColumnCandidate(
        object_id=object_id,
        offset=absolute,
        page_no=absolute // page_size,
        page_offset=absolute % page_size,
        score=140,
        column_id=column_id,
        length=length,
        name=name,
        type_name=type_name,
        name_offset=name_start,
        type_offset=type_start,
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
    column_id, length = _observed_column_id_and_length(
        context,
        local_object_offset=local_object_offset,
    )
    strings = _prefixed_ascii_strings(context)
    useful_strings = [
        item
        for item in strings
        if item[1].isidentifier() or item[1].upper() in KNOWN_DM_TYPE_NAMES
    ]
    for name_index, (name_offset, name) in enumerate(useful_strings):
        if name.upper() in KNOWN_DM_TYPE_NAMES:
            continue
        if name_offset < local_object_offset:
            continue
        for type_offset, type_name in useful_strings[name_index + 1 : name_index + 5]:
            upper_type = type_name.upper()
            if upper_type not in KNOWN_DM_TYPE_NAMES:
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
                    name=name,
                    type_name=upper_type,
                    name_offset=name_offset,
                    type_offset=type_offset,
                )
            ]
    return []


def _observed_column_id_and_length(
    context: bytes,
    *,
    local_object_offset: int,
) -> tuple[int | None, int | None]:
    if local_object_offset + 10 > len(context):
        return None, None
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
    if column_id > 4096:
        column_id = None
    if length > 1024 * 1024:
        length = None
    return column_id, length


def _prefixed_ascii_strings(context: bytes) -> list[tuple[int, str]]:
    strings: list[tuple[int, str]] = []
    for index in range(len(context)):
        try:
            decoded = decode_observed_var_length(context[index:])
        except ValueError:
            continue
        length = decoded.length
        if length < 1 or length > 64:
            continue
        start = index + decoded.encoded_size
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
    best_by_key: dict[tuple[int | None, str, str, int | None], SysColumnCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.column_id,
            candidate.name,
            candidate.type_name,
            candidate.length,
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
    flag = int.from_bytes(context[19:23], "little", signed=False)
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
    if flag in {0, 1}:
        score += 10
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
        flag=flag,
    )


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
