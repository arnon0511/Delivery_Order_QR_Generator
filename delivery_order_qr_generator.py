from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import fitz
import pytesseract
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageTk


def configure_tesseract() -> bool:
    """Use the portable OCR bundled beside the Windows executable when present."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "tesseract" / "tesseract.exe")
    candidates.extend([
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ])
    executable = next((path for path in candidates if path.exists()), None)
    if executable:
        if os.name == "nt":
            try:
                public_dir = Path(os.environ.get("PUBLIC", "C:/Users/Public"))
                runtime_dir = public_dir / "Documents" / "CheckTagRS_Runtime"
                temp_dir = runtime_dir / "temp"
                temp_dir.mkdir(parents=True, exist_ok=True)
                os.environ["TEMP"] = str(temp_dir)
                os.environ["TMP"] = str(temp_dir)
                tempfile.tempdir = str(temp_dir)

                # Some Windows Tesseract builds fail when their own path contains
                # Thai or other non-ASCII characters. Cache OCR in a stable path.
                if any(ord(char) > 127 for char in str(executable)):
                    cached_ocr = runtime_dir / "tesseract"
                    if not (cached_ocr / "tesseract.exe").exists():
                        shutil.copytree(executable.parent, cached_ocr, dirs_exist_ok=True)
                    executable = cached_ocr / "tesseract.exe"
            except OSError:
                # The normal portable path remains available if Public is locked.
                pass
        pytesseract.pytesseract.tesseract_cmd = str(executable)
        tessdata = executable.parent / "tessdata"
        if tessdata.exists():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        return True
    system_tesseract = shutil.which("tesseract")
    if system_tesseract:
        pytesseract.pytesseract.tesseract_cmd = system_tesseract
        return True
    return False


@dataclass
class DeliveryRow:
    part_no: str
    current_qty: int
    number_of_boxes: int
    confidence: str = "ตรวจแล้ว"
    source_y: float | None = None

    @property
    def payload(self) -> str:
        return (
            f"CHECKTAGRS|DO|PART={self.part_no}|"
            f"QTY={self.current_qty}|BOX={self.number_of_boxes}"
        )


@dataclass
class ExtractionResult:
    customer: str
    page_index: int
    rows: list[DeliveryRow]


def normalized_part(text: str) -> str | None:
    compact = re.sub(r"[^A-Z0-9-]", "", text.upper())
    # DNTH OCR commonly reads the printed TG prefix as T6, 7G or 16.
    match = re.fullmatch(r"[A-Z0-9]{2}(\d{6})-([A-Z0-9]{4,10})", compact)
    return f"TG{match.group(1)}-{match.group(2)}" if match else None


def validated_part(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text.upper())
    if (re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", compact)
            and len(compact) >= 6 and any(char.isdigit() for char in compact)):
        return compact
    return None


def integer_from_ocr(text: str) -> int | None:
    cleaned = text.strip().replace(",", "").replace("O", "0").replace("o", "0")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    return int(round(float(match.group())))


def dnth_values_from_ocr_row(words: list[dict]) -> tuple[int | None, int | None]:
    """Return (boxes, current_qty) from the ordered DNTH numeric columns.

    DNTH scan layouts can move horizontally. Reading a fixed x-range can pick
    PREVIOUS QTY instead of CURRENT QTY. The stable meaning is the column order:
    QUANTITY/BOX, NO. OF BOX, PREVIOUS QTY, CURRENT QTY, DIFF QTY.
    Newer layouts without previous/diff have three numeric columns.
    """
    numeric_cells: list[tuple[float, int]] = []
    for word in words:
        if not 0.46 <= word["cx"] <= 0.85:
            continue
        value = integer_from_ocr(word["text"])
        if value is not None:
            numeric_cells.append((word["cx"], value))
    numeric_cells.sort(key=lambda cell: cell[0])
    values = [value for _x, value in numeric_cells]
    if len(values) >= 5:
        # QTY/BOX, BOX, PREVIOUS, CURRENT, DIFF
        return values[1], values[3]
    if len(values) >= 3:
        # QTY/BOX, BOX, CURRENT (native/new layout without previous/diff)
        return values[1], values[2]
    return None, None


def extract_dnth_text_rows(page: fitz.Page) -> list[DeliveryRow]:
    """Read native-text DNTH tables by their headers, not fixed row positions."""
    words = page.get_text("words")
    if not words:
        return []
    page_text = page.get_text().upper()
    if "DELIVERY ORDER" not in page_text or "CURRENT" not in page_text:
        return []

    def cx(word: tuple) -> float:
        return (word[0] + word[2]) / 2

    def cy(word: tuple) -> float:
        return (word[1] + word[3]) / 2

    # Locate the actual NO. OF BOX and CURRENT QTY headers. The newer DNTH
    # layout moves these columns to the right and some rows omit RCV LANE.
    box_headers = [
        word for word in words
        if word[4].upper() == "BOX"
        and any(other[4].upper() == "OF" and abs(cy(other) - cy(word)) <= 12
                and abs(cx(word) - cx(other)) < 35 for other in words)
    ]
    qty_headers = [
        word for word in words
        if word[4].upper() == "QTY"
        and any(other[4].upper() == "CURRENT" and abs(cx(other) - cx(word)) <= 30
                and abs(cy(other) - cy(word)) <= 15 for other in words)
    ]
    if not box_headers or not qty_headers:
        return []

    box_x = cx(box_headers[0])
    qty_x = min((cx(word) for word in qty_headers if cx(word) > box_x), default=None)
    if qty_x is None:
        return []
    next_column_x = min(
        (cx(word) for word in words
         if word[4].upper() == "CURRENT" and cx(word) > qty_x + 20),
        default=page.rect.width,
    )
    previous_column_x = max(
        (cx(word) for word in words
         if word[4].upper() in {"/BOX", "QUANTITY"} and cx(word) < box_x),
        default=box_x - (qty_x - box_x),
    )
    box_min = (previous_column_x + box_x) / 2
    box_max = (box_x + qty_x) / 2
    qty_min = box_max
    qty_max = (qty_x + next_column_x) / 2

    rows: list[DeliveryRow] = []
    seen: set[tuple[str, int]] = set()
    for part_word in words:
        part = validated_part(part_word[4])
        if not part or not part.startswith("TG"):
            continue
        row_y = cy(part_word)
        row_key = (part, round(row_y))
        if row_key in seen:
            continue
        same_row = [word for word in words if abs(cy(word) - row_y) <= 4]
        box_values = [integer_from_ocr(word[4]) for word in same_row if box_min <= cx(word) < box_max]
        qty_values = [integer_from_ocr(word[4]) for word in same_row if qty_min <= cx(word) < qty_max]
        boxes = next((value for value in box_values if value is not None), None)
        qty = next((value for value in qty_values if value is not None), None)
        if boxes is None or qty is None:
            continue
        seen.add(row_key)
        rows.append(DeliveryRow(part, qty, boxes, "อ่านจาก DNTH PDF - กรุณาตรวจ", row_y))
    return sorted(rows, key=lambda row: row.source_y or 0)


def extract_dnth_rows(pdf_path: Path) -> list[DeliveryRow]:
    document = fitz.open(pdf_path)
    if document.page_count < 1:
        document.close()
        raise ValueError("PDF ไม่มีหน้าเอกสาร")
    page = document[0]
    text_rows = extract_dnth_text_rows(page)
    if text_rows:
        document.close()
        return text_rows
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    document.close()
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(
        image, config="--psm 6", output_type=pytesseract.Output.DICT
    )

    width = image.width
    lines: dict[tuple[int, int, int], list[dict]] = {}
    for index, text in enumerate(data["text"]):
        if not text.strip():
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(
            {
                "text": text,
                "cx": (data["left"][index] + data["width"][index] / 2) / width,
                "conf": float(data["conf"][index]),
            }
        )

    rows: list[DeliveryRow] = []
    seen: set[str] = set()
    for words in lines.values():
        parts = [normalized_part(w["text"].strip("|“”'\"")) for w in words]
        parts = [part for part in parts if part]
        if not parts:
            continue
        part = parts[0]
        if part in seen:
            continue

        boxes, qty = dnth_values_from_ocr_row(words)
        # Compatibility fallback for unusually sparse OCR rows.
        if boxes is None or qty is None:
            box_candidates = [integer_from_ocr(w["text"]) for w in words if 0.56 <= w["cx"] <= 0.65]
            qty_candidates = [integer_from_ocr(w["text"]) for w in words if 0.68 <= w["cx"] <= 0.77]
            boxes = next((value for value in box_candidates if value is not None), None)
            qty = next((value for value in qty_candidates if value is not None), None)
        if boxes is None or qty is None:
            continue
        seen.add(part)
        rows.append(DeliveryRow(part, qty, boxes, "OCR - กรุณาตรวจ"))

    if not rows:
        raise ValueError("อ่านตารางไม่สำเร็จ กรุณาตรวจความคมชัดหรือหมุนเอกสารให้ถูกด้าน")
    return rows


def _number_near(words: list[tuple], y: float, x_min: float, x_max: float) -> int | None:
    candidates = [
        w for w in words
        if x_min <= (w[0] + w[2]) / 2 <= x_max and abs(((w[1] + w[3]) / 2) - y) <= 8
    ]
    return integer_from_ocr(candidates[0][4]) if candidates else None


def extract_jath_rows(page: fitz.Page) -> list[DeliveryRow]:
    words = page.get_text("words")
    rows: list[DeliveryRow] = []
    for word in words:
        part = word[4].strip().upper()
        if not re.fullmatch(r"J[A-Z]{2}\d{2}-\d{6}-\d{2}", part):
            continue
        y = (word[1] + word[3]) / 2
        boxes = _number_near(words, y, 480, 515)
        qty = _number_near(words, y, 535, 575)
        if boxes is not None and qty is not None:
            rows.append(DeliveryRow(part, qty, boxes, "อ่านจาก JATH PDS - กรุณาตรวจ"))
    return rows


def extract_jtcs_rows(page: fitz.Page) -> list[DeliveryRow]:
    words = page.get_text("words")
    rows: list[DeliveryRow] = []
    for word in words:
        part = word[4].strip().upper()
        if not re.fullmatch(r"J[A-Z]{2}\d{2}-[A-Z0-9-]{6,16}", part):
            continue
        y = (word[1] + word[3]) / 2
        boxes = _number_near(words, y, 255, 300)
        qty = _number_near(words, y, 315, 365)
        if boxes is not None and qty is not None:
            rows.append(DeliveryRow(part, qty, boxes, "อ่านจาก JTCS Manifest - กรุณาตรวจ"))
    return rows


def extract_siam_nsk_rows(page: fitz.Page) -> list[DeliveryRow]:
    words = page.get_text("words")
    rows: list[DeliveryRow] = []
    for word in words:
        x_center = (word[0] + word[2]) / 2
        part = normalize_document_part(word[4])
        if not 35 <= x_center <= 115 or not re.fullmatch(r"[A-Z0-9]{8,12}", part):
            continue
        y = (word[1] + word[3]) / 2
        qty = _number_near(words, y, 455, 490)
        boxes = _number_near(words, y, 494, 515)
        if qty is not None and boxes is not None:
            rows.append(DeliveryRow(part, qty, boxes, "อ่านจาก SIAM NSK - กรุณาตรวจ", y))
    return sorted(rows, key=lambda row: row.source_y or 0)


def extract_aisin_purchase_rows(page: fitz.Page) -> list[DeliveryRow]:
    """Read AISIN Pick List(PURCHASE): AISIN Part, Order qty and # of Box."""
    words = page.get_text("words")
    if not re.search(r"PICK\s+LIST\s*\(PURCHASE\)", page.get_text().upper()):
        return []

    def cx(word: tuple) -> float:
        return (word[0] + word[2]) / 2

    def cy(word: tuple) -> float:
        return (word[1] + word[3]) / 2

    order_words = [word for word in words if word[4].upper() == "ORDER" and cy(word) > 200]
    box_words = [word for word in words if word[4].upper() == "BOX" and cy(word) > 200]
    if not order_words or not box_words:
        return []
    order_word_x = max(cx(word) for word in order_words)
    order_qty_word_x = min(
        (cx(word) for word in words
         if word[4].upper() == "QTY" and order_word_x < cx(word) < order_word_x + 45),
        default=order_word_x + 20,
    )
    order_x = (order_word_x + order_qty_word_x) / 2
    box_x = max(cx(word) for word in box_words)
    # Numeric cells are right-aligned; the Order qty value can sit close to
    # the left edge of the following # of Box header.
    split_x = box_x
    next_x = min(
        (cx(word) for word in words if word[4].upper() == "CONFIRMED" and cx(word) > box_x),
        default=page.rect.width,
    )

    rows: list[DeliveryRow] = []
    for word in words:
        part = validated_part(word[4])
        if not part or not 60 <= cx(word) <= 220 or cy(word) <= 255:
            continue
        row_y = cy(word)
        same_row = [candidate for candidate in words if abs(cy(candidate) - row_y) <= 12]
        qty_values = [integer_from_ocr(candidate[4]) for candidate in same_row
                      if order_x - 25 <= cx(candidate) < split_x]
        box_values = [integer_from_ocr(candidate[4]) for candidate in same_row
                      if split_x <= cx(candidate) < (box_x + next_x) / 2]
        qty = next((value for value in qty_values if value is not None), None)
        boxes = next((value for value in box_values if value is not None), None)
        if qty is not None and boxes is not None:
            rows.append(DeliveryRow(part, qty, boxes, "อ่านจาก AISIN PURCHASE - กรุณาตรวจ", row_y))
    return rows


