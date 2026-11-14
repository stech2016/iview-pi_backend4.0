"""Any table (headers + rows) as a CSV, Excel or PDF file. Used by the ERP export and the scheduled reports."""
import csv
import io
import re
import tempfile

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

MEDIA = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}


def build(title: str, headers: list[str], rows: list[list], fmt: str, name: str, group: int = 0):
    """Returns (file object at position 0, file name, media type). CSV / Excel stay flat (every row complete, for import);
    the PDF is for reading: the first `group` columns are shown once per group instead of on every row."""
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    if fmt == "csv":
        t = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="", write_through=True)
        w = csv.writer(t)
        w.writerow(headers)
        w.writerows(rows)
        t.detach()
    elif fmt == "xlsx":
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(re.sub(r'[\[\]:*?/\\]', '-', title)[:31] or 'Sheet')   # Excel forbids  [ ] : * ? / \  in sheet names
        ws.append(headers)
        for r in rows:
            ws.append(r)
        wb.save(tmp)
    else:
        _pdf(tmp, title, headers, rows, group)
    tmp.seek(0)
    return tmp, f"{name}.{fmt}", MEDIA[fmt]


def _txt(v) -> str:
    return "" if v is None else (f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def _pdf(tmp, title: str, headers: list[str], rows: list[list], group: int) -> None:
    """Readable PDF: empty columns dropped, repeated identity columns shown once per group, widths fit the content, long text wraps (nothing is cut)."""
    keep = [i for i in range(len(headers)) if any(_txt(r[i]) != "" for r in rows)] or list(range(len(headers)))
    shown, prev = [], None
    for r in rows:
        cells = []
        for i in keep:
            same = prev is not None and i < group and all(r[j] == prev[j] for j in range(i + 1))
            cells.append("" if same else _txt(r[i]))
        shown.append(cells)
        prev = r
    page = landscape(A4)
    avail = page[0] - 40
    size = 7.5
    want = [max([stringWidth(headers[i], "Helvetica-Bold", size)] + [stringWidth(c[k], "Helvetica", size) for c in shown[:400]]) + 16 for k, i in enumerate(keep)]
    total = sum(want)
    widths = [w * (avail / total) for w in want]   # fill the page width; if too wide, shrink and wrap, so no text is ever cut
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=size, leading=size + 2)
    head = ParagraphStyle("h", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    data = [[Paragraph(headers[i], head) for i in keep]] + [[Paragraph(x.replace("&", "&amp;").replace("<", "&lt;"), cell) for x in c] for c in shown]
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5F9")]), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    # a thin line above every new group so each device / day block is easy to see
    for n, r in enumerate(rows):
        if group and n and any(r[j] != rows[n - 1][j] for j in range(min(group, len(r)))):
            style.append(("LINEABOVE", (0, n + 1), (-1, n + 1), 0.6, colors.HexColor("#8A99AD")))
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle(style))

    def foot(c, doc):
        c.setFont("Helvetica", 7)
        c.drawRightString(page[0] - 20, 14, f"page {doc.page}")

    doc = SimpleDocTemplate(tmp, pagesize=page, leftMargin=20, rightMargin=20, topMargin=36, bottomMargin=26, title=title)
    doc.build([Paragraph(title.replace("&", "&amp;"), ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=12, leading=16, spaceAfter=6)), t],
              onFirstPage=foot, onLaterPages=foot)
