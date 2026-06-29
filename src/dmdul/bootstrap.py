from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database_summary import summarize_database_dir


DICT_FILENAMES = ("file.dict", "user.dict", "tab.dict", "col.dict")


def build_bootstrap_dicts(
    *,
    database_dir: Path,
    output_dir: Path,
    page_size: int = 8192,
    catalog_pages: int = 0,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Write first-stage bootstrap dictionary artifacts.

    The current implementation can build `file.dict` from `dm.ctl` evidence and
    DBF page-0 headers. User/table/column dictionaries are created as explicit
    empty artifacts until SYSTEM dictionary rows are decoded without heuristics.
    """

    summary = summarize_database_dir(
        database_dir=database_dir,
        page_size=page_size,
        catalog_pages=catalog_pages,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    dict_paths = {name: output_dir / name for name in DICT_FILENAMES}
    file_rows = _file_dict_rows(summary)
    _write_jsonl(dict_paths["file.dict"], file_rows)
    for name in ("user.dict", "tab.dict", "col.dict"):
        _write_jsonl(dict_paths[name], ())

    manifest = {
        "mode": "dm-bootstrap-dicts",
        "database_dir": str(database_dir),
        "page_size": page_size,
        "dict_files": {name: str(path) for name, path in dict_paths.items()},
        "rows": {
            "file.dict": len(file_rows),
            "user.dict": 0,
            "tab.dict": 0,
            "col.dict": 0,
        },
        "steps": [
            {
                "step": 1,
                "name": "read-control-file-and-data-files",
                "status": "ok" if file_rows else "incomplete",
                "output": "file.dict",
            },
            {
                "step": 2,
                "name": "locate-system-dictionary-tables",
                "status": "heuristic-only",
                "output": None,
            },
            {
                "step": 3,
                "name": "dump-user-table-column-dictionaries",
                "status": "not-yet-implemented",
                "output": ["user.dict", "tab.dict", "col.dict"],
            },
        ],
        "diagnostics": _bootstrap_diagnostics(summary, file_rows),
        "database_summary": summary,
    }
    manifest_path = output_dir / "bootstrap_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _file_dict_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal, item in enumerate(summary.get("files", ()), start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "dict_type": "file",
                "ordinal": ordinal,
                "path": item.get("path"),
                "basename": Path(str(item.get("path", ""))).name,
                "bytes": item.get("bytes"),
                "page_size": item.get("page_size"),
                "pages": item.get("pages"),
                "group_id": item.get("group_id"),
                "file_no": item.get("file_no_hint"),
                "page_type_raw": item.get("page_type_raw"),
                "page0_kind_raw": item.get("page0_kind_raw"),
                "page0_kind_label": item.get("page0_kind_label"),
                "system_candidate": item.get("system_candidate"),
                "control_file_entries": _matched_control_entries(summary, item),
            }
        )
    return rows


def _matched_control_entries(
    summary: dict[str, Any],
    file_entry: dict[str, Any],
) -> list[dict[str, Any]]:
    target_path = file_entry.get("path")
    manifest = summary.get("control_file_data_files")
    if not isinstance(manifest, dict):
        return []
    matches: list[dict[str, Any]] = []
    for entry in manifest.get("entries", ()):
        if not isinstance(entry, dict):
            continue
        matched_paths = entry.get("matched_paths")
        if not isinstance(matched_paths, list) or target_path not in matched_paths:
            continue
        matches.append(
            {
                "control_file_ordinal": entry.get("control_file_ordinal"),
                "source_control_file": entry.get("source_control_file"),
                "offset": entry.get("offset"),
                "text": entry.get("text"),
                "normalized_path": entry.get("normalized_path"),
                "basename": entry.get("basename"),
            }
        )
    return matches


def _bootstrap_diagnostics(
    summary: dict[str, Any],
    file_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not file_rows:
        diagnostics.append(
            {
                "level": "error",
                "code": "bootstrap-no-data-files",
                "message": "no DBF files were available for file.dict",
            }
        )
    diagnostics.append(
        {
            "level": "warning",
            "code": "bootstrap-sys-dictionaries-not-decoded",
            "message": "user.dict, tab.dict, and col.dict are empty until SYSTEM dictionary table rows are decoded",
        }
    )
    summary_diagnostics = summary.get("diagnostics")
    if isinstance(summary_diagnostics, dict):
        counts = summary_diagnostics.get("counts_by_code")
        if isinstance(counts, dict):
            for code, count in sorted(counts.items()):
                diagnostics.append(
                    {
                        "level": "info",
                        "code": f"database-summary-{code}",
                        "count": count,
                    }
                )
    return diagnostics


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")
