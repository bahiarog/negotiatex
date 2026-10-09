import logging
from pathlib import Path
logger = logging.getLogger(__name__)

async def extract_text(file_path: str, file_name: str) -> str:
    ext = Path(file_name).suffix.lower()
    try:
        if ext == ".pdf": return await _pdf(file_path)
        elif ext in (".xlsx", ".xls"): return await _excel(file_path)
        elif ext == ".csv": return await _csv(file_path)
        elif ext in (".doc", ".docx"): return await _docx(file_path)
        else: return f"[Unsupported: {ext}]"
    except Exception as e:
        logger.error(f"Extraction failed {file_name}: {e}")
        return f"[Error: {e}]"

async def _pdf(path):
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text()
            if t: parts.append(f"--- Page {i+1} ---\n{t}")
            for tbl in page.extract_tables() or []:
                if tbl: parts.append("\n[TABLE]\n" + "\n".join(" | ".join(str(c or "") for c in row) for row in tbl) + "\n[/TABLE]")
    return "\n\n".join(parts) or "[No text extracted]"

async def _excel(path):
    import pandas as pd
    parts = []
    for sheet in pd.ExcelFile(path).sheet_names:
        df = pd.read_excel(path, sheet_name=sheet, header=None).dropna(how="all").fillna("")
        if not df.empty: parts.append(f"--- Sheet: {sheet} ---\n{df.to_string(index=False, header=False)}")
    return "\n\n".join(parts) or "[No data]"

async def _csv(path):
    # Rohtext statt pandas: read_csv nimmt eine Titelzeile als Header und verliert dann Spalten.
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\r\n") for l in f]
    return "\n".join(f"Zeile {i}: {l}" for i, l in enumerate(lines, start=1) if l.strip()) or "[No data]"

async def _docx(path):
    # Absaetze UND Tabellen in Dokumentreihenfolge -- Ratecards stehen in Word fast immer in Tabellen.
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(path)
    parts = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = Paragraph(child, doc).text.strip()
            if t:
                parts.append(t)
        elif tag == "tbl":
            rows = [" | ".join(c.text.strip() for c in row.cells) for row in Table(child, doc).rows]
            if rows:
                parts.append("[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]")
    return "\n".join(parts) or "[No text]"
