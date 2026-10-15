"""Генерация PDF-обращения по 59-ФЗ (reportlab, кириллица через DejaVu)."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .config import ROOT
from .geo import yandex_map_url
from .legal import Qualification
from .routing import Addressee

# Шрифт DejaVu Serif лежит в проекте (fonts/), поэтому PDF собирается на любой ОС.
# Системные папки — только запасной вариант.
_FONT_DIRS = [
    ROOT / "fonts",
    Path(os.getenv("FONT_DIR", "")) if os.getenv("FONT_DIR") else None,
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/Library/Fonts"),
    Path("C:/Windows/Fonts"),
]
_fonts_ready = False


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    tried = []
    for d in filter(None, _FONT_DIRS):
        reg, bold = d / "DejaVuSerif.ttf", d / "DejaVuSerif-Bold.ttf"
        tried.append(str(d))
        if reg.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("GC", str(reg)))
            pdfmetrics.registerFont(TTFont("GC-B", str(bold)))
            pdfmetrics.registerFontFamily("GC", normal="GC", bold="GC-B")
            _fonts_ready = True
            return
    raise RuntimeError(
        "Не найдены шрифты DejaVuSerif.ttf и DejaVuSerif-Bold.ttf. "
        f"Они должны лежать в папке проекта: {ROOT / 'fonts'}. Проверенные пути: " + "; ".join(tried)
    )


@dataclass
class ComplaintData:
    qualification: Qualification
    addressees: list[Addressee]
    lat: float
    lon: float
    applicant_name: str
    applicant_address: str
    applicant_contact: str = ""
    description: str = ""
    observed_at: datetime | None = None
    photo_paths: list[Path] = field(default_factory=list)
    place_hint: str = ""  # например «вблизи г. Гурьевск»


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _prepare_photo(path: Path, max_px: int = 1400) -> tuple[io.BytesIO, float, float]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        buf.seek(0)
        return buf, im.width, im.height


def build_complaint_pdf(d: ComplaintData) -> bytes:
    _register_fonts()
    base = ParagraphStyle("b", fontName="GC", fontSize=10.5, leading=15, alignment=TA_JUSTIFY)
    right = ParagraphStyle("r", parent=base, alignment=TA_RIGHT, leading=14)
    title = ParagraphStyle("t", parent=base, fontName="GC-B", fontSize=13, alignment=TA_CENTER, spaceAfter=6)
    left_small = ParagraphStyle("ls", parent=base, alignment=0, fontSize=9, leading=12)
    small = ParagraphStyle("s", parent=base, fontSize=8.5, leading=11, textColor="#555555")
    q, primary = d.qualification, d.addressees[0]
    copies = d.addressees[1:]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=22 * mm, rightMargin=15 * mm, topMargin=18 * mm,
        bottomMargin=18 * mm, title="Обращение о нарушении природоохранного законодательства",
        author="GreenClaim",
    )
    S: list = []

    # Шапка: адресат и заявитель
    S.append(Paragraph(f"<b>В {_esc(primary.name)}</b>", right))
    if copies:
        S.append(Paragraph("Копия: " + _esc("; ".join(a.name for a in copies)), right))
    S.append(Spacer(1, 4 * mm))
    S.append(Paragraph(f"<b>От:</b> {_esc(d.applicant_name)}", right))
    S.append(Paragraph(f"<b>Адрес для ответа:</b> {_esc(d.applicant_address)}", right))
    if d.applicant_contact:
        S.append(Paragraph(f"<b>Контакты:</b> {_esc(d.applicant_contact)}", right))
    S.append(Spacer(1, 8 * mm))

    S.append(Paragraph("ОБРАЩЕНИЕ (ЖАЛОБА)<br/>о нарушении природоохранного законодательства", title))
    S.append(Spacer(1, 3 * mm))

    # Обстоятельства
    when = d.observed_at.strftime("%d.%m.%Y %H:%M") if d.observed_at else "дата не определена"
    where = f"координаты {d.lat:.6f}, {d.lon:.6f}"
    if d.place_hint:
        where += f" ({_esc(d.place_hint)})"
    facts = f"{when} мною выявлено {_esc(q.violation_label)}. Место нарушения: {where}."
    S.append(Paragraph(f"<b>1. Обстоятельства.</b> {facts}", base))
    S.append(Paragraph(f"Точка на карте: {_esc(yandex_map_url(d.lat, d.lon))}", left_small))
    if d.description:
        S.append(Spacer(1, 2 * mm))
        S.append(Paragraph(f"Дополнительные сведения: {_esc(d.description)}", base))
    if d.photo_paths:
        S.append(Spacer(1, 2 * mm))
        S.append(Paragraph(f"Факт нарушения зафиксирован на фото (приложение, {len(d.photo_paths)} шт.).", base))

    # Правовая квалификация
    S.append(Spacer(1, 4 * mm))
    S.append(Paragraph("<b>2. Правовая квалификация.</b> Описанные действия (бездействие) содержат признаки нарушения следующих норм:", base))
    for i, n in enumerate(q.norms, 1):
        S.append(Paragraph(f"{i}) {_esc(n.render())};", base))

    # Просительная часть
    S.append(Spacer(1, 4 * mm))
    S.append(Paragraph("<b>3. На основании изложенного прошу:</b>", base))
    reqs = list(q.requests)
    reqs.append("о результатах рассмотрения письменно уведомить меня по указанному адресу")
    reqs.append("при отсутствии компетенции направить обращение по подведомственности в соответствующий орган с уведомлением меня об этом (ч. 3 ст. 8 Федерального закона от 02.05.2006 № 59-ФЗ)")
    for i, r in enumerate(reqs, 1):
        S.append(Paragraph(f"{i}) {_esc(r)};", base))
    S.append(Spacer(1, 2 * mm))
    S.append(Paragraph(
        "Ответ прошу дать в срок, установленный ст. 12 Федерального закона № 59-ФЗ "
        "«О порядке рассмотрения обращений граждан Российской Федерации» (30 дней со дня регистрации).",
        base))

    # Подпись
    S.append(Spacer(1, 10 * mm))
    S.append(Paragraph(
        f"Дата: {datetime.now():%d.%m.%Y}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Подпись: ____________ / {_esc(d.applicant_name)} /",
        base))

    # Приложения: фото
    if d.photo_paths:
        S.append(Spacer(1, 6 * mm))
        S.append(Paragraph("<b>Приложение.</b> Фотоматериалы:", base))
        for i, p in enumerate(d.photo_paths, 1):
            data, w, h = _prepare_photo(p)
            max_w, max_h = 160 * mm, 105 * mm
            k = min(max_w / w, max_h / h)
            S.append(KeepTogether([
                Spacer(1, 3 * mm),
                RLImage(data, width=w * k, height=h * k),
                Paragraph(f"Фото {i}", small),
            ]))

    S.append(Spacer(1, 6 * mm))
    S.append(Paragraph("Документ подготовлен с помощью сервиса GreenClaim на основании данных заявителя.", small))
    doc.build(S)
    return buf.getvalue()
