"""Google ドキュメント類を書き出して `get --md` で Markdown にする流れの模擬テスト。

HTTP は urllib の手前（http._open）だけを差し替えるので、書き出し形式の選択・保存名・
http.download の書き込み・officemd の呼び出しまで本物のコードが動く。
書き出されるファイルの中身は Google の実物ではなく、同じ部品構成を手で組んだもの。
"""
import io
import tempfile
import unittest
import urllib.error
import urllib.parse
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

from drivedex import http
from drivedex.cli import main
from drivedex.providers import Cursor, Item, SourceSpec, gdrive
from drivedex.providers.base import safe_filename

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
PKG_RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def _zip(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return buf.getvalue()


def _rels(*targets) -> str:
    rows = "".join(f'<Relationship Id="rId{n}" Type="http://x/{kind}" Target="{t}"/>'
                   for n, (kind, t) in enumerate(targets, 1))
    return f"<Relationships {PKG_RELS}>{rows}</Relationships>"


def google_docx() -> bytes:
    # Google の書き出しは styleId が "Heading1"、表示名が "heading 1"
    styles = (f"<w:styles {W}>"
              '<w:style w:styleId="Title"><w:name w:val="Title"/></w:style>'
              '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>')
    body = (f"<w:document {W}><w:body>"
            '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>週次定例</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>決定事項</w:t></w:r></w:p>'
            "<w:p><w:r><w:t>来週リリース</w:t></w:r></w:p></w:body></w:document>")
    return _zip({"word/document.xml": body, "word/styles.xml": styles})


def google_xlsx() -> bytes:
    workbook = (f"<workbook {S} {R}><sheets>"
                '<sheet name="売上" sheetId="1" r:id="rId1"/></sheets></workbook>')
    shared = f'<sst {S}><si><t>日付</t></si><si><t>金額</t></si></sst>'
    styles = (f'<styleSheet {S}><numFmts><numFmt numFmtId="164" formatCode="yyyy/m/d"/></numFmts>'
              '<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>')
    sheet = (f"<worksheet {S}><sheetData>"
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" s="1"><v>46337</v></c><c r="B2"><v>1200</v></c></row>'
             "</sheetData></worksheet>")
    return _zip({"xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": _rels(("worksheet", "worksheets/sheet1.xml")),
                 "xl/sharedStrings.xml": shared, "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": sheet})


def google_pptx() -> bytes:
    pres = f'<p:presentation {P} {R}><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'

    def shape(ph, text, y):
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/><p:nvPr><p:ph type="{ph}"/></p:nvPr></p:nvSpPr>'
                f'<p:spPr><a:xfrm><a:off x="0" y="{y}"/></a:xfrm></p:spPr>'
                f"<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>")
    slide = (f"<p:sld {P} {A}><p:cSld><p:spTree>"
             + shape("title", "四半期の振り返り", 0) + shape("body", "目標を達成", 1000000)
             + "</p:spTree></p:cSld></p:sld>")
    return _zip({"ppt/presentation.xml": pres, "ppt/_rels/presentation.xml.rels": _rels(("slide", "slides/slide1.xml")),
                 "ppt/slides/slide1.xml": slide})


class FakeDrive:
    """Stands in for urllib: answers export / alt=media URLs, records each request."""

    def __init__(self, exports=None, media=b"", error=None):
        self.exports, self.media, self.error, self.calls = exports or {}, media, error, []

    def __call__(self, method, url, headers, data, follow, timeout):
        self.calls.append((url, dict(headers or {})))
        if self.error:
            status, body = self.error
            raise urllib.error.HTTPError(url, status, "err", Message(), io.BytesIO(body.encode()))
        u = urllib.parse.urlparse(url)
        qs = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/export"):
            return io.BytesIO(self.exports[qs["mimeType"]])
        assert qs.get("alt") == "media", url
        return io.BytesIO(self.media)


def drive():
    p = gdrive.DriveProvider({"client_id": "cid"}, store=None)
    p.access_token = lambda: "tok"
    return p


SPEC = SourceSpec("Google マイドライブ", gdrive.MY_DRIVE, "root1")


