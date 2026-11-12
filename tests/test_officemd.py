import tempfile
import unittest
import zipfile
from pathlib import Path

from drivedex import officemd
from drivedex.officemd import is_date_format, md_table, serial_to_text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' \
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" ' \
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ' \
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def rels(*items):
    body = "".join(f'<Relationship Id="{i}" Type="http://x/{t}" Target="{g}"{m}/>'
                   for i, t, g, m in items)
    return f"<Relationships {REL}>{body}</Relationships>"


def build(path, parts: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


def run(text):
    return f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"


class DocxTest(unittest.TestCase):
    def test_docx(self):
        body = (
            f'<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr>{run("概要")}</w:p>'
            f'<w:p>{run("本文と")}<w:hyperlink r:id="rIdL">{run("リンク")}</w:hyperlink></w:p>'
            f'<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="5"/></w:numPr></w:pPr>{run("項目")}</w:p>'
            f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="6"/></w:numPr></w:pPr>{run("手順")}</w:p>'
            f'<w:p>{run("削除前")}<w:del><w:r><w:delText>消えた</w:delText></w:r></w:del></w:p>'
            '<w:tbl>'
            f'<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p>{run("見出しA|B")}</w:p></w:tc>'
            f'<w:tc><w:p>{run("C")}</w:p></w:tc></w:tr>'
            f'<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p>{run("1行目")}</w:p><w:p>{run("2行目")}</w:p></w:tc>'
            f'<w:tc><w:p>{run("x")}</w:p></w:tc><w:tc><w:p>{run("y")}</w:p></w:tc></w:tr>'
            f'<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
            f'<w:tc><w:p>{run("z")}</w:p></w:tc><w:tc><w:p>{run("w")}</w:p></w:tc></w:tr>'
            '</w:tbl>'
        )
        styles = (f'<w:styles {W}><w:style w:styleId="1"><w:name w:val="heading 1"/></w:style></w:styles>')
        numbering = (f'<w:numbering {W}>'
                     '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
                     '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
                     '<w:num w:numId="5"><w:abstractNumId w:val="0"/></w:num>'
                     '<w:num w:numId="6"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
        with tempfile.TemporaryDirectory() as d:
            f = build(Path(d, "仕様.docx"), {
                "word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>",
                "word/styles.xml": styles,
                "word/numbering.xml": numbering,
                "word/_rels/document.xml.rels": rels(("rIdL", "hyperlink", "https://example.com/a", ' TargetMode="External"')),
            })
            md = officemd.convert(f)
        self.assertIn("# 仕様.docx", md)
        self.assertIn("## 概要", md)
        self.assertIn("本文と[リンク](https://example.com/a)", md)
        self.assertIn("  - 項目\n1. 手順", md)  # consecutive list items stay one tight list
        self.assertIn("削除前", md)
        self.assertNotIn("消えた", md)
        self.assertIn("| 見出しA\\|B |  | C |", md)
        self.assertIn("| 1行目<br>2行目 | x | y |", md)
        self.assertIn("|  | z | w |", md)


class XlsxTest(unittest.TestCase):
    def test_xlsx(self):
        shared = (f'<sst {S}>'
                  '<si><t>品名</t></si>'
                  '<si><r><t>東京</t></r><r><t>本社</t></r><rPh sb="0" eb="2"><t>トウキョウ</t></rPh></si>'
                  '<si><t>日付</t></si></sst>')
        styles = (f'<styleSheet {S}><numFmts><numFmt numFmtId="176" formatCode="[$-411]ggge&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/>'
                  '<numFmt numFmtId="177" formatCode="#,##0&quot;円&quot;"/></numFmts>'
                  '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="176"/><xf numFmtId="177"/>'
                  '<xf numFmtId="20"/><xf numFmtId="9"/><xf numFmtId="31"/></cellXfs></styleSheet>')
        sheet1 = (f'<worksheet {S}><sheetData>'
                  '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>2</v></c></row>'
                  '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="C3" s="1"><v>46337</v></c><c r="D3" s="3"><v>1200</v></c></row>'
                  '<row r="4"><c r="A4" t="inlineStr"><is><t>a|b</t></is></c><c r="C4" s="2"><v>46337.5</v></c>'
                  '<c r="D4" s="4"><v>0.75</v></c><c r="E4" s="5"><v>0.125</v></c><c r="F4" t="b"><v>1</v></c>'
                  '<c r="G4"><v>0.30000000000000004</v></c></row>'
                  '<row r="5"><c r="A5" s="6"><v>46357</v></c></row>'
                  '</sheetData></worksheet>')
        wb = (f'<workbook {S}><sheets><sheet name="一覧" sheetId="1" r:id="rId1"/>'
              '<sheet name="裏" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>')
        with tempfile.TemporaryDirectory() as d:
            f = build(Path(d, "台帳.xlsx"), {
                "xl/workbook.xml": wb,
                "xl/_rels/workbook.xml.rels": rels(("rId1", "worksheet", "worksheets/sheet1.xml", ""),
                                                   ("rId2", "worksheet", "/xl/worksheets/sheet2.xml", "")),
                "xl/sharedStrings.xml": shared,
                "xl/styles.xml": styles,
                "xl/worksheets/sheet1.xml": sheet1,
                "xl/worksheets/sheet2.xml": f'<worksheet {S}><sheetData/></worksheet>',
            })
            md = officemd.convert(f)
        self.assertIn("## 一覧", md)
        self.assertIn("| 品名 | 日付 |  |  |  |  |", md)  # empty column B dropped, empty row 2 dropped
        self.assertIn("| 東京本社 | 2026-11-11 | 1200 |", md)  # furigana (rPh) removed
        self.assertNotIn("トウキョウ", md)
        self.assertIn("| a\\|b | 2026-11-11 12:00 | 18:00 | 12.5% | TRUE | 0.3 |", md)
        self.assertIn("| 2026-12-01 |", md)  # Japanese built-in date format id 31
        self.assertIn("## 裏（非表示）\n\n（空）", md)

    def test_max_rows(self):
        rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 11))
        with tempfile.TemporaryDirectory() as d:
            f = build(Path(d, "big.xlsx"), {
                "xl/workbook.xml": f'<workbook {S}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": rels(("rId1", "worksheet", "worksheets/sheet1.xml", "")),
                "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>{rows}</sheetData></worksheet>",
            })
            md = officemd.convert(f, max_rows=3)
        self.assertIn("| 3 |", md)
        self.assertNotIn("| 4 |", md)
        self.assertIn("先頭 3 行まで", md)


