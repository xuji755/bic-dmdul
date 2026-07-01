from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_map import build_control_ctl
from .discovery import discover_data_files


DEFAULT_INIT_VALUES = {
    "filelist": "filelist.dul",
    "dirlist": ".",
    "diskgroups": "",
    "output_dir": "dulout",
    "dict_dir": "dulout",
    "parallel": "1",
    "page_size": "8192",
    "data_delimiter": "|",
}


@dataclass(frozen=True)
class FileListEntry:
    group_id: int
    file_id: int
    path: Path


@dataclass(frozen=True)
class DulRuntimeConfig:
    init_file: Path | None
    values: dict[str, str]

    @property
    def page_size(self) -> int:
        return int(self.values.get("page_size", DEFAULT_INIT_VALUES["page_size"]))

    @property
    def filelist(self) -> Path:
        return Path(self.values.get("filelist", DEFAULT_INIT_VALUES["filelist"]))

    @property
    def dirlist(self) -> tuple[Path, ...]:
        value = self.values.get("dirlist", "")
        return tuple(Path(item.strip()) for item in value.split(",") if item.strip())

    @property
    def output_dir(self) -> Path:
        return Path(self.values.get("output_dir", DEFAULT_INIT_VALUES["output_dir"]))

    @property
    def dict_dir(self) -> Path:
        return Path(self.values.get("dict_dir", self.values.get("output_dir", DEFAULT_INIT_VALUES["dict_dir"])))

    @property
    def parallel(self) -> int:
        return max(1, int(self.values.get("parallel", DEFAULT_INIT_VALUES["parallel"])))

    @property
    def data_delimiter(self) -> str:
        value = self.values.get("data_delimiter", DEFAULT_INIT_VALUES["data_delimiter"])
        return value[:1] or "|"


def read_init_dul(path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    if not path.exists():
        return config
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("--"):
            stripped = stripped[2:]
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        config[key.strip().lower().replace("-", "_")] = value.strip().strip('"').strip("'")
    return config


def write_init_dul(path: Path, values: dict[str, str] | None = None) -> None:
    merged = dict(DEFAULT_INIT_VALUES)
    if values:
        merged.update({key: str(value) for key, value in values.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"--{key}={value}" for key, value in merged.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_runtime_config(init_file: Path | None) -> DulRuntimeConfig:
    values = dict(DEFAULT_INIT_VALUES)
    resolved_init: Path | None = None
    candidates = [init_file] if init_file is not None else [Path("init.dul")]
    for candidate in candidates:
        if candidate is None:
            continue
        loaded = read_init_dul(candidate)
        if loaded:
            values.update(loaded)
            resolved_init = candidate
            break
    return DulRuntimeConfig(init_file=resolved_init, values=values)


def read_filelist(path: Path) -> tuple[FileListEntry, ...]:
    if not path.exists():
        return ()
    entries: list[FileListEntry] = []
    with path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.reader(file):
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 3:
                continue
            entries.append(
                FileListEntry(
                    group_id=int(row[0].strip()),
                    file_id=int(row[1].strip()),
                    path=Path(row[2].strip()),
                )
            )
    return tuple(entries)


def write_filelist(path: Path, entries: tuple[FileListEntry, ...] | list[FileListEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        for entry in sorted(entries, key=lambda item: (item.group_id, item.file_id, str(item.path))):
            writer.writerow([entry.group_id, entry.file_id, str(entry.path)])


def build_filelist_from_dirs(
    *,
    dirlist: tuple[Path, ...],
    page_size: int,
) -> tuple[FileListEntry, ...]:
    entries: list[FileListEntry] = []
    seen: set[Path] = set()
    for directory in dirlist:
        for item in discover_data_files(directory, page_size=page_size):
            if item.path in seen:
                continue
            seen.add(item.path)
            entries.append(
                FileListEntry(
                    group_id=item.group_id,
                    file_id=item.file_no_hint,
                    path=item.path,
                )
            )
    return tuple(entries)


def build_filelist_from_database_dir(
    *,
    database_dir: Path,
    page_size: int,
    sample_limit: int = 8,
) -> tuple[FileListEntry, ...]:
    manifest = build_control_ctl(
        database_dir=database_dir,
        page_size=page_size,
        sample_limit=sample_limit,
    )
    return tuple(
        FileListEntry(
            group_id=int(row["tablespace_id"]),
            file_id=int(row["file_id"]),
            path=Path(str(row["path"])),
        )
        for row in manifest["rows"]
    )


def validate_filelist(entries: tuple[FileListEntry, ...]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for entry in entries:
        key = (entry.group_id, entry.file_id)
        if key in seen:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "filelist-duplicate-group-file",
                    "group_id": entry.group_id,
                    "file_id": entry.file_id,
                }
            )
        seen.add(key)
        if not entry.path.exists():
            diagnostics.append(
                {
                    "level": "error",
                    "code": "filelist-file-not-found",
                    "group_id": entry.group_id,
                    "file_id": entry.file_id,
                    "path": str(entry.path),
                }
            )
    if not entries:
        diagnostics.append(
            {
                "level": "error",
                "code": "filelist-empty",
                "message": "filelist.dul contains no data files",
            }
        )
    return diagnostics
