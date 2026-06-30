from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .database_summary import summarize_database_dir


def build_control_ctl(
    *,
    database_dir: Path,
    page_size: int = 8192,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Build the extraction-time control.ctl file map.

    The output rows intentionally use paths from the current offline copy, not
    necessarily the original paths embedded in dm.ctl.
    """

    summary = summarize_database_dir(
        database_dir=database_dir,
        page_size=page_size,
        catalog_pages=0,
        sample_limit=sample_limit,
    )
    rows = _control_ctl_rows(summary)
    return {
        "mode": "dm-control-ctl",
        "database_dir": str(database_dir),
        "page_size": page_size,
        "rows": rows,
        "rows_total": len(rows),
        "diagnostics": _diagnostics(summary, rows),
        "database_summary": summary,
    }


def write_control_ctl(
    *,
    database_dir: Path,
    output: Path,
    page_size: int = 8192,
    sample_limit: int = 8,
) -> dict[str, Any]:
    manifest = build_control_ctl(
        database_dir=database_dir,
        page_size=page_size,
        sample_limit=sample_limit,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        for row in manifest["rows"]:
            writer.writerow([row["tablespace_id"], row["file_id"], row["path"]])
    manifest["output"] = str(output)
    return manifest


def _control_ctl_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summary.get("files", ()):
        if not isinstance(item, dict):
            continue
        group_id = item.get("group_id")
        file_no = item.get("file_no_hint")
        path = item.get("path")
        if group_id is None or file_no is None or path is None:
            continue
        rows.append(
            {
                "tablespace_id": group_id,
                "file_id": file_no,
                "path": str(path),
                "system_candidate": bool(item.get("system_candidate")),
                "source": "dbf-page0",
            }
        )
    return sorted(
        rows,
        key=lambda row: (row["tablespace_id"], row["file_id"], row["path"]),
    )


def _diagnostics(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not rows:
        diagnostics.append(
            {
                "level": "error",
                "code": "control-ctl-no-data-files",
                "message": "no data files were available for control.ctl",
            }
        )
    if not summary.get("control_files"):
        diagnostics.append(
            {
                "level": "warning",
                "code": "control-ctl-without-dm-ctl",
                "message": "control.ctl was generated from DBF page0 headers because no dm.ctl file was found",
            }
        )
    if not any(row.get("system_candidate") for row in rows):
        diagnostics.append(
            {
                "level": "warning",
                "code": "control-ctl-system-file-not-found",
                "message": "SYSTEM data file was not identified in the generated control.ctl rows",
            }
        )
    return diagnostics
