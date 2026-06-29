from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .page import ObservedPageHeader
from .storage import DataFile


def catalog_data_file_pages(
    *,
    path: Path,
    page_size: int = 8192,
    start_page: int = 0,
    max_pages: int | None = None,
    sample_limit: int = 32,
) -> dict[str, Any]:
    """Build a conservative page catalog for one DM data file.

    This scanner records observed page identity and kind fields without
    assigning semantic names to unknown page kinds. It is intended for storage
    exploration and fixture comparison.
    """

    if start_page < 0:
        raise ValueError("start_page must be non-negative")
    if max_pages is not None and max_pages < 0:
        raise ValueError("max_pages must be non-negative")
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")

    data_file = DataFile(path, page_size=page_size)
    stat = path.stat()
    pages_total = stat.st_size // page_size
    stop_page = pages_total if max_pages is None else min(pages_total, start_page + max_pages)

    kind_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()
    mismatch_pages: list[dict[str, Any]] = []
    nonzero_samples: list[dict[str, Any]] = []
    ref_samples: list[dict[str, Any]] = []
    zero_pages = 0

    for page_no in range(start_page, stop_page):
        page = data_file.read_page(page_no)
        if _is_all_zero(page):
            zero_pages += 1
            kind_counts["zero"] += 1
            continue

        header = ObservedPageHeader.from_page(page)
        kind_key = f"0x{header.page_kind_raw:08x}"
        group_key = str(header.group_id)
        kind_counts[kind_key] += 1
        group_counts[group_key] += 1

        summary = _page_summary(page_no=page_no, header=header, page=page)
        if len(nonzero_samples) < sample_limit:
            nonzero_samples.append(summary)
        if not header.prev_page.is_null or not header.next_page.is_null:
            if len(ref_samples) < sample_limit:
                ref_samples.append(summary)
        if header.page_no != page_no and len(mismatch_pages) < sample_limit:
            mismatch_pages.append(summary)

    scanned_pages = max(0, stop_page - start_page)
    return {
        "file": str(path),
        "bytes": stat.st_size,
        "page_size": page_size,
        "pages_total": pages_total,
        "trailing_bytes": stat.st_size % page_size,
        "scan": {
            "start_page": start_page,
            "stop_page_exclusive": stop_page,
            "scanned_pages": scanned_pages,
            "sample_limit": sample_limit,
        },
        "zero_pages": zero_pages,
        "nonzero_pages": scanned_pages - zero_pages,
        "page_kind_counts": dict(sorted(kind_counts.items())),
        "group_id_counts": dict(sorted(group_counts.items(), key=lambda item: int(item[0]))),
        "page_no_mismatches": mismatch_pages,
        "nonzero_samples": nonzero_samples,
        "reference_samples": ref_samples,
    }


def _page_summary(
    *,
    page_no: int,
    header: ObservedPageHeader,
    page: bytes,
) -> dict[str, Any]:
    return {
        "page_no": page_no,
        "header_page_no": header.page_no,
        "group_raw": header.group_raw,
        "group_id": header.group_id,
        "file_no_hint": header.file_no_hint,
        "page_kind_raw": header.page_kind_raw,
        "prev_page": str(header.prev_page),
        "next_page": str(header.next_page),
        "observed_row_count": header.observed_row_count,
        "nonzero_bytes": sum(1 for byte in page if byte != 0),
        "header_hex": page[:64].hex(),
    }


def _is_all_zero(page: bytes) -> bool:
    return all(byte == 0 for byte in page)
