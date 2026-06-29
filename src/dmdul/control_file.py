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
    strings = _extract_printable_strings(data)
    dbf_hints: list[str] = []
    seen: set[str] = set()
    for text in strings:
        for match in _DBF_HINT_RE.finditer(text):
            hint = match.group(0)
            if hint not in seen:
                seen.add(hint)
                dbf_hints.append(hint)
            if len(dbf_hints) >= sample_limit:
                break
        if len(dbf_hints) >= sample_limit:
            break

    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dbf_path_hints": dbf_hints,
        "printable_string_samples": strings[:sample_limit],
    }


def _extract_printable_strings(data: bytes, *, min_length: int = 4) -> list[str]:
    strings: list[str] = []
    current = bytearray()
    for byte in data:
        if 32 <= byte <= 126:
            current.append(byte)
            continue
        if len(current) >= min_length:
            strings.append(current.decode("ascii"))
        current.clear()
    if len(current) >= min_length:
        strings.append(current.decode("ascii"))
    return strings
