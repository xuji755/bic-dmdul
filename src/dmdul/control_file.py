from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


_DBF_HINT_RE = re.compile(r"[A-Za-z0-9_./\\:-]*\.DBF", re.IGNORECASE)


def summarize_control_file(
    path: Path,
    *,
    sample_limit: int = 32,
) -> dict[str, Any]:
    """Summarize a DM control file without assuming its binary layout."""

    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")

    data = path.read_bytes()
    string_records = _extract_printable_string_records(data)
    dbf_hints: list[str] = []
    dbf_hint_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in string_records:
        text = str(record["text"])
        for match in _DBF_HINT_RE.finditer(text):
            hint = match.group(0)
            if hint not in seen:
                seen.add(hint)
                dbf_hints.append(hint)
                dbf_hint_records.append(
                    {
                        "text": hint,
                        "offset": int(record["offset"]) + match.start(),
                        "length": len(hint),
                        "string_offset": record["offset"],
                    }
                )
            if len(dbf_hints) >= sample_limit:
                break
        if len(dbf_hints) >= sample_limit:
            break
    sampled_strings = string_records[:sample_limit]

    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dbf_path_hints": dbf_hints,
        "dbf_path_hint_records": dbf_hint_records,
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
