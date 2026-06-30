import unittest


class TypeStorageEvidenceTest(unittest.TestCase):
    def test_date_bit_layout(self) -> None:
        self.assertEqual(_pack_date(1, 1, 1), bytes.fromhex("01 80 08"))
        self.assertEqual(_pack_date(2000, 1, 1), bytes.fromhex("d0 87 08"))
        self.assertEqual(_pack_date(2024, 2, 29), bytes.fromhex("e8 07 e9"))
        self.assertEqual(_pack_date(2026, 6, 30), bytes.fromhex("ea 07 f3"))

    def test_timestamp_bit_layout(self) -> None:
        self.assertEqual(
            _pack_timestamp(2024, 2, 29, 23, 59, 59, 123456),
            bytes.fromhex("e8 07 e9 77 df 81 c4 03"),
        )
        self.assertEqual(
            _pack_timestamp(2026, 6, 30, 10, 11, 12, 654321),
            bytes.fromhex("ea 07 f3 6a 61 e2 f7 13"),
        )

    def test_number38_base100_evidence(self) -> None:
        positive = bytes.fromhex(
            "d3 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f"
        )
        negative = bytes.fromhex(
            "2c 59 43 2d 17 0b 59 43 2d 17 0b 59 43 2d 17 0b 59 43 2d 17 66"
        )

        self.assertEqual(_decode_positive_base100_pairs(positive), "12345678901234567890123456789012345678")
        self.assertEqual(_decode_negative_base100_pairs(negative), "-12345678901234567890123456789012345678")


def _pack_date(year: int, month: int, day: int) -> bytes:
    value = year | (month << 15) | (day << 19)
    return value.to_bytes(3, "little")


def _pack_timestamp(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    microsecond: int,
) -> bytes:
    time_value = hour | (minute << 5) | (second << 11) | (microsecond << 17)
    return _pack_date(year, month, day) + time_value.to_bytes(5, "little")


def _decode_positive_base100_pairs(payload: bytes) -> str:
    pairs = [item - 1 for item in payload[1:]]
    return "".join(f"{item:02d}" for item in pairs)


def _decode_negative_base100_pairs(payload: bytes) -> str:
    pairs = [101 - item for item in payload[1:-1]]
    return "-" + "".join(f"{item:02d}" for item in pairs)


if __name__ == "__main__":
    unittest.main()
