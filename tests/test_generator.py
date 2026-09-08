import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_order_qr_generator import (
    DeliveryRow,
    dnth_values_from_ocr_row,
    extract_delivery,
    integer_from_ocr,
    normalized_part,
    validated_part,
    validated_manual_part,
)


class GeneratorTest(unittest.TestCase):
    def test_common_ocr_prefix_errors_are_normalized(self):
        self.assertEqual("TG053661-7151", normalized_part("T6053661-7151"))
        self.assertEqual("TG067122-1921", normalized_part("7G067122-1921"))
        self.assertEqual("TG067122-1921", normalized_part("16067122-1921"))

    def test_numeric_ocr_is_rounded_to_integer(self):
        self.assertEqual(1100, integer_from_ocr("1,100.09"))
        self.assertEqual(0, integer_from_ocr("0.00"))

    def test_dnth_ocr_uses_current_not_previous_qty_after_horizontal_shift(self):
        words = [
            {"text": "300.00", "cx": 0.535},
            {"text": "1.00", "cx": 0.630},
            {"text": "0.00", "cx": 0.702},
            {"text": "300.00", "cx": 0.769},
            {"text": "300.00", "cx": 0.835},
        ]
        self.assertEqual((1, 300), dnth_values_from_ocr_row(words))

    def test_dnth_ocr_preserves_real_zero_current_qty(self):
        words = [
            {"text": "400.00", "cx": 0.535},
            {"text": "3.00", "cx": 0.630},
            {"text": "0.00", "cx": 0.702},
            {"text": "0.00", "cx": 0.769},
            {"text": "0.00", "cx": 0.835},
        ]
        self.assertEqual((3, 0), dnth_values_from_ocr_row(words))

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

    def test_new_manual_part_format_can_be_confirmed(self):
        self.assertEqual("NEW.PART/01_A", validated_manual_part(" new.part/01_a "))
        self.assertIsNone(validated_manual_part("หมายเลขใหม่"))

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

    def test_native_dnth_kanban_delivery_order_uses_header_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dnth_native.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((210, 80), "KANBAN DELIVERY ORDER")
            page.insert_text((420, 180), "NO. OF")
            page.insert_text((424, 190), "BOX")
            page.insert_text((475, 180), "CURRENT")
            page.insert_text((484, 190), "QTY")
            page.insert_text((533, 180), "CURRENT")
            for y, part, boxes, qty in [
                (210, "TG028211-6190", "10.00", "500.00"),
                (230, "TG028993-6160", "10.00", "200.00"),
                (250, "TG053661-7020S2", "30.00", "3,000.00"),
            ]:
                page.insert_text((97, y), part)
                page.insert_text((162, y), part)
                page.insert_text((444, y), boxes)
                page.insert_text((496, y), qty)
            document.save(path)
            document.close()

            result = extract_delivery(path)
            self.assertEqual("DNTH", result.customer)
            self.assertEqual(
                [
                    ("TG028211-6190", 500, 10),
                    ("TG028993-6160", 200, 10),
                    ("TG053661-7020S2", 3000, 30),
                ],
                [(row.part_no, row.current_qty, row.number_of_boxes) for row in result.rows],
            )

    def test_aisin_purchase_uses_order_qty_and_number_of_boxes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "aisin.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((200, 50), "Pick List(PURCHASE)")
            page.insert_text((380, 250), "Order qty")
            page.insert_text((450, 240), "# of")
            page.insert_text((450, 255), "Box")
            page.insert_text((500, 240), "confirmed")
            page.insert_text((30, 180), "Total number of box")
            page.insert_text((75, 275), "31452-S301")
            page.insert_text((425, 282), "150")
            page.insert_text((475, 282), "1")
            document.save(path)
            document.close()
            result = extract_delivery(path)
            self.assertEqual("AISIN_PURCHASE", result.customer)
            self.assertEqual([("31452-S301", 150, 1)], [
                (row.part_no, row.current_qty, row.number_of_boxes) for row in result.rows
            ])

    def test_unknown_pdf_opens_in_manual_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unknown.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "UNSUPPORTED CUSTOMER DOCUMENT")
            document.save(path)
            document.close()
            result = extract_delivery(path)
            self.assertEqual("MANUAL_UNKNOWN", result.customer)
            self.assertEqual([], result.rows)


if __name__ == "__main__":
    unittest.main()
