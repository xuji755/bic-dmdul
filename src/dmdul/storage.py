from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class MarkerMatch:
    offset: int
    page_no: int
    page_offset: int


@dataclass(frozen=True)
class DataFile:
    path: Path
    page_size: int = 8192

    def read_page(self, page_no: int) -> bytes:
        if page_no < 0:
            raise ValueError("page_no must be non-negative")
        with self.path.open("rb") as file:
            file.seek(page_no * self.page_size)
            data = file.read(self.page_size)
        if len(data) != self.page_size:
            raise EOFError(f"page {page_no} is incomplete or outside the file")
        return data

    def find(self, marker: bytes, *, chunk_size: int = 1024 * 1024) -> Iterator[MarkerMatch]:
        if not marker:
            raise ValueError("marker must not be empty")
        overlap = len(marker) - 1
        offset = 0
        previous = b""
        with self.path.open("rb") as file:
            while True:
                chunk = file.read(chunk_size)
                if not chunk:
                    break
                window = previous + chunk
                search_from = 0
                while True:
                    index = window.find(marker, search_from)
                    if index < 0:
                        break
                    absolute = offset - len(previous) + index
                    yield MarkerMatch(
                        offset=absolute,
                        page_no=absolute // self.page_size,
                        page_offset=absolute % self.page_size,
                    )
                    search_from = index + 1
                previous = window[-overlap:] if overlap else b""
                offset += len(chunk)

