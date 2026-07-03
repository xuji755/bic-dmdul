from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dmdul.lob import read_out_of_line_lob
from dmdul.storage import DataFile


class LobPageReadTest(unittest.TestCase):
    def _page_ref(self, page_no: int | None) -> bytes:
        if page_no is None:
            return b"\xff" * 6
        return (0).to_bytes(2, "little") + page_no.to_bytes(4, "little")

    def _lob_page(
        self,
        *,
        page_no: int,
        lob_id: int,
        payload: bytes,
        prev_page: int | None,
        next_page: int | None,
    ) -> bytes:
        page = bytearray(b"\0" * 8192)
        page[0:4] = (6).to_bytes(4, "little")
        page[4:8] = page_no.to_bytes(4, "little")
        page[8:14] = self._page_ref(prev_page)
        page[14:20] = self._page_ref(next_page)
        page[20:24] = (0x20).to_bytes(4, "little")
        page[0x24:0x28] = lob_id.to_bytes(4, "little")
        page[0x2C:0x2E] = len(payload).to_bytes(2, "little")
        page[0x38 : 0x38 + len(payload)] = payload
        return bytes(page)

    def _locator(self, *, lob_id: int, byte_length: int, start_page: int) -> bytes:
        return (
            b"\x02"
            + lob_id.to_bytes(4, "little")
            + b"\0" * 4
            + byte_length.to_bytes(4, "little")
            + (6).to_bytes(4, "little")
            + start_page.to_bytes(4, "little")
        )

    def test_reads_out_of_line_lob_page_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "DMDUL_TS01.DBF"
            lob_id = 0x12345678
            payload = b"hello-world"
            pages = [
                b"\0" * 8192,
                self._lob_page(
                    page_no=1,
                    lob_id=lob_id,
                    payload=payload[:5],
                    prev_page=None,
                    next_page=2,
                ),
                self._lob_page(
                    page_no=2,
                    lob_id=lob_id,
                    payload=payload[5:],
                    prev_page=1,
                    next_page=None,
                ),
            ]
            path.write_bytes(b"".join(pages))

            result = read_out_of_line_lob(
                raw_locator=self._locator(
                    lob_id=lob_id,
                    byte_length=len(payload),
                    start_page=1,
                ),
                data_files={0: DataFile(path)},
                group_id=6,
                file_no=0,
            )

        self.assertEqual(result.payload, payload)
        self.assertEqual(result.page_numbers, (1, 2))
