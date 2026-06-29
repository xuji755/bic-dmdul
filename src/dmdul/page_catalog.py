from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .page import ObservedPageHeader
from .row import scan_observed_row_chain
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
    type_counts: Counter[str] = Counter()
    type_kind_counts: dict[str, Counter[str]] = {}
    kind_type_counts: dict[str, Counter[str]] = {}
    group_counts: Counter[str] = Counter()
    mismatch_pages: list[dict[str, Any]] = []
    nonzero_samples: list[dict[str, Any]] = []
    ref_samples: list[dict[str, Any]] = []
    out_of_range_refs: list[dict[str, Any]] = []
    row_area_summary = _new_row_area_summary()
    zero_pages = 0

    for page_no in range(start_page, stop_page):
        page = data_file.read_page(page_no)
        if _is_all_zero(page):
            zero_pages += 1
            kind_counts["zero"] += 1
            type_counts["zero"] += 1
            _increment_nested(type_kind_counts, "zero", "zero")
            _increment_nested(kind_type_counts, "zero", "zero")
            continue

        header = ObservedPageHeader.from_page(page)
        kind_key = f"0x{header.page_kind_raw:08x}"
        type_key = f"0x{header.page_type_raw:02x}"
        group_key = str(header.group_id)
        kind_counts[kind_key] += 1
        type_counts[type_key] += 1
        _increment_nested(type_kind_counts, type_key, kind_key)
        _increment_nested(kind_type_counts, kind_key, type_key)
        group_counts[group_key] += 1

        summary = _page_summary(page_no=page_no, header=header, page=page)
        _accumulate_row_area_summary(
            row_area_summary,
            page_no=page_no,
            header=header,
            probe=summary["row_area_probe"],
            sample_limit=sample_limit,
        )
        if len(nonzero_samples) < sample_limit:
            nonzero_samples.append(summary)
        if not header.prev_page.is_null or not header.next_page.is_null:
            if len(ref_samples) < sample_limit:
                ref_samples.append(summary)
            for direction, page_ref in (
                ("prev", header.prev_page),
                ("next", header.next_page),
            ):
                if len(out_of_range_refs) >= sample_limit:
                    break
                if page_ref.page_no is None:
                    continue
                same_file_hint = page_ref.file_no == header.file_no_hint
                if same_file_hint and page_ref.page_no >= pages_total:
                    out_of_range_refs.append(
                        {
                            "source_page_no": page_no,
                            "source_header_page_no": header.page_no,
                            "source_file_no_hint": header.file_no_hint,
                            "direction": direction,
                            "ref_file_no": page_ref.file_no,
                            "ref_page_no": page_ref.page_no,
                            "same_file_hint": same_file_hint,
                            "pages_total": pages_total,
                            "page_kind_raw": header.page_kind_raw,
                            "page_kind_label": header.page_kind_label,
                        }
                    )
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
        "page_type_counts": dict(sorted(type_counts.items())),
        "page_type_kind_counts": _sorted_nested_counts(type_kind_counts),
        "page_kind_type_counts": _sorted_nested_counts(kind_type_counts),
        "row_area_summary": _finalize_row_area_summary(row_area_summary),
        "group_id_counts": dict(sorted(group_counts.items(), key=lambda item: int(item[0]))),
        "page_no_mismatches": mismatch_pages,
        "reference_out_of_range": out_of_range_refs,
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
        "page_type_raw": header.page_type_raw,
        "group_id": header.group_id,
        "file_no_hint": header.file_no_hint,
        "page_kind_raw": header.page_kind_raw,
        "page_kind_label": header.page_kind_label,
        "prev_page": str(header.prev_page),
        "next_page": str(header.next_page),
        "field_20_u32le": header.field_20_u32le,
        "field_24_u16le": header.field_24_u16le,
        "field_26_u16le": header.field_26_u16le,
        "field_2c_u16le": header.field_2c_u16le,
        "observed_row_count": header.observed_row_count,
        "row_area_probe": _row_area_probe(header=header, page=page),
        "nonzero_bytes": sum(1 for byte in page if byte != 0),
        "header_hex": page[:64].hex(),
    }


def _is_all_zero(page: bytes) -> bool:
    return all(byte == 0 for byte in page)