def normalize_document_part(text: str) -> str:
    return re.sub(r"\s+", "", text.upper())


def extract_delivery(pdf_path: Path) -> ExtractionResult:
    document = fitz.open(pdf_path)
    try:
        page_texts = [page.get_text().upper() for page in document]

        for index, text in enumerate(page_texts):
            if (re.search(r"PICK\s+LIST\s*\(PURCHASE\)", text)
                    and re.search(r"TOTAL\s+NUMBER\s+OF\s+BOX", text)):
                rows = extract_aisin_purchase_rows(document[index])
                if rows:
                    return ExtractionResult("AISIN_PURCHASE", index, rows)

        for index, text in enumerate(page_texts):
            if "PARTS DELIVERY REPORT" in text and "SIAM NSK" in text:
                rows = extract_siam_nsk_rows(document[index])
                if rows:
                    return ExtractionResult("SIAM_NSK", index, rows)

        for index, text in enumerate(page_texts):
            if "PART DELIVERY SHEET" in text and "ORDER" in text and "KANBANS" in text:
                rows = extract_jath_rows(document[index])
                if rows:
                    return ExtractionResult("JATH", index, rows)

        for index, text in enumerate(page_texts):
            if "SUPPLIER MANIFEST" in text and "CONTAINERS" in text:
                rows = extract_jtcs_rows(document[index])
                if rows:
                    return ExtractionResult("JTCS", index, rows)

        has_native_text = any(text.strip() for text in page_texts)
        looks_like_dnth = any(
            "DENSO THAILAND" in text
            or "DNTH" in text
            or "KANBAN DELIVERY ORDER" in text
            for text in page_texts
        )
    finally:
        document.close()

    # Unknown text-based layouts go directly to manual entry. OCR is reserved
    # for scanned documents and DNTH layouts that genuinely require it.
    if has_native_text and not looks_like_dnth:
        return ExtractionResult("MANUAL_UNKNOWN", 0, [])

    try:
        rows = extract_dnth_rows(pdf_path)
        return ExtractionResult("DNTH", 0, rows)
    except ValueError:
        # Unknown documents remain usable through the side-by-side manual mode.
        return ExtractionResult("MANUAL_UNKNOWN", 0, [])


