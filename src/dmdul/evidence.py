from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .page import ObservedPageHeader, format_hex_dump
from .storage import DataFile


def capture_data_file_evidence(
    *,
    path: Path,
    page_size: int = 8192,
    pages: tuple[int, ...] = (),
    markers: tuple[str, ...] = (),
    marker_encoding: str = "utf-8",
    marker_context: int = 64,
    label: str | None = None,
    copy_state: str | None = None,
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Capture reproducible raw-page evidence from one DM data file.

    The result is intentionally descriptive, not interpretive. It records page
    identity fields already observed elsewhere, page hashes, and marker
    locations so later research notes can point back to exact bytes.
    """

    data_file = DataFile(path, page_size=page_size)
    stat = path.stat()
    return {
        "label": label,
        "copy_state": copy_state,
        "notes": list(notes),
        "file": str(path),
        "bytes": stat.st_size,
        "page_size": page_size,
        "pages_total": stat.st_size // page_size,
        "trailing_bytes": stat.st_size % page_size,
        "captured_pages": [
            _capture_page(data_file, page_no=page_no) for page_no in pages
        ],
        "markers": [
            _capture_marker(
                data_file,
                marker=marker,
                encoding=marker_encoding,
                context_bytes=marker_context,
            )
            for marker in markers
        ],
    }


def parse_page_selection(spec: str) -> tuple[int, ...]:
    """Parse comma-separated page numbers and inclusive ranges.

    Example: `0,1,16,96-98`.
    """

    if not spec:
        return ()
    pages: list[int] = []
    for part in spec.split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start < 0 or end < start:
                raise ValueError(f"invalid page range: {item}")
            pages.extend(range(start, end + 1))
        else:
            page_no = int(item)
            if page_no < 0:
                raise ValueError(f"invalid page number: {item}")
            pages.append(page_no)
    return tuple(dict.fromkeys(pages))


def _capture_page(data_file: DataFile, *, page_no: int) -> dict[str, Any]:
    page = data_file.read_page(page_no)
    header = ObservedPageHeader.from_page(page)
    return {
        "page_no": page_no,
        "file_offset": page_no * data_file.page_size,
        "sha256": hashlib.sha256(page).hexdigest(),
        "is_all_zero": all(byte == 0 for byte in page),
        "nonzero_bytes": sum(1 for byte in page if byte != 0),
        "observed_header": header.as_dict(),
        "header_hex": page[:64].hex(),
        "header_dump": format_hex_dump(
            page[:64],
            base_offset=page_no * data_file.page_size,
        ),
    }


def _capture_marker(
    data_file: DataFile,
    *,
    marker: str,
    encoding: str,
    context_bytes: int,
) -> dict[str, Any]:
    marker_bytes = marker.encode(encoding)
    matches = []
    with data_file.path.open("rb") as file:
        for match in data_file.find(marker_bytes):
            context_start = max(0, match.offset - context_bytes)
            context_end = match.offset + len(marker_bytes) + context_bytes
            file.seek(context_start)
            context = file.read(context_end - context_start)
            matches.append(
                {
                    "offset": match.offset,
                    "page_no": match.page_no,
                    "page_offset": match.page_offset,
                    "context_start": context_start,
                    "context_hex": context.hex(),
                    "context_dump": format_hex_dump(
                        context,
                        base_offset=context_start,
                    ),
                }
            )
    return {
        "marker": marker,
        "encoding": encoding,
        "matches": matches,
    }
