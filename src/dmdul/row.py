from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObservedRowHeader:
    """Observed DM row header prefix.

    This is based on controlled table samples and remains intentionally narrow:
    the first two bytes appear to hold row length, with the high bit marking a
    deleted row.
    """

    raw_len_flags: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "ObservedRowHeader":
        if len(data) < 2:
            raise ValueError("row data must contain at least two bytes")
        return cls(int.from_bytes(data[0:2], "big"))

    @property
    def is_deleted(self) -> bool:
        return bool(self.raw_len_flags & 0x8000)

    @property
    def length(self) -> int:
        return self.raw_len_flags & 0x7FFF


@dataclass(frozen=True)
class ObservedVarLength:
    length: int
    encoded_size: int


@dataclass(frozen=True)
class ObservedRow:
    page_offset: int
    data: bytes
    header: ObservedRowHeader

    @property
    def is_deleted(self) -> bool:
        return self.header.is_deleted

    @property
    def length(self) -> int:
        return self.header.length


@dataclass(frozen=True)
class ObservedRowLayout:
    header_size: int
    metadata: bytes
    column_payload_offset: int

    @property
    def metadata_size(self) -> int:
        return len(self.metadata)

    @property
    def has_unsupported_metadata(self) -> bool:
        return any(self.metadata)


def describe_observed_row_layout(row: ObservedRow, *, column_count: int) -> ObservedRowLayout:
    """Describe the currently observed row prefix before user column payload.

    Controlled samples show a two-byte row length/status prefix, followed by
    one metadata byte for <=4 columns and two metadata bytes for 5 columns. The
    metadata bytes are zero in the supported non-NULL samples. Non-zero bytes
    are kept visible for future NULL bitmap, column-directory, and MVCC work.
    """

    if column_count < 0:
        raise ValueError("column_count must be non-negative")
    metadata_size = 2 if column_count >= 5 else 1
    column_payload_offset = 2 + metadata_size
    if len(row.data) < column_payload_offset:
        raise ValueError(
            "row is too short while describing observed row layout: "
            f"payload_offset={column_payload_offset}, row_length={len(row.data)}"
        )
    return ObservedRowLayout(
        header_size=2,
        metadata=row.data[2:column_payload_offset],
        column_payload_offset=column_payload_offset,
    )


def decode_observed_var_length(data: bytes) -> ObservedVarLength:
    """Decode the observed DM inline variable-length prefix.

    Samples show lengths 0..127 encoded as one byte `0x80 + length`, and
    lengths >=128 encoded as a two-byte big-endian integer.
    """

    if not data:
        raise ValueError("variable-length prefix is empty")
    first = data[0]
    if first >= 0x80:
        return ObservedVarLength(length=first - 0x80, encoded_size=1)
    if len(data) < 2:
        raise ValueError("two-byte variable-length prefix is incomplete")
    return ObservedVarLength(length=int.from_bytes(data[0:2], "big"), encoded_size=2)


def iter_observed_rows(
    page: bytes,
    *,
    row_count: int,
    start_offset: int = 0x62,
) -> list[ObservedRow]:
    """Slice rows using the currently observed row-length chain.

    Controlled DM8 BTREE data pages place row bytes at offset 0x62 in the tested
    ordinary table pages. Each row starts with the observed two-byte row
    length/status field, and the next row starts after that length.
    """

    rows: list[ObservedRow] = []
    offset = start_offset
    for _ in range(row_count):
        if offset + 2 > len(page):
            raise ValueError(f"row header at offset {offset} is outside the page")
        header = ObservedRowHeader.from_bytes(page[offset : offset + 2])
        if header.length < 2:
            raise ValueError(f"invalid row length {header.length} at offset {offset}")
        end = offset + header.length
        if end > len(page):
            raise ValueError(
                f"row at offset {offset} extends past page boundary: end={end}"
            )
        rows.append(ObservedRow(page_offset=offset, data=page[offset:end], header=header))
        offset = end
    return rows


def scan_observed_row_chain(
    page: bytes,
    *,
    start_offset: int = 0x62,
    max_rows: int = 4096,
) -> list[ObservedRow]:
    """Scan consecutive row-like records until the observed free area.

    Some tested pages report only active row count in the page header while the
    physical row chain still contains deleted and updated records. This scanner
    follows row lengths until it reaches zero-filled free space or an invalid
    row-length prefix.
    """

    rows: list[ObservedRow] = []
    offset = start_offset
    for _ in range(max_rows):
        if offset + 2 > len(page):
            break
        raw = page[offset : offset + 2]
        if raw == b"\0\0" or raw == b"\xff\xff":
            break
        header = ObservedRowHeader.from_bytes(raw)
        if header.length < 2:
            break
        end = offset + header.length
        if end > len(page):
            break
        rows.append(ObservedRow(page_offset=offset, data=page[offset:end], header=header))
        offset = end
    return rows
