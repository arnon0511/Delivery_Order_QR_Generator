from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass
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
        raise ValueError("PDF ไม่มีหน้าเอกสาร")
    page = document[0]
    text_rows = extract_dnth_text_rows(page)
    if text_rows:
        return text_rows
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
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


def normalize_document_part(text: str) -> str:
    return re.sub(r"\s+", "", text.upper())


def extract_delivery(pdf_path: Path) -> ExtractionResult:
    document = fitz.open(pdf_path)
    page_texts = [page.get_text().upper() for page in document]

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

    rows = extract_dnth_rows(pdf_path)
    return ExtractionResult("DNTH", 0, rows)


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
    def __init__(self) -> None:
        super().__init__()
        self.title("Delivery Order QR Generator v0.5.3 Portable")
        self.geometry("850x520")
        self.minsize(760, 460)
        self.pdf_path: Path | None = None
        self.rows: list[DeliveryRow] = []
        self.result: ExtractionResult | None = None

        ttk.Label(self, text="Delivery Order QR Generator", font=("Segoe UI", 19, "bold")).pack(pady=(18, 4))
        ttk.Label(self, text="อ่าน Current QTY และ NO. OF BOX แล้วเพิ่ม QR โดยไม่แก้ไฟล์ต้นฉบับ").pack()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=24, pady=16)
        ttk.Button(bar, text="1. เลือก Delivery Order PDF", command=self.open_pdf).pack(side="left")
        ttk.Button(bar, text="2. แก้รายการที่เลือก", command=self.edit_selected).pack(side="left", padx=8)
        ttk.Button(bar, text="3. สร้าง PDF พร้อม QR", command=self.generate).pack(side="right")

        self.file_label = ttk.Label(self, text="ยังไม่ได้เลือกไฟล์")
        self.file_label.pack(fill="x", padx=24)

        columns = ("part", "qty", "box", "status")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=14)
        for key, title, width in [
            ("part", "Part No.", 220), ("qty", "Current QTY", 150),
            ("box", "NO. OF BOX", 150), ("status", "สถานะ", 220)
        ]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=24, pady=12)
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())
        ttk.Label(self, text="ต้องตรวจค่าก่อนสร้างทุกครั้ง • QTY = 0 จะไม่สร้าง QR ส่งงาน", foreground="#b42318").pack(pady=(0, 16))

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            status = "ไม่สร้าง QR: QTY เป็น 0" if row.current_qty <= 0 else row.confidence
            self.tree.insert("", "end", iid=str(index), values=(row.part_no, f"{row.current_qty:,}", row.number_of_boxes, status))

    def open_pdf(self) -> None:
        selected = filedialog.askopenfilename(title="เลือก Delivery Order", filetypes=[("PDF", "*.pdf")])
        if not selected:
            return
        self.preview_pdf(Path(selected))

    def preview_pdf(self, selected_path: Path) -> None:
        """Show every PDF page before accepting the document for extraction."""
        try:
            document = fitz.open(selected_path)
            if document.page_count < 1:
                document.close()
                raise ValueError("PDF ไม่มีหน้าเอกสาร")
        except Exception as error:
            messagebox.showerror("เปิดตัวอย่าง PDF ไม่สำเร็จ", str(error))
            return

        preview = tk.Toplevel(self)
        preview.title("ตรวจสอบไฟล์ Delivery Order ก่อนใช้งาน")
        # Fit inside the usable screen on small displays and high DPI scaling.
        # A fixed 760px window can otherwise put the action buttons behind the
        # Windows taskbar on a 768px-high display.
        screen_w = preview.winfo_screenwidth()
        screen_h = preview.winfo_screenheight()
        window_w = max(680, min(1000, screen_w - 80))
        window_h = max(500, min(720, screen_h - 140))
        preview.geometry(f"{window_w}x{window_h}+{max(20, (screen_w - window_w) // 2)}+20")
        preview.minsize(640, 480)
        preview.transient(self)
        preview.grab_set()

        page_index = tk.IntVar(value=0)
        zoom = tk.DoubleVar(value=1.0)
        page_text = tk.StringVar()
        photo_holder: dict[str, ImageTk.PhotoImage] = {}

        ttk.Label(preview, text=selected_path.name, font=("Segoe UI", 12, "bold")).pack(pady=(10, 2))
        ttk.Label(preview, text=str(selected_path), foreground="#555555").pack(padx=16)

        viewer_frame = ttk.Frame(preview)
        viewer_frame.pack(fill="both", expand=True, padx=12, pady=10)
        canvas = tk.Canvas(viewer_frame, background="#777777", highlightthickness=0)
        vertical = ttk.Scrollbar(viewer_frame, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(viewer_frame, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        viewer_frame.rowconfigure(0, weight=1)
        viewer_frame.columnconfigure(0, weight=1)

        def render_page() -> None:
            matrix = fitz.Matrix(1.35 * zoom.get(), 1.35 * zoom.get())
            pix = document[page_index.get()].get_pixmap(matrix=matrix, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            photo = ImageTk.PhotoImage(image)
            photo_holder["page"] = photo
            canvas.delete("all")
            canvas.create_image(12, 12, image=photo, anchor="nw")
            canvas.configure(scrollregion=(0, 0, image.width + 24, image.height + 24))
            canvas.xview_moveto(0)
            canvas.yview_moveto(0)
            page_text.set(f"หน้า {page_index.get() + 1} / {document.page_count}   •   ซูม {int(zoom.get() * 100)}%")

        def change_page(step: int) -> None:
            page_index.set(max(0, min(document.page_count - 1, page_index.get() + step)))
            render_page()

        def change_zoom(step: float) -> None:
            zoom.set(max(0.6, min(2.0, round(zoom.get() + step, 1))))
            render_page()

        def close_preview() -> None:
            document.close()
            preview.grab_release()
            preview.destroy()

        def accept_file() -> None:
            close_preview()
            self.load_confirmed_pdf(selected_path)

        def choose_another_file() -> None:
            close_preview()
            self.after(50, self.open_pdf)

        # Keep navigation and confirmation on separate rows. This prevents the
        # confirmation button from being pushed off-screen by Windows scaling.
        navigation = ttk.Frame(preview)
        navigation.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(navigation, text="◀ หน้าก่อนหน้า", command=lambda: change_page(-1)).pack(side="left")
        ttk.Button(navigation, text="หน้าถัดไป ▶", command=lambda: change_page(1)).pack(side="left", padx=6)
        ttk.Label(navigation, textvariable=page_text).pack(side="left", padx=12)
        ttk.Button(navigation, text="+ ขยาย", command=lambda: change_zoom(0.2)).pack(side="right")
        ttk.Button(navigation, text="− ย่อ", command=lambda: change_zoom(-0.2)).pack(side="right", padx=6)

        actions = ttk.Frame(preview)
        actions.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(actions, text="เลือกไฟล์ใหม่", command=choose_another_file).pack(side="left")
        confirm_button = ttk.Button(actions, text="ยืนยันใช้ไฟล์นี้", command=accept_file)
        confirm_button.pack(side="right", ipadx=18, ipady=5)

        preview.protocol("WM_DELETE_WINDOW", close_preview)
        preview.bind("<Return>", lambda _event: accept_file())
        preview.bind("<Escape>", lambda _event: close_preview())
        render_page()

    def load_confirmed_pdf(self, selected_path: Path) -> None:
        try:
            result = extract_delivery(selected_path)
            self.pdf_path = selected_path
            self.result = result
            self.rows = result.rows
            self.file_label.configure(
                text=f"{self.pdf_path}   |   ลูกค้า: {self.result.customer}   |   หน้า QR: {self.result.page_index + 1}"
            )
            self.refresh()
        except Exception as error:
            messagebox.showerror("อ่าน PDF ไม่สำเร็จ", str(error))

    def edit_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("เลือกรายการ", "กรุณาเลือกรายการที่ต้องการแก้")
            return
        index = int(selected[0])
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
        self.rows[index] = DeliveryRow(checked_part, qty, boxes, "ผู้ใช้ตรวจแล้ว")
        self.refresh()

    def generate(self) -> None:
        if not self.pdf_path or not self.rows or self.result is None:
            messagebox.showinfo("ยังไม่มีข้อมูล", "กรุณาเลือก Delivery Order PDF ก่อน")
            return
        destination = filedialog.asksaveasfilename(
            title="บันทึก PDF พร้อม QR",
            initialfile=f"{self.pdf_path.stem}_PM75_ONE_PAGE.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")]
        )
        if not destination:
            return
        try:
            self.result.rows = self.rows
            add_qr_to_pdf(self.pdf_path, Path(destination), self.result)
            messagebox.showinfo("สำเร็จ", f"สร้างไฟล์แล้ว\n{destination}")
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