class ExportDownloadTest(unittest.TestCase):
    def run_download(self, item, fake):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(http, "_open", fake):
            dest = drive().download(SPEC, item, d)
            return Path(dest).relative_to(d).as_posix(), Path(dest).read_bytes()

    def test_export_format_and_name(self):
        fake = FakeDrive({DOCX: b"D", XLSX: b"X", PPTX: b"P", "application/pdf": b"F"})
        for mime, name, want in (
                ("application/vnd.google-apps.document", "議事録", "議事録.docx"),
                ("application/vnd.google-apps.spreadsheet", "売上", "売上.xlsx"),
                ("application/vnd.google-apps.presentation", "報告", "報告.pptx"),
                ("application/vnd.google-apps.drawing", "構成図", "構成図.pdf"),
                # アップロード時に変換された文書は名前に拡張子が残っている
                ("application/vnd.google-apps.document", "報告書.docx", "報告書.docx"),
                ("application/vnd.google-apps.spreadsheet", "集計.XLSX", "集計.XLSX")):
            with self.subTest(name=name):
                rel, _ = self.run_download(Item("f1", None, name, False, mime=mime), fake)
                self.assertEqual(rel, want)
        url, headers = fake.calls[0]
        self.assertTrue(url.startswith(gdrive.API + "/files/f1/export?"))
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_unsafe_names_stay_in_dest_dir(self):
        fake = FakeDrive({DOCX: b"D"}, media=b"M")
        rel, _ = self.run_download(Item("f", None, "2026/11/13 打合せ: 案?", False,
                                        mime="application/vnd.google-apps.document"), fake)
        self.assertEqual(rel, "2026_11_13 打合せ_ 案_.docx")
        rel, body = self.run_download(Item("g", None, "../秘密.pdf", False, mime="application/pdf"), fake)
        self.assertEqual((rel, body), (".._秘密.pdf", b"M"))
        self.assertIn("alt=media", fake.calls[-1][0])

    def test_safe_filename(self):
        self.assertEqual(safe_filename("a\\b|c<d>.txt"), "a_b_c_d_.txt")
        self.assertEqual(safe_filename("末尾. "), "末尾")
        self.assertEqual(safe_filename(".."), "_")
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename("nul.txt"), "_nul.txt")
        self.assertEqual(safe_filename("console.txt"), "console.txt")

    def test_non_exportable_google_types(self):
        fake = FakeDrive()
        for kind in ("form", "shortcut", "site"):
            with self.subTest(kind=kind), self.assertRaises(ValueError) as cm:
                self.run_download(Item("f", None, "アンケート", False, mime=f"application/vnd.google-apps.{kind}"), fake)
            self.assertIn(kind, str(cm.exception))
        self.assertEqual(fake.calls, [])  # no doomed request is sent

    def test_export_size_limit(self):
        body = '{"error":{"code":403,"errors":[{"reason":"exportSizeLimitExceeded"}]}}'
        with self.assertRaises(ValueError) as cm:
            self.run_download(Item("f", None, "巨大", False, mime="application/vnd.google-apps.document"),
                              FakeDrive(error=(403, body)))
        self.assertIn("10MB", str(cm.exception))
        with self.assertRaises(http.HttpError):  # other errors are not disguised
            self.run_download(Item("f", None, "x", False, mime="application/vnd.google-apps.document"),
                              FakeDrive(error=(404, "notFound")))


class GetMarkdownTest(unittest.TestCase):
    """drivedex get --md を索引から通しで動かす。"""

    FILES = [("d", "application/vnd.google-apps.document", "週次定例"),
             ("s", "application/vnd.google-apps.spreadsheet", "売上"),
             ("p", "application/vnd.google-apps.presentation", "振り返り"),
             ("f", "application/vnd.google-apps.form", "アンケート")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.out = Path(self.home, "dl")
        from drivedex.cli import App
        app = App(Path(self.home))
        self.sid = app.index.add_source("google", "google", SPEC)
        items = [Item("root1", None, "マイドライブ", True)]
        items += [Item(i, "root1", name, False, mime=mime) for i, mime, name in self.FILES]
        app.index.apply(self.sid, items + [Cursor("c")], full=True)
        app.index.close()
        self.fake = FakeDrive({DOCX: google_docx(), XLSX: google_xlsx(), PPTX: google_pptx()})

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        with mock.patch("drivedex.providers.get", lambda *a, **k: drive()), \
                mock.patch.object(http, "_open", self.fake), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            code = main(["--home", self.home, "get", str(self.sid), path, "-o", str(self.out), "--md"])
        return code, "".join(c.args[0] for c in err.write.call_args_list)

    def test_document(self):
        self.assertEqual(self.get("/週次定例")[0], 0)
        self.assertTrue((self.out / "週次定例.docx").is_file())
        md = (self.out / "週次定例.docx.md").read_text("utf-8")
        self.assertEqual(md, "# 週次定例.docx\n\n## 週次定例\n\n## 決定事項\n\n来週リリース\n")

    def test_spreadsheet(self):
        self.assertEqual(self.get("/売上")[0], 0)
        md = (self.out / "売上.xlsx.md").read_text("utf-8")
        self.assertIn("## 売上", md)
        self.assertIn("| 日付 | 金額 |", md)
        self.assertIn("| 2026-11-11 | 1200 |", md)

    def test_presentation(self):
        self.assertEqual(self.get("/振り返り")[0], 0)
        md = (self.out / "振り返り.pptx.md").read_text("utf-8")
        self.assertIn("## 1. 四半期の振り返り", md)
        self.assertIn("- 目標を達成", md)

    def test_form_is_a_clear_error(self):
        code, err = self.get("/アンケート")
        self.assertEqual(code, 2)
        self.assertIn("form", err)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
