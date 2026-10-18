"""Генерация отчётов для подачи в ФНС.

PDF (ReportLab):
  • 2-3 страницы: титул с реквизитами плательщика, сводная таблица
    доходов и расходов, разбивка НДФЛ по ступеням, перечень операций.
  • Подпись и дата — на последней странице.

XLSX (openpyxl):
  • Лист «Сводка» — итоги по году.
  • Лист «Доходы» — все income_events (аирдропы, майнинг, стейкинг).
  • Лист «Реализация» — все disposal_events (продажи, обмены, оплаты).
  • Лист «НДФЛ» — разбивка по ступеням.
  • Лист «Предупреждения» — warnings и skipped.

CSV:
  • «Доходы» и «Реализация» отдельными файлами — для загрузки в ЛК ФНС
    или других декларационных программ (Декларация 3-НДФЛ и т.п.).
"""

from __future__ import annotations

import csv
import io
import logging
import time
from decimal import Decimal
from typing import IO, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from utils.price_oracle import format_rub
from utils.tax_calc import YearSummary, summary_to_dict

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ PDF


def _register_cyrillic_font() -> Optional[str]:
    """Регистрация шрифта с поддержкой кириллицы для PDF.

    Пробует DejaVuSans (обычно есть в /usr/share/fonts на Linux). Если
    ничего не нашлось — возвращает None, и PDF будет в латинице (с
    transliteration, не идеально, но хоть что-то). Render-контейнеры и
    большинство Linux-окружений имеют DejaVu.
    """
    candidates = [
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("DejaVuSans", "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        # Windows-окружение разработчика
        ("DejaVuSans", "C:/Windows/Fonts/DejaVuSans.ttf"),
        # macOS
        ("DejaVuSans", "/Library/Fonts/DejaVuSans.ttf"),
        ("Arial", "C:/Windows/Fonts/arial.ttf"),
    ]
    for name, path in candidates:
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            continue
    return None


def render_pdf(summary: YearSummary, settings: dict) -> bytes:
    """Сгенерировать PDF-отчёт за год в байтах."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Отчёт 3-НДФЛ по криптовалюте за {summary.year}",
        author="Allira Crypto Bot",
    )
    font_name = _register_cyrillic_font()
    base_font = font_name or "Helvetica"

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"],
                        fontName=base_font, fontSize=15, leading=18,
                        textColor=colors.HexColor("#1a1a1a"))
    h2 = ParagraphStyle("H2", parent=styles["Heading2"],
                        fontName=base_font, fontSize=11, leading=14,
                        textColor=colors.HexColor("#1a1a1a"))
    body = ParagraphStyle("Body", parent=styles["Normal"],
                          fontName=base_font, fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=styles["Normal"],
                           fontName=base_font, fontSize=8, leading=10,
                           textColor=colors.HexColor("#666666"))

    story: list = []

    # Титул
    story.append(Paragraph(
        f"ОТЧЁТ ПО ОПЕРАЦИЯМ С ЦИФРОВОЙ ВАЛЮТОЙ<br/>"
        f"за {summary.year} налоговый период",
        h1))
    story.append(Spacer(1, 4 * mm))

    inn = settings.get("taxpayer_inn") or "—"
    name = settings.get("taxpayer_name") or "—"
    status_ru = {"resident": "резидент РФ",
                 "nonresident": "нерезидент",
                 "miner": "майнер (резидент)"}.get(
        settings.get("tax_status") or "resident", "резидент РФ")
    method_ru = {"fifo": "FIFO (первый пришёл — первый ушёл)",
                "wac": "средневзвешенная"}.get(
        settings.get("cost_basis") or "fifo", "FIFO")
    meta = [
        ["Плательщик:", name],
        ["ИНН:", inn],
        ["Статус:", status_ru],
        ["Метод расчёта себестоимости:", method_ru],
        ["Период:", f"01.01.{summary.year} — 31.12.{summary.year}"],
        ["Дата формирования:", time.strftime("%d.%m.%Y %H:%M:%S")],
    ]
    t = Table(meta, colWidths=[60 * mm, 110 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#cccccc")),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    # Сводка
    story.append(Paragraph("Сводка по году", h2))
    summary_rows = [
        ["Показатель", "Значение (RUB)"],
        ["Доход (аирдропы, майнинг, стейкинг и т.п.)",
         format_rub(float(summary.total_income_rub))],
        ["Расходы (себестоимость реализованных активов)",
         format_rub(float(summary.total_cost_rub))],
        ["Финансовый результат от реализации",
         format_rub(float(summary.realized_gain_rub))],
        ["Налоговая база",
         format_rub(float(summary.taxable_income_rub))],
        ["НДФЛ к уплате",
         format_rub(float(summary.ndfl_total))],
    ]
    t = Table(summary_rows, colWidths=[110 * mm, 60 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("FONTNAME", (0, 0), (-1, 0), base_font),
        ("FONTWEIGHT", (0, 0), (-1, 0), "BOLD"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    # НДФЛ по ступеням
    if summary.ndfl_breakdown:
        story.append(Paragraph("Расчёт НДФЛ по прогрессивной шкале ст. 224 НК РФ", h2))
        rows = [["Ступень", "База, RUB", "Ставка", "Налог, RUB"]]
        for step in summary.ndfl_breakdown:
            rows.append([
                str(step.get("bracket", "")),
                format_rub(float(step.get("base", 0))),
                f"{float(step.get('rate', 0)) * 100:.0f}%",
                format_rub(float(step.get("tax", 0))),
            ])
        rows.append(["", "", "Итого:", format_rub(float(summary.ndfl_total))])
        t = Table(rows, colWidths=[55 * mm, 45 * mm, 25 * mm, 45 * mm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), base_font),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
            ("FONTWEIGHT", (0, 0), (-1, 0), "BOLD"),
            ("FONTWEIGHT", (-2, -1), (-1, -1), "BOLD"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(t)
        story.append(Spacer(1, 6 * mm))

    # Доходы
    if summary.income_events:
        story.append(Paragraph(
            f"Доходы ({len(summary.income_events)} событий)", h2))
        rows = [["Дата", "Актив", "Кол-во", "Тип", "Доход, RUB", "Комментарий"]]
        for ev in summary.income_events:
            rows.append([
                time.strftime("%d.%m.%Y", time.localtime(ev.ts)),
                ev.asset_symbol,
                f"{ev.qty:.8f}".rstrip("0").rstrip("."),
                _short_type_label(ev.tx_type),
                format_rub(float(ev.income_rub)),
                (ev.note or "")[:40],
            ])
        t = Table(rows, colWidths=[20 * mm, 16 * mm, 30 * mm,
                                    30 * mm, 30 * mm, 44 * mm],
                  repeatRows=1)
        t.setStyle(_table_style(base_font))
        story.append(t)
        story.append(Spacer(1, 6 * mm))

    # Реализация
    if summary.disposal_events:
        story.append(Paragraph(
            f"Реализация ({len(summary.disposal_events)} событий)", h2))
        rows = [["Дата", "Актив", "Кол-во", "Выручка", "Расход", "Прибыль"]]
        for ev in summary.disposal_events:
            rows.append([
                time.strftime("%d.%m.%Y", time.localtime(ev.ts)),
                ev.asset_symbol,
                f"{ev.qty:.8f}".rstrip("0").rstrip("."),
                format_rub(float(ev.proceeds_rub)),
                format_rub(float(ev.cost_rub)),
                format_rub(float(ev.gain_rub)),
            ])
        rows.append(["", "", "", "", "Итого прибыль:",
                     format_rub(float(summary.realized_gain_rub))])
        t = Table(rows, colWidths=[20 * mm, 16 * mm, 30 * mm,
                                    30 * mm, 30 * mm, 44 * mm],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), base_font),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
            ("FONTWEIGHT", (0, 0), (-1, 0), "BOLD"),
            ("FONTWEIGHT", (-2, -1), (-1, -1), "BOLD"),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 6 * mm))

    # Предупреждения
    if summary.warnings:
        story.append(Paragraph("Замечания к отчёту", h2))
        for w in summary.warnings:
            story.append(Paragraph(f"• {w}", body))
        story.append(Spacer(1, 6 * mm))

    # Подвал
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "Документ сформирован автоматически. Перед подачей в ФНС рекомендуется "
        "сверить данные с выписками бирж и блокчейн-обозревателями. "
        "Используйте файл <b>tax_report_&lt;год&gt;_details.xlsx</b> для детальной проверки.",
        small))

    doc.build(story)
    return buf.getvalue()


def _short_type_label(t: str) -> str:
    return {
        "airdrop": "Аирдроп",
        "mining": "Майнинг",
        "staking": "Стейкинг",
        "reward": "Награда",
        "interest": "Процент",
        "gift_in": "Подарок вход.",
        "refund": "Возврат",
        "income_other": "Прочий",
        "sell": "Продажа",
        "swap_out": "Обмен",
        "spend": "Оплата",
        "gift_out": "Подарок исх.",
        "sell_nft": "Продажа NFT",
        "disposal_other": "Прочее",
    }.get(t, t)


def _table_style(font_name: str) -> TableStyle:
    return TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("FONTWEIGHT", (0, 0), (-1, 0), "BOLD"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])


# ------------------------------------------------------------------ XLSX


def render_xlsx(summary: YearSummary, settings: dict) -> bytes:
    """Детальная XLSX-книга: 5 листов."""
    wb = Workbook()
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDDDDD")

    # 1. Сводка
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Параметр", "Значение"])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
    ws.append(["Налоговый период", str(summary.year)])
    ws.append(["Плательщик", settings.get("taxpayer_name") or "—"])
    ws.append(["ИНН", settings.get("taxpayer_inn") or "—"])
    ws.append(["Статус", settings.get("tax_status") or "resident"])
    ws.append(["Метод себестоимости", settings.get("cost_basis") or "fifo"])
    ws.append([""])
    ws.append(["Доход (аирдропы, майнинг и т.п.), RUB",
               float(summary.total_income_rub)])
    ws.append(["Себестоимость реализованных активов, RUB",
               float(summary.total_cost_rub)])
    ws.append(["Фин. результат реализации, RUB",
               float(summary.realized_gain_rub)])
    ws.append(["Налоговая база, RUB",
               float(summary.taxable_income_rub)])
    ws.append(["НДФЛ к уплате, RUB", float(summary.ndfl_total)])
    ws.append([""])
    ws.append(["Дата формирования",
               time.strftime("%d.%m.%Y %H:%M:%S")])
    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 50

    # 2. Доходы
    ws = wb.create_sheet("Доходы")
    ws.append(["Дата", "Актив", "Количество", "Тип", "Доход, RUB",
               "Комментарий", "ID операции"])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
    for ev in summary.income_events:
        ws.append([
            time.strftime("%d.%m.%Y", time.localtime(ev.ts)),
            ev.asset_symbol,
            float(ev.qty),
            ev.tx_type,
            float(ev.income_rub),
            ev.note,
            ev.tx_id,
        ])
    for col, width in enumerate([12, 10, 16, 16, 14, 40, 8], 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    # 3. Реализация
    ws = wb.create_sheet("Реализация")
    ws.append(["Дата", "Актив", "Количество", "Выручка, RUB",
               "Расход, RUB", "Прибыль, RUB", "Комментарий", "ID операции"])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
    for ev in summary.disposal_events:
        ws.append([
            time.strftime("%d.%m.%Y", time.localtime(ev.ts)),
            ev.asset_symbol,
            float(ev.qty),
            float(ev.proceeds_rub),
            float(ev.cost_rub),
            float(ev.gain_rub),
            ev.note,
            ev.tx_id,
        ])
    for col, width in enumerate([12, 10, 16, 14, 14, 14, 40, 8], 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    # 4. НДФЛ
    ws = wb.create_sheet("НДФЛ")
    ws.append(["Ступень", "База, RUB", "Ставка", "Налог, RUB"])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
    for step in summary.ndfl_breakdown:
        ws.append([
            step.get("bracket", ""),
            float(step.get("base", 0)),
            float(step.get("rate", 0)),
            float(step.get("tax", 0)),
        ])
    ws.append(["Итого", "", "", float(summary.ndfl_total)])
    for col, width in enumerate([35, 18, 12, 18], 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    # 5. Замечания
    ws = wb.create_sheet("Замечания")
    ws.append(["Тип", "Сообщение"])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
    for w in summary.warnings:
        ws.append(["warning", w])
    for s in summary.skipped:
        ws.append(["skipped", str(s)])
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ CSV


def render_income_csv(summary: YearSummary) -> bytes:
    """CSV с income_events для импорта в сторонние декларационные программы."""
    return _csv_of(summary.income_events, [
        ("Дата", lambda ev: time.strftime("%Y-%m-%d", time.localtime(ev.ts))),
        ("Актив", lambda ev: ev.asset_symbol),
        ("Кол-во", lambda ev: str(ev.qty)),
        ("Тип", lambda ev: ev.tx_type),
        ("Доход (RUB)", lambda ev: str(ev.income_rub)),
        ("Комментарий", lambda ev: ev.note),
        ("Tx ID", lambda ev: str(ev.tx_id)),
    ])


def render_disposal_csv(summary: YearSummary) -> bytes:
    """CSV с disposal_events."""
    return _csv_of(summary.disposal_events, [
        ("Дата", lambda ev: time.strftime("%Y-%m-%d", time.localtime(ev.ts))),
        ("Актив", lambda ev: ev.asset_symbol),
        ("Кол-во", lambda ev: str(ev.qty)),
        ("Выручка (RUB)", lambda ev: str(ev.proceeds_rub)),
        ("Расход (RUB)", lambda ev: str(ev.cost_rub)),
        ("Прибыль (RUB)", lambda ev: str(ev.gain_rub)),
        ("Комментарий", lambda ev: ev.note),
        ("Tx ID", lambda ev: str(ev.tx_id)),
    ])


def _csv_of(events: list, columns: list[tuple[str, callable]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([name for name, _ in columns])
    for ev in events:
        writer.writerow([fn(ev) for _, fn in columns])
    return buf.getvalue().encode("utf-8-sig")  # BOM для Excel в РФ