import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_order_qr_generator import (
    DeliveryRow,
    extract_delivery,
    integer_from_ocr,
    normalized_part,
    validated_part,
)


class GeneratorTest(unittest.TestCase):
    def test_common_ocr_prefix_errors_are_normalized(self):
        self.assertEqual("TG053661-7151", normalized_part("T6053661-7151"))
        self.assertEqual("TG067122-1921", normalized_part("7G067122-1921"))
        self.assertEqual("TG067122-1921", normalized_part("16067122-1921"))

    def test_numeric_ocr_is_rounded_to_integer(self):
        self.assertEqual(1100, integer_from_ocr("1,100.09"))
        self.assertEqual(0, integer_from_ocr("0.00"))

    def test_payload_is_stable(self):
        row = DeliveryRow("TG053661-7151", 1100, 11)
        self.assertEqual(
            "CHECKTAGRS|DO|PART=TG053661-7151|QTY=1100|BOX=11",
            row.payload,
        )

    def test_all_supported_customer_parts_can_be_edited(self):
        self.assertEqual("TG053661-7151", validated_part(" TG053661-7151 "))
        self.assertEqual("JGF02-002190-31", validated_part("jgf02-002190-31"))
        self.assertEqual("JGC10-001180", validated_part("JGC10-001180"))
        self.assertEqual("7521T0376", validated_part("7521T0376"))

    def test_invalid_part_is_rejected(self):
        self.assertIsNone(validated_part("PART NUMBER"))

    def test_siam_nsk_report_uses_qty_pcs_and_box_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "siam_nsk.pdf"
            document = fitz.open()
            page = document.new_page(width=595.32, height=841.92)
            page.insert_text((280, 42), "PARTS DELIVERY REPORT")
            page.insert_text((350, 100), "SIAM NSK")
            page.insert_text((48, 170), "7521T0376")
            page.insert_text((466, 170), "3200")
            page.insert_text((501, 170), "40")
            document.save(path)
            document.close()

            result = extract_delivery(path)
            self.assertEqual("SIAM_NSK", result.customer)
            self.assertEqual(1, len(result.rows))
            self.assertEqual("7521T0376", result.rows[0].part_no)
            self.assertEqual(3200, result.rows[0].current_qty)
            self.assertEqual(40, result.rows[0].number_of_boxes)


if __name__ == "__main__":
    unittest.main()
