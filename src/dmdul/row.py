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
    compact metadata bytes whose size currently tracks `ceil(column_count/4)`.
    The metadata bytes are zero in the supported non-NULL samples. Non-zero
    bytes are kept visible for future NULL bitmap, column-directory, and MVCC
    work.
    """

    if column_count < 0:
        raise ValueError("column_count must be non-negative")
    metadata_size = max(1, (column_count + 3) // 4)
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

    This "row chain" is only the physical in-page row-length sequence. It is
    not DM/Oracle-style row chaining where one logical row spans multiple data
    blocks. Cross-block chained rows need a separate pointer decoder and row
    reassembly path.
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


def iter_observed_rows_by_slots(
    page: bytes,
    *,
    start_offset: int = 0x62,
    max_rows: int = 4096,
) -> list[ObservedRow]:
    """Return active rows using the observed page-tail slot directory.

    DM data pages store row offsets in a page-tail slot array. The row count
    observed at page-header bytes 0x2c..0x2d gives the number of slot entries,
    and the array starts at `page_size - 10 - row_count * 2`. Rows must be cut
    from these slot offsets rather than guessed by scanning byte-by-byte or by
    assuming a fully contiguous physical row chain.
    """

    row_count = _observed_slot_count(page)
    if row_count <= 0:
        return _iter_observed_rows_by_discovered_slots(
            page,
            start_offset=start_offset,
            max_rows=max_rows,
        )
    if row_count > max_rows:
        return []
    slot_start = len(page) - 10 - (row_count * 2)
    if slot_start < 0:
        return []
    result: list[ObservedRow] = []
    seen_offsets: set[int] = set()
    for slot_index in range(row_count):
        slot_offset = slot_start + (slot_index * 2)
        row_offset = int.from_bytes(page[slot_offset : slot_offset + 2], "little")
        if row_offset in seen_offsets:
            continue
        seen_offsets.add(row_offset)
        if row_offset < start_offset or row_offset + 2 > len(page):
            continue
        header = ObservedRowHeader.from_bytes(page[row_offset : row_offset + 2])
        if header.length < 2:
            continue
        row_end = row_offset + header.length
        if row_end > len(page):
            continue
        row = ObservedRow(
            page_offset=row_offset,
            data=page[row_offset:row_end],
            header=header,
        )
        if not row.is_deleted:
            result.append(row)
    return result


def _observed_slot_count(page: bytes) -> int:
    if len(page) < 0x2E:
        return 0
    return int.from_bytes(page[0x2C:0x2E], "little")


def _iter_observed_rows_by_discovered_slots(
    page: bytes,
    *,
    start_offset: int,
    max_rows: int,
) -> list[ObservedRow]:
    physical_rows = scan_observed_row_chain(
        page,
        start_offset=start_offset,
        max_rows=max_rows,
    )
    rows_by_offset = {row.page_offset: row for row in physical_rows}
    slot_offsets = find_observed_row_slots(
        page,
        row_start_offsets=set(rows_by_offset),
        search_start_offset=_slot_search_start_offset(page),
    )
    result: list[ObservedRow] = []
    seen_offsets: set[int] = set()
    for offset in reversed(slot_offsets):
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        row = rows_by_offset.get(offset)
        if row is not None and not row.is_deleted:
            result.append(row)
    return result


def find_observed_row_slots(
    page: bytes,
    *,
    row_start_offsets: set[int],
    search_start_offset: int = 0,
) -> list[int]:
    """Find page-tail slot entries that point to observed row starts.

    Current evidence shows 2-byte little-endian page offsets near the page tail,
    ordered from the last physical row back to the first. The caller supplies
    known row starts so this remains an evidence probe, not a full slot decoder.
    """

    slots: list[tuple[int, int]] = []
    for offset in range(search_start_offset, len(page) - 1, 2):
        value = int.from_bytes(page[offset : offset + 2], "little")
        if value in row_start_offsets:
            slots.append((offset, value))
    return [value for _, value in slots]


def _slot_search_start_offset(page: bytes) -> int:
    return max(0, len(page) - 512)
