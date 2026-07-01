from __future__ import annotations

from dataclasses import dataclass


OBSERVED_PAGE_KIND_LABELS = {
    0x00000011: "tentative-space-bitmap",
    0x00000013: "tentative-file-control",
    0x00000014: "tentative-btree-data",
    0x00000015: "tentative-segment-root",
    0x1A1A001A: "tentative-internal-metadata",
    0xFFFF00FF: "tentative-empty-initialized",
}


@dataclass(frozen=True)
class ObservedPageRef:
    raw: bytes

    @property
    def is_null(self) -> bool:
        return self.raw == b"\xff" * 6

    @property
    def file_no(self) -> int | None:
        if self.is_null:
            return None
        return int.from_bytes(self.raw[0:2], "little")

    @property
    def page_no(self) -> int | None:
        if self.is_null:
            return None
        return int.from_bytes(self.raw[2:6], "little")

    def __str__(self) -> str:
        if self.is_null:
            return "null"
        return f"file={self.file_no},page={self.page_no}"


@dataclass(frozen=True)
class ObservedPageHeader:
    """Raw fields observed at the beginning of a DM page.

    Names are intentionally generic until the field meanings are verified by
    more controlled page samples.
    """

    raw: bytes

    @classmethod
    def from_page(cls, page: bytes) -> "ObservedPageHeader":
        if len(page) < 64:
            raise ValueError("page must contain at least 64 bytes")
        return cls(page[:64])

    @property
    def page_type_raw(self) -> int:
        return self.raw[0]

    @property
    def group_raw(self) -> int:
        return int.from_bytes(self.raw[0:4], "little")

    @property
    def group_id(self) -> int:
        return self.group_raw & 0xFFFF

    @property
    def file_no_hint(self) -> int:
        return self.group_raw >> 16

    @property
    def page_no(self) -> int:
        return int.from_bytes(self.raw[4:8], "little")

    @property
    def prev_page(self) -> ObservedPageRef:
        return ObservedPageRef(self.raw[8:14])

    @property
    def next_page(self) -> ObservedPageRef:
        return ObservedPageRef(self.raw[14:20])

    @property
    def page_kind_raw(self) -> int:
        return int.from_bytes(self.raw[20:24], "little")

    @property
    def page_kind_label(self) -> str:
        return observed_page_kind_label(self.page_kind_raw)

    @property
    def field_20_u32le(self) -> int:
        return int.from_bytes(self.raw[32:36], "little")

    @property
    def field_24_u16le(self) -> int:
        return int.from_bytes(self.raw[36:38], "little")

    @property
    def field_26_u16le(self) -> int:
        return int.from_bytes(self.raw[38:40], "little")

    @property
    def field_2c_u16le(self) -> int:
        return int.from_bytes(self.raw[44:46], "little")

    @property
    def storage_id_candidate(self) -> int:
        return int.from_bytes(self.raw[58:62], "little")

    @property
    def observed_row_count(self) -> int:
        return self.field_2c_u16le

    def as_dict(self) -> dict[str, str | int]:
        return {
            "group_raw": self.group_raw,
            "page_type_raw": self.page_type_raw,
            "group_id": self.group_id,
            "file_no_hint": self.file_no_hint,
            "page_no": self.page_no,
            "prev_page": str(self.prev_page),
            "next_page": str(self.next_page),
            "page_kind_raw": self.page_kind_raw,
            "page_kind_label": self.page_kind_label,
            "storage_id_candidate": self.storage_id_candidate,
            "field_20_u32le": self.field_20_u32le,
            "field_24_u16le": self.field_24_u16le,
            "field_26_u16le": self.field_26_u16le,
            "field_2c_u16le": self.field_2c_u16le,
            "observed_row_count": self.observed_row_count,
        }


def observed_page_kind_label(page_kind_raw: int) -> str:
    return OBSERVED_PAGE_KIND_LABELS.get(page_kind_raw, "unknown")


def format_hex_dump(data: bytes, *, base_offset: int = 0, width: int = 16) -> str:
    lines: list[str] = []
    for index in range(0, len(data), width):
        chunk = data[index : index + width]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        lines.append(f"{base_offset + index:08x}  {hex_part:<{width * 3}} {ascii_part}")
    return "\n".join(lines)