def make_qr_png(payload: str) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=3)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def make_qr_card(row: DeliveryRow) -> bytes:
    qr = Image.open(io.BytesIO(make_qr_png(row.payload))).convert("RGB").resize((520, 520))
    card = Image.new("RGB", (620, 660), "white")
    card.paste(qr, (50, 18))
    draw = ImageDraw.Draw(card)
    font_paths = [
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    font_path = next((path for path in font_paths if path.exists()), None)
    font = ImageFont.truetype(str(font_path), 30) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(str(font_path), 26) if font_path else ImageFont.load_default()
    draw.text((310, 555), row.part_no, anchor="mm", fill="black", font=font)
    draw.text((310, 610), f"QTY {row.current_qty:,} | BOX {row.number_of_boxes}", anchor="mm", fill="black", font=small)
    stream = io.BytesIO()
    card.save(stream, format="PNG")
    return stream.getvalue()


def _card_rectangles(customer: str, page: fitz.Page, count: int) -> list[fitz.Rect]:
    if customer == "JATH":
        size_w, size_h, start_y = 150.0, 160.0, 325.0
        gap = (page.rect.width - min(count, 3) * size_w) / (min(count, 3) + 1)
        return [fitz.Rect(gap + (i % 3) * (size_w + gap), start_y + (i // 3) * 175,
                          gap + (i % 3) * (size_w + gap) + size_w, start_y + (i // 3) * 175 + size_h)
                for i in range(count)]
    if customer == "JTCS":
        size_w, size_h, start_y = 112.0, 120.0, 540.0
        gap = (page.rect.width - min(count, 4) * size_w) / (min(count, 4) + 1)
        return [fitz.Rect(gap + (i % 4) * (size_w + gap), start_y + (i // 4) * 135,
                          gap + (i % 4) * (size_w + gap) + size_w, start_y + (i // 4) * 135 + size_h)
                for i in range(count)]
    # DNTH uses a portrait page with a large blank centre. Use two columns for
    # three or more items so every QR remains on the original single page.
    if count >= 3:
        size_w, size_h, start_y = 145.0, 154.0, 300.0
        gap_x = (page.rect.width - 2 * size_w) / 3
        return [
            fitz.Rect(
                gap_x + (i % 2) * (size_w + gap_x),
                start_y + (i // 2) * 168,
                gap_x + (i % 2) * (size_w + gap_x) + size_w,
                start_y + (i // 2) * 168 + size_h,
            )
            for i in range(count)
        ]
    size_w, size_h = 175.0, 186.0
    x = (page.rect.width - size_w) / 2
    return [fitz.Rect(x, 285 + i * 205, x + size_w, 285 + i * 205 + size_h) for i in range(count)]


def _siam_nsk_card_rectangles(rows: list[DeliveryRow]) -> list[fitz.Rect]:
    return [fitz.Rect(522, row.source_y - 27, 582, row.source_y + 32) for row in rows if row.source_y is not None]


def _write_dnth_single_page(source: Path, destination: Path, rows: list[DeliveryRow]) -> None:
    source_doc = fitz.open(source)
    output = fitz.open()
    original_page = source_doc[0]
    pix = original_page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
    background = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    active = [row for row in rows if row.current_qty > 0 and row.number_of_boxes > 0]
    page_w, page_h = original_page.rect.width, original_page.rect.height
    for rect, row in zip(_card_rectangles("DNTH", original_page, len(active)), active):
        card = Image.open(io.BytesIO(make_qr_card(row))).convert("RGB")
        pixel_rect = (
            int(rect.x0 / page_w * background.width), int(rect.y0 / page_h * background.height),
            int(rect.x1 / page_w * background.width), int(rect.y1 / page_h * background.height),
        )
        card = card.resize((pixel_rect[2] - pixel_rect[0], pixel_rect[3] - pixel_rect[1]))
        background.paste(card, pixel_rect[:2])
    stream = io.BytesIO()
    background.save(stream, format="PNG")
    new_page = output.new_page(width=page_w, height=page_h)
    new_page.insert_image(new_page.rect, stream=stream.getvalue())
    output.save(destination, garbage=4, deflate=True)


def add_qr_to_pdf(source: Path, destination: Path, result: ExtractionResult) -> None:
    rows = result.rows
    active = [row for row in rows if row.current_qty > 0 and row.number_of_boxes > 0]
    if not active:
        raise ValueError("ไม่มีรายการ Current QTY และ NO. OF BOX มากกว่า 0")

    if result.customer == "DNTH":
        _write_dnth_single_page(source, destination, rows)
        return

    if result.customer == "MANUAL_UNKNOWN":
        source_document = fitz.open(source)
        document = fitz.open()
        document.insert_pdf(source_document)
        page = document.new_page(width=595, height=842)
        page.insert_text((36, 42), "CHECK TAG_RS - MANUAL VERIFIED QR", fontsize=14)
        rects = _card_rectangles("DNTH", page, len(active))
        if any(rect.y1 > page.rect.height - 30 for rect in rects):
            raise ValueError("รายการมากเกินพื้นที่หน้า QR กรุณาแบ่งเอกสาร")
        for rect, row in zip(rects, active):
            page.insert_image(rect, stream=make_qr_card(row), keep_proportion=True, overlay=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination, garbage=4, deflate=True)
        return

    source_document = fitz.open(source)
    document = fitz.open()
    document.insert_pdf(
        source_document,
        from_page=result.page_index,
        to_page=result.page_index,
    )
    page = document[0]
    rects = (_siam_nsk_card_rectangles(active) if result.customer == "SIAM_NSK"
             else _card_rectangles(result.customer, page, len(active)))
    if len(rects) != len(active) or any(rect.y1 > page.rect.height - 55 for rect in rects):
        raise ValueError("พื้นที่หน้าเอกสารไม่พอสำหรับ QR กรุณาลดรายการหรือใช้หน้า QR แยก")
    for rect, row in zip(rects, active):
        page.insert_image(rect, stream=make_qr_card(row), keep_proportion=True, overlay=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination, garbage=4, deflate=True)


class App(tk.Tk):
    WARNING_TEXT = (
        "คำเตือน: ข้อมูลอ่านจาก PDF ด้วยระบบอัตโนมัติและมีโอกาสผิดพลาด "
        "กรุณาเปรียบเทียบ Part No., Current QTY และ NO. OF BOX กับ PDF ต้นฉบับให้ครบทุกแถว "
        "ผู้ตรวจสอบต้องลงชื่อก่อน Generate QR Code"
    )

    def __init__(self) -> None:
        super().__init__()
        self.title("Delivery Order QR Generator v0.7.1 - Review & Sign")
        self.geometry("1280x800")
        self.minsize(980, 650)
        self.pdf_path: Path | None = None
        self.pdf_document: fitz.Document | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.page_index = 0
        self.zoom = 0.9
        self.rows: list[DeliveryRow] = []
        self.result: ExtractionResult | None = None
        self.verified_rows: set[int] = set()
        self.signed_name = ""
        self.signed_at: datetime | None = None

        ttk.Label(self, text="Delivery Order QR Generator", font=("Segoe UI", 19, "bold")).pack(pady=(10, 2))
        ttk.Label(self, text="เปรียบเทียบ PDF ต้นฉบับกับข้อมูลด้านขวา ก่อนยืนยันและลงชื่อ").pack()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=14, pady=8)
        ttk.Button(top, text="1. เลือก Delivery Order PDF", command=self.open_pdf).pack(side="left")
        self.file_label = ttk.Label(top, text="ยังไม่ได้เลือกไฟล์")
        self.file_label.pack(side="left", padx=12)

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        left = ttk.LabelFrame(panes, text="PDF ต้นฉบับ")
        right = ttk.LabelFrame(panes, text="ข้อมูลที่โปรแกรมอ่านได้ - แก้ไขและยืนยันทีละแถว")
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        viewer = ttk.Frame(left)
        viewer.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas = tk.Canvas(viewer, background="#777777", highlightthickness=0)
        yscroll = ttk.Scrollbar(viewer, orient="vertical", command=self.canvas.yview)
        xscroll = ttk.Scrollbar(viewer, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        viewer.rowconfigure(0, weight=1)
        viewer.columnconfigure(0, weight=1)

        nav = ttk.Frame(left)
        nav.pack(fill="x", padx=6, pady=(0, 6), before=viewer)
        ttk.Button(nav, text="◀ ก่อนหน้า", command=lambda: self.change_page(-1)).pack(side="left")
        ttk.Button(nav, text="ถัดไป ▶", command=lambda: self.change_page(1)).pack(side="left", padx=5)
        self.page_label = ttk.Label(nav, text="หน้า - / -")
        self.page_label.pack(side="left", padx=10)
        ttk.Button(nav, text="+ ขยาย", command=lambda: self.change_zoom(0.15)).pack(side="right")
        ttk.Button(nav, text="− ย่อ", command=lambda: self.change_zoom(-0.15)).pack(side="right", padx=5)

        columns = ("part", "qty", "box", "status")
        self.tree = ttk.Treeview(right, columns=columns, show="headings", height=14)
        for key, title, width in [
            ("part", "Part No.", 180), ("qty", "Current QTY", 105),
            ("box", "NO. OF BOX", 95), ("status", "สถานะ", 130),
        ]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center")
        self.tree.tag_configure("pending", foreground="#b42318")
        self.tree.tag_configure("verified", foreground="#067647")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())

        row_buttons = ttk.Frame(right)
        row_buttons.pack(fill="x", padx=6, pady=(0, 6), before=self.tree)
        ttk.Button(row_buttons, text="แก้รายการ", command=self.edit_selected).pack(side="left")
        ttk.Button(row_buttons, text="เพิ่มรายการ", command=self.add_row).pack(side="left", padx=5)
        ttk.Button(row_buttons, text="ยืนยันแถวที่เลือก", command=self.verify_selected).pack(side="right")
        ttk.Button(row_buttons, text="ยืนยันครบทุกแถว", command=self.verify_all).pack(side="right", padx=5)

        table_scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=table_scroll.set)
        table_scroll.pack(side="right", fill="y", padx=(0, 6), pady=6, before=self.tree)

        warning = tk.Label(
            self, text=self.WARNING_TEXT, bg="#fff1f0", fg="#b42318",
            font=("Segoe UI", 10, "bold"), justify="left", anchor="w", wraplength=1200,
            padx=10, pady=7,
        )
        # Place the warning and signature controls before the expandable PDF
        # panes so Windows scaling cannot push Generate below the screen.
        warning.pack(fill="x", padx=14, pady=(0, 6), before=panes)

        sign = ttk.Frame(self)
        sign.pack(fill="x", padx=14, pady=(0, 8), before=panes)
        self.ack_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            sign, text="ฉันตรวจสอบข้อมูลกับ PDF ต้นฉบับครบทุกแถวแล้วและยอมรับคำเตือน",
            variable=self.ack_var, command=self.invalidate_signature,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 5))
        ttk.Label(sign, text="ชื่อผู้ตรวจสอบ / รหัสพนักงาน:").grid(row=1, column=0, sticky="w")
        self.signer_var = tk.StringVar()
        self.signer_entry = ttk.Entry(sign, textvariable=self.signer_var, width=34)
        self.signer_entry.grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(sign, text="2. ลงชื่อและยืนยัน", command=self.sign_review).grid(row=1, column=2, padx=6)
        self.generate_button = ttk.Button(sign, text="3. Generate QR Code", command=self.generate, state="disabled")
        self.generate_button.grid(row=1, column=3, padx=(6, 0), ipadx=12, ipady=4)
        self.sign_status = ttk.Label(sign, text="ยังไม่ได้ลงชื่อ", foreground="#b42318")
        self.sign_status.grid(row=2, column=0, columnspan=4, sticky="w", pady=(5, 0))
        sign.columnconfigure(1, weight=1)
        self.signer_var.trace_add("write", lambda *_args: self.on_signer_changed())

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            verified = index in self.verified_rows
            status = "ตรวจแล้ว" if verified else "รอตรวจ"
            if row.current_qty <= 0 or row.number_of_boxes <= 0:
                status += " (ค่าเป็น 0)"
            self.tree.insert(
                "", "end", iid=str(index),
                values=(row.part_no, f"{row.current_qty:,}", row.number_of_boxes, status),
                tags=("verified" if verified else "pending",),
            )
        self.update_generate_state()

    def open_pdf(self) -> None:
        selected = filedialog.askopenfilename(title="เลือก Delivery Order", filetypes=[("PDF", "*.pdf")])
        if not selected:
            return
        selected_path = Path(selected)
        try:
            result = extract_delivery(selected_path)
            document = fitz.open(selected_path)
            if self.pdf_document is not None:
                self.pdf_document.close()
            self.pdf_path = selected_path
            self.pdf_document = document
            self.result = result
            self.rows = result.rows
            self.page_index = result.page_index
            self.verified_rows.clear()
            self.ack_var.set(False)
            self.reset_signature()
            self.file_label.configure(text=f"{selected_path.name} | ลูกค้า: {result.customer}")
            self.refresh()
            self.render_page()
            if result.customer == "MANUAL_UNKNOWN":
                messagebox.showwarning(
                    "ไม่รู้จักรูปแบบเอกสาร",
                    "โปรแกรมยังไม่มีรูปแบบนี้ในรายการ PDF จะแสดงด้านซ้าย กรุณากด 'เพิ่มรายการ' "
                    "แล้วกรอก Part No., Current QTY และ NO. OF BOX ด้วยตนเอง",
                )
        except Exception as error:
            messagebox.showerror("อ่าน PDF ไม่สำเร็จ", str(error))

    def render_page(self) -> None:
        if self.pdf_document is None:
            return
        pix = self.pdf_document[self.page_index].get_pixmap(
            matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False
        )
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        self.preview_photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(10, 10, image=self.preview_photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, image.width + 20, image.height + 20))
        self.page_label.configure(
            text=f"หน้า {self.page_index + 1} / {self.pdf_document.page_count} | ซูม {int(self.zoom * 100)}%"
        )

    def change_page(self, step: int) -> None:
        if self.pdf_document is None:
            return
        self.page_index = max(0, min(self.pdf_document.page_count - 1, self.page_index + step))
        self.render_page()

    def change_zoom(self, step: float) -> None:
        self.zoom = max(0.45, min(2.0, round(self.zoom + step, 2)))
        self.render_page()

    def selected_index(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("เลือกรายการ", "กรุณาเลือกรายการก่อน")
            return None
        return int(selected[0])

    def edit_selected(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        row = self.rows[index]
        part = simpledialog.askstring("Part No.", "Part No.", initialvalue=row.part_no, parent=self)
        if part is None:
            return
        qty = simpledialog.askinteger("Current QTY", "Current QTY", initialvalue=row.current_qty, minvalue=0, parent=self)
        if qty is None:
            return
        boxes = simpledialog.askinteger("NO. OF BOX", "NO. OF BOX", initialvalue=row.number_of_boxes, minvalue=0, parent=self)
        if boxes is None:
            return
        checked_part = validated_part(part)
        if not checked_part:
            messagebox.showerror("Part No. ไม่ถูกต้อง", "กรุณาตรวจ Part No.")
            return
        self.rows[index] = DeliveryRow(checked_part, qty, boxes, "ผู้ใช้แก้ไข")
        self.verified_rows.discard(index)
        self.ack_var.set(False)
        self.reset_signature()
        self.refresh()

    def add_row(self) -> None:
        if self.result is None:
            messagebox.showinfo("ยังไม่มี PDF", "กรุณาเลือก PDF ก่อน")
            return
        part = simpledialog.askstring("เพิ่มรายการ", "Part No.", parent=self)
        if part is None:
            return
        qty = simpledialog.askinteger("เพิ่มรายการ", "Current QTY", minvalue=0, parent=self)
        if qty is None:
            return
        boxes = simpledialog.askinteger("เพิ่มรายการ", "NO. OF BOX", minvalue=0, parent=self)
        if boxes is None:
            return
        checked_part = validated_part(part)
        if not checked_part:
            messagebox.showerror("Part No. ไม่ถูกต้อง", "กรุณาตรวจ Part No.")
            return
        self.rows.append(DeliveryRow(checked_part, qty, boxes, "ผู้ใช้เพิ่ม"))
        self.ack_var.set(False)
        self.reset_signature()
        self.refresh()

    def verify_selected(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        self.verified_rows.add(index)
        self.reset_signature()
        self.refresh()

    def verify_all(self) -> None:
        if not self.rows:
            return
        if not messagebox.askyesno(
            "ยืนยันการตรวจสอบ",
            "คุณได้เปรียบเทียบ Part No., Current QTY และ NO. OF BOX กับ PDF ครบทุกแถวแล้วใช่หรือไม่?",
        ):
            return
        self.verified_rows = set(range(len(self.rows)))
        self.reset_signature()
        self.refresh()

    def on_signer_changed(self) -> None:
        if self.signed_name and self.signer_var.get().strip() != self.signed_name:
            self.reset_signature()

    def invalidate_signature(self) -> None:
        if self.signed_name:
            self.reset_signature()
        else:
            self.update_generate_state()

    def reset_signature(self) -> None:
        self.signed_name = ""
        self.signed_at = None
        self.sign_status.configure(text="ยังไม่ได้ลงชื่อ", foreground="#b42318")
        self.update_generate_state()

    def sign_review(self) -> None:
        if not self.rows or len(self.verified_rows) != len(self.rows):
            messagebox.showerror("ยังตรวจไม่ครบ", "ต้องยืนยันข้อมูลให้ครบทุกแถวก่อนลงชื่อ")
            return
        if not self.ack_var.get():
            messagebox.showerror("ยังไม่ยอมรับคำเตือน", "กรุณาเลือกช่องยืนยันว่าได้ตรวจสอบข้อมูลกับ PDF แล้ว")
            return
        signer = self.signer_var.get().strip()
        if len(signer) < 2:
            messagebox.showerror("ยังไม่ได้ลงชื่อ", "กรุณากรอกชื่อผู้ตรวจสอบหรือรหัสพนักงาน")
            return
        self.signed_name = signer
        self.signed_at = datetime.now()
        self.sign_status.configure(
            text=f"ลงชื่อแล้ว: {signer} | {self.signed_at.strftime('%d/%m/%Y %H:%M:%S')}",
            foreground="#067647",
        )
        self.update_generate_state()

    def update_generate_state(self) -> None:
        ready = bool(
            self.rows and len(self.verified_rows) == len(self.rows)
            and self.ack_var.get() and self.signed_name and self.signed_at
        )
        self.generate_button.configure(state="normal" if ready else "disabled")

    def generate(self) -> None:
        if not self.pdf_path or not self.rows or self.result is None:
            messagebox.showinfo("ยังไม่มีข้อมูล", "กรุณาเลือก Delivery Order PDF ก่อน")
            return
        if len(self.verified_rows) != len(self.rows) or not self.signed_name or self.signed_at is None:
            messagebox.showerror("ยังไม่พร้อม Generate", "ต้องตรวจครบ ยอมรับคำเตือน และลงชื่อก่อน")
            return
        destination = filedialog.asksaveasfilename(
            title="บันทึก PDF พร้อม QR",
            initialfile=f"{self.pdf_path.stem}_PM75_ONE_PAGE.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
        )
        if not destination:
            return
        try:
            output_path = Path(destination)
            self.result.rows = self.rows
            add_qr_to_pdf(self.pdf_path, output_path, self.result)
            audit_path = output_path.with_suffix(".verification.txt")
            lines = [
                "CHECK TAG_RS - QR GENERATION VERIFICATION",
                f"Source PDF: {self.pdf_path}",
                f"Output PDF: {output_path}",
                f"Customer: {self.result.customer}",
                f"Verified by: {self.signed_name}",
                f"Verified at: {self.signed_at.strftime('%d/%m/%Y %H:%M:%S')}",
                "Warning accepted: YES",
                "",
            ]
            lines.extend(f"{index + 1}. {row.payload} | VERIFIED" for index, row in enumerate(self.rows))
            audit_path.write_text("\n".join(lines), encoding="utf-8-sig")
            messagebox.showinfo("สำเร็จ", f"สร้าง PDF และบันทึกผู้ตรวจสอบแล้ว\n{output_path}\n{audit_path}")
        except Exception as error:
            messagebox.showerror("สร้าง PDF ไม่สำเร็จ", str(error))


if __name__ == "__main__":
    if not configure_tesseract():
        messagebox.showerror(
            "ไม่พบ Tesseract OCR",
            "ชุดโปรแกรมไม่สมบูรณ์: ไม่พบโฟลเดอร์ tesseract กรุณาแตก ZIP ใหม่ทั้งชุด"
        )
    else:
        App().mainloop()
