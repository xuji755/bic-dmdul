from __future__ import annotations

from dataclasses import dataclass

from .page import ObservedPageHeader
from .row import iter_observed_rows_by_slots, scan_observed_row_chain
from .storage import DataFile


LOB_DATA_PAGE_KIND = 0x20
LOB_DATA_PAYLOAD_OFFSET = 0x38
LONG_ROW_DATA_PAGE_KIND = 0x22
LONG_ROW_DATA_PAYLOAD_OFFSET = 0x70
LONG_ROW_RECORD_PAYLOAD_OFFSET = 0x0E


class LobReadError(ValueError):
    pass


@dataclass(frozen=True)
class OutOfLineLobLocator:
    lob_id: int
    byte_length: int
    group_id: int
    start_page: int


@dataclass(frozen=True)
class OutOfLineLobRead:
    payload: bytes
    locator: OutOfLineLobLocator
    page_numbers: tuple[int, ...]


def parse_out_of_line_lob_locator(raw: bytes) -> OutOfLineLobLocator | None:
    if len(raw) != 21:
        return None
    if raw[0] != 0x02:
        return None
    return OutOfLineLobLocator(
        lob_id=int.from_bytes(raw[1:5], "little", signed=False),
        byte_length=int.from_bytes(raw[9:13], "little", signed=False),
        group_id=int.from_bytes(raw[13:17], "little", signed=False),
        start_page=int.from_bytes(raw[17:21], "little", signed=False),
    )


def read_out_of_line_lob(
    *,
    raw_locator: bytes,
    data_files: dict[int, DataFile],
    group_id: int,
    file_no: int,
    max_pages: int = 65536,
) -> OutOfLineLobRead:
    locator = parse_out_of_line_lob_locator(raw_locator)
    if locator is None:
        raise LobReadError("unsupported LOB locator shape")
    if locator.group_id != group_id:
        raise LobReadError(
            f"LOB locator group id mismatch: locator={locator.group_id}, table={group_id}"
        )
    data_file = data_files.get(file_no)
    if data_file is None:
        raise LobReadError(f"LOB data file is not present in metadata: file_no={file_no}")

    chunks: list[bytes] = []
    page_numbers: list[int] = []
    page_no: int | None = locator.start_page
    seen: set[int] = set()
    while page_no is not None and sum(len(chunk) for chunk in chunks) < locator.byte_length:
        if page_no in seen:
            raise LobReadError(f"LOB page chain cycle at page {page_no}")
        if len(seen) >= max_pages:
            raise LobReadError(f"LOB page chain exceeds max_pages={max_pages}")
        seen.add(page_no)
        page = data_file.read_page(page_no)
        header = ObservedPageHeader.from_page(page)
        if header.group_id != group_id:
            raise LobReadError(
                f"LOB page group id mismatch at page {page_no}: {header.group_id}"
            )
        if header.file_no_hint != file_no:
            raise LobReadError(
                f"LOB page file hint mismatch at page {page_no}: {header.file_no_hint}"
            )
        if header.page_no != page_no:
            raise LobReadError(
                f"LOB page header page number mismatch: expected={page_no}, observed={header.page_no}"
            )
        if header.page_kind_raw == LOB_DATA_PAGE_KIND:
            observed_lob_id = int.from_bytes(page[0x24:0x28], "little", signed=False)
            payload_length = int.from_bytes(page[0x2C:0x2E], "little", signed=False)
            payload_offset = LOB_DATA_PAYLOAD_OFFSET
        elif header.page_kind_raw == LONG_ROW_DATA_PAGE_KIND:
            payload = _read_long_row_payload_from_page(
                page=page,
                lob_id=locator.lob_id,
            )
            chunks.append(payload)
            page_numbers.append(page_no)
            page_no = None if header.next_page.is_null else header.next_page.page_no
            continue
        else:
            raise LobReadError(
                f"LOB page kind mismatch at page {page_no}: 0x{header.page_kind_raw:08x}"
            )
        if observed_lob_id != locator.lob_id:
            raise LobReadError(
                f"LOB id mismatch at page {page_no}: expected={locator.lob_id}, observed={observed_lob_id}"
            )
        if payload_length > len(page) - payload_offset:
            raise LobReadError(
                f"LOB page payload length is outside page boundary at page {page_no}: {payload_length}"
            )
        chunks.append(page[payload_offset : payload_offset + payload_length])
        page_numbers.append(page_no)
        page_no = None if header.next_page.is_null else header.next_page.page_no

    payload = b"".join(chunks)
    if len(payload) < locator.byte_length:
        raise LobReadError(
            f"LOB page chain ended before requested length: read={len(payload)}, expected={locator.byte_length}"
        )
    return OutOfLineLobRead(
        payload=payload[: locator.byte_length],
        locator=locator,
        page_numbers=tuple(page_numbers),
    )


def _read_long_row_payload_from_page(*, page: bytes, lob_id: int) -> bytes:
    rows = iter_observed_rows_by_slots(page) or scan_observed_row_chain(page)
    for row in rows:
        data = row.data
        if len(data) < LONG_ROW_RECORD_PAYLOAD_OFFSET:
            continue
        observed_lob_id = int.from_bytes(data[2:6], "little", signed=False)
        if observed_lob_id != lob_id:
            continue
        payload_length = int.from_bytes(data[10:12], "little", signed=False)
        if payload_length > len(data) - LONG_ROW_RECORD_PAYLOAD_OFFSET:
            raise LobReadError(
                "long row record payload length is outside record boundary: "
                f"lob_id={lob_id}, payload_length={payload_length}, record_length={len(data)}"
            )
        return data[
            LONG_ROW_RECORD_PAYLOAD_OFFSET : LONG_ROW_RECORD_PAYLOAD_OFFSET + payload_length
        ]
    raise LobReadError(f"long row payload record not found for lob_id={lob_id}")
