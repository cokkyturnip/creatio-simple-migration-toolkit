"""Minimal Markdown -> DOCX converter for the subset used in PANDUAN-PEMAKAIAN.md."""
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

SRC, DST = Path(sys.argv[1]), Path(sys.argv[2])
lines = SRC.read_text(encoding="utf-8").splitlines()

doc = Document()
for section in doc.sections:
    section.left_margin = section.right_margin = Cm(2.2)
    section.top_margin = section.bottom_margin = Cm(2)

normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(10.5)
normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
for name, size in (("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)):
    st = doc.styles[name]
    st.font.name = "Calibri"
    st.font.size = Pt(size)
    st.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

TOKEN = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def add_inline(paragraph, text, *, size=None, italic=False):
    for part in TOKEN.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(0x9C, 0x27, 0x27)
        elif part.startswith("[") and "](" in part:
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part).groups()
            run = paragraph.add_run(label)
            run.underline = True
            run.font.color.rgb = RGBColor(0x1F, 0x5F, 0xAA)
            if not url.startswith("#") and not url.startswith(".."):
                # external link: show the URL once for printed copies
                note = paragraph.add_run(f" ({url})")
                note.font.size = Pt(8.5)
                note.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            continue
        else:
            run = paragraph.add_run(part)
        if size:
            run.font.size = Pt(size)
        if italic:
            run.italic = True


def shade(cell, hex_color):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def add_table(rows):
    header = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    body = []
    for r in rows[2:]:
        body.append([c.strip() for c in r.strip().strip("|").split("|")])
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, text in enumerate(header):
        cell = table.rows[0].cells[i]
        cell.text = ""
        add_inline(cell.paragraphs[0], text, size=9.5)
        for run in cell.paragraphs[0].runs:
            run.bold = True
        shade(cell, "DCE6F1")
    for row in body:
        cells = table.add_row().cells
        for i in range(len(header)):
            text = row[i] if i < len(row) else ""
            cells[i].text = ""
            add_inline(cells[i].paragraphs[0], text, size=9.5)
    # repeat header row on each page
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)
    doc.add_paragraph()


def add_code(block):
    for code_line in block or [""]:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Cm(0.5)
        run = p.add_run(code_line if code_line else " ")
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.strip()

    if stripped.startswith("```"):
        block = []
        i += 1
        while i < len(lines) and not lines[i].strip().startswith("```"):
            block.append(lines[i])
            i += 1
        add_code(block)
        i += 1
        continue

    if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
        rows = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            rows.append(lines[i])
            i += 1
        add_table(rows)
        continue

    if stripped == "---" or not stripped:
        i += 1
        continue

    if stripped.startswith("### "):
        doc.add_heading(stripped[4:], level=3)
    elif stripped.startswith("## "):
        doc.add_heading(stripped[3:], level=2)
    elif stripped.startswith("# "):
        doc.add_heading(stripped[2:], level=1)
    elif stripped.startswith("> "):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.6)
        add_inline(p, stripped[2:], italic=False)
        for run in p.runs:
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    elif re.match(r"^\s*- ", line):
        p = doc.add_paragraph(style="List Bullet")
        add_inline(p, re.sub(r"^\s*- ", "", line))
    elif re.match(r"^\d+\. ", stripped):
        number = re.match(r"^(\d+)\. ", stripped).group(1)
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.9)
        p.paragraph_format.first_line_indent = Cm(-0.6)
        p.paragraph_format.space_after = Pt(3)
        p.add_run(f"{number}.  ")
        add_inline(p, re.sub(r"^\d+\. ", "", stripped))
    else:
        p = doc.add_paragraph()
        add_inline(p, stripped)
    i += 1

# footer with page numbers
footer = doc.sections[0].footer.paragraphs[0]
footer.text = "Creatio Simple Migration Toolkit — Panduan Pemakaian"
footer.runs[0].font.size = Pt(8)

doc.save(DST)
print("saved", DST)
