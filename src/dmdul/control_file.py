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
