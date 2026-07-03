from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


_DBF_HINT_RE = re.compile(r"[A-Za-z0-9_./\\:-]*\.DBF", re.IGNORECASE)
_CONTROL_ATTRIBUTE_STRINGS = {
    "NORMAL",
}


def summarize_control_file(
    path: Path,
    *,
    sample_limit: int = 32,
    dbf_hint_limit: int | None = None,
) -> dict[str, Any]:
    """Summarize a DM control file without assuming its binary layout."""

    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    if dbf_hint_limit is not None and dbf_hint_limit < 0:
        raise ValueError("dbf_hint_limit must be non-negative")

    data = path.read_bytes()
    string_records = _extract_printable_string_records(data)
    dbf_occurrences: list[dict[str, Any]] = []
    dbf_hints: list[str] = []
    dbf_hint_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    if dbf_hint_limit != 0:
        for record in string_records:
            text = str(record["text"])
            for match in _DBF_HINT_RE.finditer(text):
                hint = match.group(0)
                occurrence = _dbf_path_occurrence(
                    text=hint,
                    offset=int(record["offset"]) + match.start(),
                    string_offset=int(record["offset"]),
                    ordinal=len(dbf_occurrences),
                )
                dbf_occurrences.append(occurrence)
                if hint not in seen:
                    seen.add(hint)
                    dbf_hints.append(hint)
                    dbf_hint_records.append(occurrence)
                if dbf_hint_limit is not None and len(dbf_hints) >= dbf_hint_limit:
                    break
            if dbf_hint_limit is not None and len(dbf_hints) >= dbf_hint_limit:
                break
    sampled_strings = string_records[:sample_limit]
    tablespace_file_hints = _control_file_tablespace_file_hints(
        string_records=string_records,
        dbf_occurrences=dbf_occurrences,
    )

    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dbf_path_hints": dbf_hints,
        "dbf_path_hint_records": dbf_hint_records,
        "dbf_path_occurrences": dbf_occurrences,
        "tablespace_file_hints": tablespace_file_hints,
        "printable_string_samples": [str(record["text"]) for record in sampled_strings],
        "printable_string_records": sampled_strings,
    }


def compare_control_files(
    before_path: Path,
    after_path: Path,
    *,
    context_bytes: int = 16,
    sample_limit: int = 64,
) -> dict[str, Any]:
    """Compare two control-file snapshots for byte-level structure research."""

    if context_bytes < 0:
        raise ValueError("context_bytes must be non-negative")
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")

    before = before_path.read_bytes()
    after = after_path.read_bytes()
    changed_ranges = _changed_ranges(before, after, sample_limit=sample_limit)
    return {
        "before": _file_identity(before_path, before),
        "after": _file_identity(after_path, after),
        "same_size": len(before) == len(after),
        "changed_bytes": _changed_byte_count(before, after),
        "changed_ranges_total": len(_changed_ranges(before, after, sample_limit=None)),
        "changed_ranges": [
            _range_record(
                start=start,
                stop=stop,
                before=before,
                after=after,
                context_bytes=context_bytes,
            )
            for start, stop in changed_ranges
        ],
    }


def _file_identity(path: Path, data: bytes) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _dbf_path_occurrence(
    *,
    text: str,
    offset: int,
    string_offset: int,
    ordinal: int,
) -> dict[str, Any]:
    normalized = text.replace("\\", "/")
    return {
        "ordinal": ordinal,
        "text": text,
        "normalized_path": normalized.lower(),
        "basename": Path(normalized).name.lower(),
        "offset": offset,
        "length": len(text),
        "string_offset": string_offset,
    }