def _row_area_probe(
    *,
    header: ObservedPageHeader,
    page: bytes,
    start_offset: int = 0x62,
    sample_limit: int = 16,
) -> dict[str, Any]:
    rows = scan_observed_row_chain(page, start_offset=start_offset)
    live_rows = sum(1 for row in rows if not row.is_deleted)
    deleted_rows = sum(1 for row in rows if row.is_deleted)
    return {
        "start_offset": start_offset,
        "header_observed_row_count": header.observed_row_count,
        "physical_rows_scanned": len(rows),
        "live_rows_scanned": live_rows,
        "deleted_rows_scanned": deleted_rows,
        "count_delta_physical_minus_header": len(rows) - header.observed_row_count,
        "sampled_rows": [
            {
                "page_offset": row.page_offset,
                "length": row.length,
                "deleted": row.is_deleted,
            }
            for row in rows[:sample_limit]
        ],
    }


def _new_row_area_summary() -> dict[str, Any]:
    return {
        "start_offset": 0x62,
        "included_page_kind_label": "tentative-btree-data",
        "included_pages": 0,
        "pages_with_physical_rows": 0,
        "pages_with_deleted_rows": 0,
        "pages_with_count_delta": 0,
        "total_header_observed_row_count": 0,
        "total_physical_rows_scanned": 0,
        "total_live_rows_scanned": 0,
        "total_deleted_rows_scanned": 0,
        "count_delta_histogram": Counter(),
        "count_delta_samples": [],
        "deleted_row_samples": [],
    }


def _accumulate_row_area_summary(
    summary: dict[str, Any],
    *,
    page_no: int,
    header: ObservedPageHeader,
    probe: dict[str, Any],
    sample_limit: int,
) -> None:
    if header.page_kind_label != summary["included_page_kind_label"]:
        return

    delta = probe["count_delta_physical_minus_header"]
    deleted_rows = probe["deleted_rows_scanned"]
    physical_rows = probe["physical_rows_scanned"]

    summary["included_pages"] += 1
    summary["total_header_observed_row_count"] += probe["header_observed_row_count"]
    summary["total_physical_rows_scanned"] += physical_rows
    summary["total_live_rows_scanned"] += probe["live_rows_scanned"]
    summary["total_deleted_rows_scanned"] += deleted_rows
    summary["count_delta_histogram"][str(delta)] += 1
    if physical_rows:
        summary["pages_with_physical_rows"] += 1
    if deleted_rows:
        summary["pages_with_deleted_rows"] += 1
        if len(summary["deleted_row_samples"]) < sample_limit:
            summary["deleted_row_samples"].append(
                _row_area_page_sample(page_no=page_no, header=header, probe=probe)
            )
    if delta:
        summary["pages_with_count_delta"] += 1
        if len(summary["count_delta_samples"]) < sample_limit:
            summary["count_delta_samples"].append(
                _row_area_page_sample(page_no=page_no, header=header, probe=probe)
            )


def _row_area_page_sample(
    *,
    page_no: int,
    header: ObservedPageHeader,
    probe: dict[str, Any],
) -> dict[str, Any]:
    return {
        "page_no": page_no,
        "header_page_no": header.page_no,
        "page_type_raw": header.page_type_raw,
        "page_kind_raw": header.page_kind_raw,
        "page_kind_label": header.page_kind_label,
        "header_observed_row_count": probe["header_observed_row_count"],
        "physical_rows_scanned": probe["physical_rows_scanned"],
        "live_rows_scanned": probe["live_rows_scanned"],
        "deleted_rows_scanned": probe["deleted_rows_scanned"],
        "count_delta_physical_minus_header": probe[
            "count_delta_physical_minus_header"
        ],
        "sampled_rows": probe["sampled_rows"],
    }


def _finalize_row_area_summary(summary: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(summary)
    finalized["count_delta_histogram"] = dict(
        sorted(
            summary["count_delta_histogram"].items(),
            key=lambda item: int(item[0]),
        )
    )
    return finalized


def _increment_nested(
    counts: dict[str, Counter[str]],
    outer_key: str,
    inner_key: str,
) -> None:
    if outer_key not in counts:
        counts[outer_key] = Counter()
    counts[outer_key][inner_key] += 1


def _sorted_nested_counts(counts: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {
        outer_key: dict(sorted(inner_counts.items()))
        for outer_key, inner_counts in sorted(counts.items())
    }
