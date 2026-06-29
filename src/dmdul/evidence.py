from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .page import ObservedPageHeader, format_hex_dump
from .storage import DataFile


VALID_COPY_STATES = frozenset(
    {
        "clean-shutdown",
        "storage-snapshot",
        "live-copy",
        "crash-state",
        "open-transaction",
        "unknown",
    }
)


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


def verify_evidence_manifest(path: Path) -> dict[str, Any]:
    """Verify a captured evidence manifest and referenced local files."""

    root = path.parent
    with path.open("r", encoding="utf-8") as file:
        manifest = json.load(file)

    errors: list[str] = []
    warnings: list[str] = []

    label = manifest.get("label")
    if not isinstance(label, str) or not label:
        errors.append("manifest label is required")

    copy_state = manifest.get("copy_state")
    if copy_state not in VALID_COPY_STATES:
        errors.append(f"invalid copy_state: {copy_state!r}")
    elif copy_state == "unknown":
        warnings.append("copy_state is unknown; evidence cannot prove consistency")

    copied_files = manifest.get("copied_files")
    if not isinstance(copied_files, list) or not copied_files:
        errors.append("copied_files must contain at least one file entry")
    elif isinstance(copied_files, list):
        for index, item in enumerate(copied_files):
            _verify_manifest_file_entry(
                root=root,
                item=item,
                index=index,
                errors=errors,
                warnings=warnings,
            )

    evidence_json = manifest.get("evidence_json")
    if not isinstance(evidence_json, list) or not evidence_json:
        errors.append("evidence_json must contain at least one capture file")
    elif isinstance(evidence_json, list):
        for index, item in enumerate(evidence_json):
            _verify_evidence_json_ref(
                root=root,
                item=item,
                index=index,
                errors=errors,
            )

    reference_output = manifest.get("reference_output", [])
    if not reference_output:
        warnings.append("reference_output is empty; online calibration output is missing")

    return {
        "manifest": str(path),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


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


def _verify_manifest_file_entry(
    *,
    root: Path,
    item: Any,
    index: int,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(item, dict):
        errors.append(f"copied_files[{index}] must be an object")
        return
    value = item.get("path")
    if not isinstance(value, str) or not value:
        errors.append(f"copied_files[{index}].path is required")
        return
    file_path = _manifest_path(root, value)
    if not file_path.exists():
        errors.append(f"copied_files[{index}] does not exist: {file_path}")
        return
    actual_bytes = file_path.stat().st_size
    expected_bytes = item.get("bytes")
    if expected_bytes is None:
        warnings.append(f"copied_files[{index}].bytes is not recorded")
    else:
        try:
            expected_bytes_int = int(expected_bytes)
        except (TypeError, ValueError):
            errors.append(f"copied_files[{index}].bytes must be an integer")
        else:
            if expected_bytes_int != actual_bytes:
                errors.append(
                    f"copied_files[{index}].bytes mismatch: "
                    f"expected={expected_bytes}, actual={actual_bytes}"
                )
    expected_sha256 = item.get("sha256")
    if expected_sha256 is None:
        warnings.append(f"copied_files[{index}].sha256 is not recorded")
    elif str(expected_sha256).lower() != _sha256_file(file_path):
        errors.append(f"copied_files[{index}].sha256 mismatch: {file_path}")


def _verify_evidence_json_ref(
    *,
    root: Path,
    item: Any,
    index: int,
    errors: list[str],
) -> None:
    if not isinstance(item, str) or not item:
        errors.append(f"evidence_json[{index}] must be a path string")
        return
    evidence_path = _manifest_path(root, item)
    if not evidence_path.exists():
        errors.append(f"evidence_json[{index}] does not exist: {evidence_path}")
        return
    try:
        with evidence_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        errors.append(f"evidence_json[{index}] is invalid JSON: {exc}")
        return
    required = ("file", "page_size", "captured_pages", "markers")
    for key in required:
        if key not in payload:
            errors.append(f"evidence_json[{index}] missing key: {key}")


def _manifest_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