def _control_file_tablespace_file_hints(
    *,
    string_records: list[dict[str, Any]],
    dbf_occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Associate control-file DBF path records with preceding TS name records.

    DM stores the authoritative data-file to tablespace relationship in the
    control file.  This intentionally does not derive a tablespace name from the
    DBF filename; it only links strings that are physically present in dm.ctl.
    """

    if not dbf_occurrences:
        return []

    candidates = [
        record for record in string_records if _is_tablespace_name_candidate(record)
    ]
    if not candidates:
        return []

    hints: list[dict[str, Any]] = []
    for occurrence in dbf_occurrences:
        file_offset = int(occurrence.get("offset", -1))
        if file_offset < 0:
            continue
        previous = [
            record
            for record in candidates
            if int(record.get("offset", -1)) < file_offset
        ]
        if not previous:
            continue
        candidate = previous[-1]
        ts_offset = int(candidate["offset"])
        distance = file_offset - ts_offset
        if distance > 2048:
            continue
        tablespace_name = str(candidate["text"])
        hints.append(
            {
                "tablespace_name": tablespace_name,
                "tablespace_offset": ts_offset,
                "dbf_ordinal": occurrence.get("ordinal"),
                "dbf_offset": occurrence.get("offset"),
                "dbf_text": occurrence.get("text"),
                "normalized_path": occurrence.get("normalized_path"),
                "basename": occurrence.get("basename"),
                "distance": distance,
            }
        )
    return hints


def _is_tablespace_name_candidate(record: dict[str, Any]) -> bool:
    text = str(record.get("text", ""))
    if not (1 <= len(text) <= 128):
        return False
    if any(char in text for char in "/\\.=:- "):
        return False
    if text.upper() != text:
        return False
    if text in _CONTROL_ATTRIBUTE_STRINGS:
        return False
    return re.fullmatch(r"[A-Z][A-Z0-9_$#]*", text) is not None


def _changed_ranges(
    before: bytes,
    after: bytes,
    *,
    sample_limit: int | None,
) -> list[tuple[int, int]]:
    if sample_limit == 0:
        return []
    max_len = max(len(before), len(after))
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for offset in range(max_len):
        before_byte = before[offset] if offset < len(before) else None
        after_byte = after[offset] if offset < len(after) else None
        if before_byte == after_byte:
            if start is not None:
                ranges.append((start, offset))
                if sample_limit is not None and len(ranges) >= sample_limit:
                    return ranges
                start = None
            continue
        if start is None:
            start = offset
    if start is not None and (sample_limit is None or len(ranges) < sample_limit):
        ranges.append((start, max_len))
    return ranges


def _changed_byte_count(before: bytes, after: bytes) -> int:
    max_len = max(len(before), len(after))
    changed = 0
    for offset in range(max_len):
        before_byte = before[offset] if offset < len(before) else None
        after_byte = after[offset] if offset < len(after) else None
        if before_byte != after_byte:
            changed += 1
    return changed


def _range_record(
    *,
    start: int,
    stop: int,
    before: bytes,
    after: bytes,
    context_bytes: int,
) -> dict[str, Any]:
    context_start = max(0, start - context_bytes)
    context_stop = min(max(len(before), len(after)), stop + context_bytes)
    return {
        "start": start,
        "stop_exclusive": stop,
        "length": stop - start,
        "context_start": context_start,
        "context_stop_exclusive": context_stop,
        "before_hex": before[context_start:min(context_stop, len(before))].hex(),
        "after_hex": after[context_start:min(context_stop, len(after))].hex(),
        "numeric_candidates": _numeric_diff_candidates(
            before=before,
            after=after,
            start=start,
            stop=stop,
        ),
    }


def _numeric_diff_candidates(
    *,
    before: bytes,
    after: bytes,
    start: int,
    stop: int,
    max_candidates: int = 32,
) -> list[dict[str, Any]]:
    max_len = min(len(before), len(after))
    window_start = max(0, start - 8)
    window_stop = min(max_len, stop + 8)
    candidates: list[dict[str, Any]] = []
    for offset in range(window_start, window_stop):
        for size in (2, 4, 8):
            if len(candidates) >= max_candidates:
                return candidates
            if offset + size > max_len:
                continue
            before_value = int.from_bytes(before[offset:offset + size], "little")
            after_value = int.from_bytes(after[offset:offset + size], "little")
            if before_value == after_value:
                continue
            candidates.append(
                {
                    "offset": offset,
                    "size": size,
                    "endian": "little",
                    "before_unsigned": before_value,
                    "after_unsigned": after_value,
                }
            )
    return candidates


def _extract_printable_string_records(
    data: bytes,
    *,
    min_length: int = 4,
) -> list[dict[str, Any]]:
    strings: list[dict[str, Any]] = []
    current = bytearray()
    current_offset = 0
    for offset, byte in enumerate(data):
        if 32 <= byte <= 126:
            if not current:
                current_offset = offset
            current.append(byte)
            continue
        if len(current) >= min_length:
            strings.append(
                {
                    "offset": current_offset,
                    "length": len(current),
                    "text": current.decode("ascii"),
                }
            )
        current.clear()
    if len(current) >= min_length:
        strings.append(
            {
                "offset": current_offset,
                "length": len(current),
                "text": current.decode("ascii"),
            }
        )
    return strings