class PptxTest(unittest.TestCase):
    def sp(self, text, *, ph=None, x=None, y=None, bullet=False, lvl=0):
        nv = f'<p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/><p:nvPr>{f"<p:ph type={chr(34)}{ph}{chr(34)}/>" if ph else ""}</p:nvPr></p:nvSpPr>'
        xfrm = f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="1" cy="1"/></a:xfrm>' if x is not None else ""
        bu = '<a:buChar char="•"/>' if bullet else ""
        return (f'<p:sp>{nv}<p:spPr>{xfrm}</p:spPr><p:txBody><a:bodyPr/>'
                f'<a:p><a:pPr lvl="{lvl}">{bu}</a:pPr><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')

    def test_pptx(self):
        table = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="t"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
                 '<p:xfrm><a:off x="0" y="5000000"/><a:ext cx="1" cy="1"/></p:xfrm><a:graphic><a:graphicData><a:tbl>'
                 '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>項目</a:t></a:r></a:p></a:txBody></a:tc><a:tc hMerge="1"><a:txBody><a:p/></a:txBody></a:tc></a:tr>'
                 '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>A</a:t></a:r></a:p></a:txBody></a:tc>'
                 '<a:tc><a:txBody><a:p><a:r><a:t>B</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
                 '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
        tree = (self.sp("右下", x=6000000, y=3000000) + self.sp("左下", x=100, y=3050000)
                + self.sp("タイトル", ph="title") + self.sp("要点", ph="body") + self.sp("詳細", ph="body", lvl=1)
                + self.sp("12", ph="sldNum") + table)
        slide = f'<p:sld {P}><p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>'
        notes = f'<p:notes {P}><p:cSld><p:spTree>{self.sp("話す内容", ph="body")}</p:spTree></p:cSld></p:notes>'
        hidden = f'<p:sld {P} show="0"><p:cSld><p:spTree>{self.sp("予備", bullet=True)}</p:spTree></p:cSld></p:sld>'
        pres = (f'<p:presentation {P}><p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId3"/>'
                '</p:sldIdLst></p:presentation>')
        with tempfile.TemporaryDirectory() as d:
            f = build(Path(d, "説明.pptx"), {
                "ppt/presentation.xml": pres,
                "ppt/_rels/presentation.xml.rels": rels(("rId2", "slide", "slides/slide1.xml", ""),
                                                        ("rId3", "slide", "slides/slide2.xml", "")),
                "ppt/slides/slide1.xml": slide,
                "ppt/slides/_rels/slide1.xml.rels": rels(("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml", "")),
                "ppt/notesSlides/notesSlide1.xml": notes,
                "ppt/slides/slide2.xml": hidden,
            })
            md = officemd.convert(f)
        self.assertIn("## 1. タイトル", md)
        order = [md.index(s) for s in ("- 要点", "  - 詳細", "左下", "右下", "| 項目 |  |")]
        self.assertEqual(order, sorted(order))  # placeholders, then same visual row left to right, then table
        self.assertNotIn("12", md.replace("説明", ""))
        self.assertIn("> ノート: 話す内容", md)
        self.assertIn("## 2.（非表示）\n\n- 予備", md)


class HelperTest(unittest.TestCase):
    def test_formats(self):
        self.assertTrue(is_date_format("yyyy/m/d"))
        self.assertTrue(is_date_format('[$-411]ggge"年"m"月"d"日"'))
        self.assertTrue(is_date_format("h:mm"))
        self.assertFalse(is_date_format('#,##0"円"'))
        self.assertFalse(is_date_format("0.00E+00"))
        self.assertFalse(is_date_format("[Red]#,##0;(#,##0)"))
        self.assertFalse(is_date_format("General"))
        self.assertEqual(serial_to_text(1461.0, date1904=True), "1908-01-01")  # 1904 is a leap year
        self.assertEqual(serial_to_text(46337.0), "2026-11-11")

    def test_table_and_errors(self):
        self.assertEqual(md_table([["a", "b"], ["", ""], ["1"]]), "| a | b |\n|---|---|\n| 1 |  |")
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "x.docx")
            bad.write_bytes(b"not a zip")
            with self.assertRaises(ValueError):
                officemd.convert(bad)
            with self.assertRaises(ValueError):
                officemd.convert(Path(d, "x.doc"))


if __name__ == "__main__":
    unittest.main()
