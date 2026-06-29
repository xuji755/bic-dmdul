from __future__ import annotations

from pathlib import Path
from typing import Any

from .page import ObservedPageHeader, ObservedPageRef
from .storage import DataFile


def analyze_segment_root(
    *,
    path: Path,
    page_size: int,
    group_id: int,
    file_no: int,
    root_page: int,
    known_file_nos: set[int] | None = None,
    sample_limit: int = 64,
) -> dict[str, Any]:
    """Capture conservative evidence from a table segment root page.

    The page-reference scan is intentionally evidence-oriented. It records
    plausible 6-byte file/page references found in the root page body, but does
    not claim that they are decoded BTREE child pointers or extent-map entries.
    """

    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")

    data_file = DataFile(path, page_size=page_size)
    stat = path.stat()
    pages_total = stat.st_size // page_size
    diagnostics: list[dict[str, Any]] = []

    if root_page < 0 or root_page >= pages_total:
        return {
            "data_file": str(path),
            "page_size": page_size,
            "pages_total": pages_total,
            "expected": {
                "group_id": group_id,
                "file_no": file_no,
                "root_page": root_page,
            },
            "root_header": None,
            "identity_ok": False,
            "candidate_page_refs_total": 0,
            "candidate_page_refs": [],
            "diagnostics": [
                {
                    "level": "error",
                    "code": "segment-root-out-of-range",
                    "message": "segment root page is outside the data file",
                    "root_page": root_page,
                    "pages_total": pages_total,
                }
            ],
        }

    page = data_file.read_page(root_page)
    header = ObservedPageHeader.from_page(page)
    identity_ok = (
        header.group_id == group_id
        and header.file_no_hint == file_no
        and header.page_no == root_page
    )
    if header.group_id != group_id:
        diagnostics.append(
            {
                "level": "error",
                "code": "segment-root-group-mismatch",
                "message": "segment root page group id does not match SYSINDEXES",
                "expected": group_id,
                "observed": header.group_id,
            }
        )
    if header.file_no_hint != file_no:
        diagnostics.append(
            {
                "level": "error",
                "code": "segment-root-file-mismatch",
                "message": "segment root page file hint does not match SYSINDEXES",
                "expected": file_no,
                "observed": header.file_no_hint,
            }
        )
    if header.page_no != root_page:
        diagnostics.append(
            {
                "level": "error",
                "code": "segment-root-page-mismatch",
                "message": "segment root page header page number does not match SYSINDEXES root page",
                "expected": root_page,
                "observed": header.page_no,
            }
        )
    if header.page_kind_raw not in {0x14, 0x15}:
        diagnostics.append(
            {
                "level": "warning",
                "code": "segment-root-kind-unexpected",
                "message": "segment root page kind is not one of the currently observed table root kinds",
                "page_kind_raw": header.page_kind_raw,
                "page_kind_label": header.page_kind_label,
            }
        )

    candidate_refs = _candidate_page_refs(
        page=page,
        data_file=data_file,
        current_file_no=file_no,
        root_page=root_page,
        pages_total=pages_total,
        known_file_nos=known_file_nos or {file_no},
        sample_limit=sample_limit,
    )
    diagnostics.extend(candidate_refs["diagnostics"])
    return {
        "data_file": str(path),
        "page_size": page_size,
        "pages_total": pages_total,
        "expected": {
            "group_id": group_id,
            "file_no": file_no,
            "root_page": root_page,
        },
        "root_header": header.as_dict(),
        "identity_ok": identity_ok,
        "candidate_page_refs_total": candidate_refs["total"],
        "candidate_page_refs": candidate_refs["samples"],
        "diagnostics": diagnostics,
    }


def _candidate_page_refs(
    *,
    page: bytes,
    data_file: DataFile,
    current_file_no: int,
    root_page: int,
    pages_total: int,
    known_file_nos: set[int],
    sample_limit: int,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    total = 0
    seen: set[tuple[int, int, int]] = set()
    for offset in range(64, max(64, len(page) - 5)):
        raw = page[offset : offset + 6]
        if raw in {b"\0" * 6, b"\xff" * 6}:
            continue
        page_ref = ObservedPageRef(raw)
        if page_ref.file_no is None or page_ref.page_no is None:
            continue
        if page_ref.file_no not in known_file_nos:
            continue
        if page_ref.page_no <= 0 or page_ref.page_no >= pages_total:
            continue
        if page_ref.page_no == root_page:
            continue
        key = (offset, page_ref.file_no, page_ref.page_no)
        if key in seen:
            continue
        seen.add(key)
        total += 1
        if len(samples) < sample_limit:
            samples.append(
                _candidate_ref_record(
                    offset=offset,
                    page_ref=page_ref,
                    data_file=data_file,
                    current_file_no=current_file_no,
                )
            )
    return {
        "total": total,
        "samples": samples,
        "diagnostics": _candidate_ref_diagnostics(samples),
    }


def _candidate_ref_record(
    *,
    offset: int,
    page_ref: ObservedPageRef,
    data_file: DataFile,
    current_file_no: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "offset": offset,
        "raw_hex": page_ref.raw.hex(),
        "file_no": page_ref.file_no,
        "page_no": page_ref.page_no,
    }
    if page_ref.file_no == current_file_no and page_ref.page_no is not None:
        target_page = data_file.read_page(page_ref.page_no)
        if _is_all_zero(target_page):
            record["target_page_kind_raw"] = None
            record["target_page_kind_label"] = "zero"
        else:
            target_header = ObservedPageHeader.from_page(target_page)
            record["target_page_kind_raw"] = target_header.page_kind_raw
            record["target_page_kind_label"] = target_header.page_kind_label
            record["target_header_page_no"] = target_header.page_no
    return record


def _candidate_ref_diagnostics(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    non_data_refs = [
        _candidate_ref_summary(item)
        for item in samples
        if item.get("target_page_kind_label") is not None
        and item.get("target_page_kind_label") != "tentative-btree-data"
    ]
    if non_data_refs:
        diagnostics.append(
            {
                "level": "warning",
                "code": "segment-root-candidate-ref-non-data-page",
                "message": "one or more sampled segment-root page references point to pages not currently classified as BTREE data pages",
                "count": len(non_data_refs),
                "refs": non_data_refs,
            }
        )
    unread_refs = [
        _candidate_ref_summary(item)
        for item in samples
        if item.get("target_page_kind_label") is None
    ]
    if unread_refs:
        diagnostics.append(
            {
                "level": "warning",
                "code": "segment-root-candidate-ref-target-unread",
                "message": "one or more sampled segment-root page references point to files that were not read during root analysis",
                "count": len(unread_refs),
                "refs": unread_refs,
            }
        )
    return diagnostics


def _candidate_ref_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "offset": item.get("offset"),
        "file_no": item.get("file_no"),
        "page_no": item.get("page_no"),
    }
    if "target_page_kind_label" in item:
        summary["target_page_kind_label"] = item.get("target_page_kind_label")
    return summary


def _is_all_zero(page: bytes) -> bool:
    return all(byte == 0 for byte in page)
