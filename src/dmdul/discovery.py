from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .page import ObservedPageHeader


@dataclass(frozen=True)
class DiscoveredDataFile:
    path: Path
    bytes: int
    page_size: int
    pages: int
    group_raw: int
    page_type_raw: int
    group_id: int
    file_no_hint: int
    page_no: int
    page_kind_raw: int

    @property
    def is_system_candidate(self) -> bool:
        return self.group_id == 0 and self.page_no == 0 and self.page_kind_raw == 0x13


def discover_data_files(
    database_dir: Path,
    *,
    page_size: int = 8192,
) -> list[DiscoveredDataFile]:
    files: list[DiscoveredDataFile] = []
    for path in find_dbf_files(database_dir):
        stat = path.stat()
        if stat.st_size < page_size:
            continue
        with path.open("rb") as file:
            page0 = file.read(page_size)
        try:
            header = ObservedPageHeader.from_page(page0)
        except ValueError:
            continue
        files.append(
            DiscoveredDataFile(
                path=path,
                bytes=stat.st_size,
                page_size=page_size,
                pages=stat.st_size // page_size,
                group_raw=header.group_raw,
                page_type_raw=header.page_type_raw,
                group_id=header.group_id,
                file_no_hint=header.file_no_hint,
                page_no=header.page_no,
                page_kind_raw=header.page_kind_raw,
            )
        )
    return sorted(files, key=lambda item: (item.group_id, item.file_no_hint, str(item.path)))


def find_dbf_files(database_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("*.DBF", "*.dbf"):
        candidates.extend(path for path in database_dir.rglob(pattern) if path.is_file())
    return sorted(set(candidates))


def find_control_files(database_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("dm.ctl", "DM.CTL", "*.ctl", "*.CTL"):
        candidates.extend(path for path in database_dir.rglob(pattern) if path.is_file())
    return sorted(set(candidates))
