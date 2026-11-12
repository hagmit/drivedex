"""Office Open XML (docx / xlsx / pptx) -> Markdown, standard library only.

Aimed at feeding documents to an LLM: keeps headings, lists, tables, sheet
grids, slide order and speaker notes; drops styling. Legacy binary formats
(.doc / .xls / .ppt) are not supported.
"""
from __future__ import annotations

import datetime as dt
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


SUPPORTED = {".docx": "docx", ".docm": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx",
             ".pptx": "pptx", ".pptm": "pptx"}


def supported(path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED


# ---------------------------------------------------------------- helpers
class Package:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())

    def has(self, name: str) -> bool:
        return name in self.names

    def xml(self, name: str) -> ET.Element:
        return ET.fromstring(self.zip.read(name))

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """rId -> (absolute part name or external URL, relationship type)."""
        folder, base = posixpath.split(part)
        name = posixpath.join(folder, "_rels", base + ".rels")
        if not self.has(name):
            return {}
        out = {}
        for r in self.xml(name):
            target, kind = r.get("Target", ""), r.get("Type", "")
            if r.get("TargetMode") != "External":
                target = target.lstrip("/") if target.startswith("/") else \
                    posixpath.normpath(posixpath.join(folder, target))
            out[r.get("Id")] = (target, kind)
        return out


def _cell(text: str) -> str:
    return text.replace("\r\n", "\n").strip().replace("|", "\\|").replace("\n", "<br>")


def md_table(rows: list[list[str]]) -> str:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(_cell(c) for c in rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


# ------------------------------------------------------------------- docx
class _Docx:
    def __init__(self, pkg: Package):
        self.pkg = pkg
        self.links = {k: v[0] for k, v in pkg.rels("word/document.xml").items()}
        self.styles = {}
        if pkg.has("word/styles.xml"):
            for st in pkg.xml("word/styles.xml").iter(q("w:style")):
                name = st.find(q("w:name"))
                self.styles[st.get(q("w:styleId"))] = (name.get(q("w:val")) if name is not None else "").lower()
        self.numfmt = {}
        if pkg.has("word/numbering.xml"):
            numbering = pkg.xml("word/numbering.xml")
            abstract = {}
            for an in numbering.findall(q("w:abstractNum")):
                for lvl in an.findall(q("w:lvl")):
                    fmt = lvl.find(q("w:numFmt"))
                    abstract[(an.get(q("w:abstractNumId")), lvl.get(q("w:ilvl")))] = \
                        fmt.get(q("w:val")) if fmt is not None else "bullet"
            for num in numbering.findall(q("w:num")):
                aid = num.find(q("w:abstractNumId")).get(q("w:val"))
                for (a, lvl), fmt in abstract.items():
                    if a == aid:
                        self.numfmt[(num.get(q("w:numId")), lvl)] = fmt

    def text(self, el) -> str:
        out = []
        for ch in el:
            tag = ch.tag
            if tag == q("w:t"):
                out.append(ch.text or "")
            elif tag == q("w:tab"):
                out.append("\t")
            elif tag in (q("w:br"), q("w:cr")):
                out.append("\n")
            elif tag == q("w:hyperlink"):
                inner = self.text(ch)
                url = self.links.get(ch.get(q("r:id")))
                out.append(f"[{inner}]({url})" if url and inner.strip() else inner)
            elif tag == q("w:p"):  # paragraphs inside text boxes
                out.append(self.text(ch) + "\n")
            elif tag in (q("w:rPr"), q("w:pPr"), q("w:del"), q("mc:Fallback"), q("w:instrText")):
                continue
            else:
                out.append(self.text(ch))
        return "".join(out)

    def paragraph(self, p) -> str:
        body = self.text(p).strip()
        if not body:
            return ""
        ppr = p.find(q("w:pPr"))
        style, num = "", None
        if ppr is not None:
            ps = ppr.find(q("w:pStyle"))
            if ps is not None:
                style = self.styles.get(ps.get(q("w:val")), ps.get(q("w:val"), "").lower())
            num = ppr.find(q("w:numPr"))
        m = re.fullmatch(r"heading (\d)", style)
        if m:
            return "#" * min(int(m.group(1)) + 1, 6) + " " + body.replace("\n", " ")
        if style == "title":
            return "## " + body.replace("\n", " ")
        if num is not None:
            ilvl = num.find(q("w:ilvl"))
            num_id = num.find(q("w:numId"))
            lvl = ilvl.get(q("w:val")) if ilvl is not None else "0"
            nid = num_id.get(q("w:val")) if num_id is not None else None
            if nid and nid != "0":
                fmt = self.numfmt.get((nid, lvl), "bullet")
                mark = "-" if fmt in ("bullet", "none") else "1."
                return "  " * int(lvl) + f"{mark} " + body.replace("\n", " ")
        return body

    def table(self, tbl) -> str:
        rows = []
        for tr in tbl.findall(q("w:tr")):
            row = []
            for tc in tr.findall(q("w:tc")):
                span, cont = 1, False
                pr = tc.find(q("w:tcPr"))
                if pr is not None:
                    gs = pr.find(q("w:gridSpan"))
                    span = int(gs.get(q("w:val"), "1")) if gs is not None else 1
                    vm = pr.find(q("w:vMerge"))
                    cont = vm is not None and vm.get(q("w:val"), "continue") == "continue"
                row.append("" if cont else self.flat(tc))
                row += [""] * (span - 1)
            rows.append(row)
        return md_table(rows)

    def flat(self, container) -> str:
        parts = []
        for ch in container:
            if ch.tag == q("w:p"):
                parts.append(self.text(ch).strip())
            elif ch.tag == q("w:tbl"):
                parts += [" / ".join(self.flat(tc) for tc in tr.findall(q("w:tc")))
                          for tr in ch.findall(q("w:tr"))]
            elif ch.tag == q("w:sdt"):
                content = ch.find(q("w:sdtContent"))
                if content is not None:
                    parts.append(self.flat(content))
        return "\n".join(p for p in parts if p)

    def blocks(self, container, out):
        for ch in container:
            if ch.tag == q("w:p"):
                out.append(self.paragraph(ch))
            elif ch.tag == q("w:tbl"):
                out.append(self.table(ch))
            elif ch.tag == q("w:sdt"):
                content = ch.find(q("w:sdtContent"))
                if content is not None:
                    self.blocks(content, out)

    def convert(self) -> str:
        out: list[str] = []
        self.blocks(self.pkg.xml("word/document.xml").find(q("w:body")), out)
        text = ""
        for b in (b for b in out if b):
            # consecutive list items form one tight list; everything else is separated by a blank line
            tight = text and LIST_ITEM.match(b) and LIST_ITEM.match(text.rsplit("\n", 1)[-1])
            text += ("\n" if tight else "\n\n" if text else "") + b
        return text


LIST_ITEM = re.compile(r"\s*(?:-|1\.) ")


# ------------------------------------------------------------------- xlsx
# 14-22, 45-47: built-in date/time formats. 27-36, 50-58: East Asian built-ins
# (Japanese Excel saves e.g. yyyy"年"m"月"d"日" as id 31 without a numFmt entry).
BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47} | set(range(27, 37)) | set(range(50, 59))


def is_date_format(code: str) -> bool:
    s = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.', "", code).lower()
    return "general" not in s and bool(re.search(r"[ymdhsge]", s.replace("e+", "").replace("e-", "")))


def serial_to_text(value: float, date1904: bool = False) -> str:
    base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
    stamp = base + dt.timedelta(days=value)
    stamp = (stamp + dt.timedelta(seconds=0.5)).replace(microsecond=0)
    if value < 1 and not date1904:
        return stamp.strftime("%H:%M:%S" if stamp.second else "%H:%M")
    if stamp.hour == stamp.minute == stamp.second == 0:
        return stamp.strftime("%Y-%m-%d")
    return stamp.strftime("%Y-%m-%d %H:%M")


def _number(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f.is_integer() and abs(f) < 1e15 else f"{f:.15g}"


_COLS: dict[str, int] = {}


def _col(ref: str) -> int:
    letters = ref.rstrip("0123456789")
    n = _COLS.get(letters)
    if n is None:
        n = 0
        for ch in letters:
            n = n * 26 + ord(ch) - 64
        n = _COLS[letters] = n - 1
    return n


class _Xlsx:
    def __init__(self, pkg: Package, max_rows: int):
        self.pkg = pkg
        self.max_rows = max_rows
        self.shared = []
        if pkg.has("xl/sharedStrings.xml"):
            self.shared = [self.rich(si) for si in pkg.xml("xl/sharedStrings.xml").findall(q("s:si"))]
        self.date_styles, self.pct_styles = set(), set()
        if pkg.has("xl/styles.xml"):
            st = pkg.xml("xl/styles.xml")
            custom = {int(n.get("numFmtId")): n.get("formatCode", "")
                      for n in st.iter(q("s:numFmt"))}
            xfs = st.find(q("s:cellXfs"))
            for i, xf in enumerate(xfs if xfs is not None else []):
                fid = int(xf.get("numFmtId", "0"))
                code = custom.get(fid, "")
                if fid in BUILTIN_DATE_FORMATS or (code and is_date_format(code)):
                    self.date_styles.add(i)
                elif fid in (9, 10) or (code and "%" in re.sub(r'"[^"]*"', "", code)):
                    self.pct_styles.add(i)

    @staticmethod
    def rich(si) -> str:
        """Text of a shared/inline string. Phonetic runs (rPh, furigana) are skipped."""
        parts = []
        for ch in si:
            if ch.tag == q("s:t"):
                parts.append(ch.text or "")
            elif ch.tag == q("s:r"):
                parts += [t.text or "" for t in ch.findall(q("s:t"))]
        return "".join(parts)

    def value(self, c, date1904) -> str:
        t = c.get("t", "n")
        if t == "inlineStr":
            node = c.find(q("s:is"))
            return self.rich(node) if node is not None else ""
        v = c.find(q("s:v"))
        if v is None or v.text is None:
            return ""
        raw = v.text
        if t == "s":
            return self.shared[int(raw)]
        if t == "b":
            return "TRUE" if raw == "1" else "FALSE"
        if t in ("str", "e", "d"):
            return raw
        style = int(c.get("s", "0"))
        if style in self.date_styles:
            try:
                return serial_to_text(float(raw), date1904)
            except (ValueError, OverflowError):
                return raw
        if style in self.pct_styles:
            try:
                return _number(str(round(float(raw) * 100, 10))) + "%"
            except ValueError:
                return raw
        return _number(raw)

    def sheet(self, part: str, date1904: bool) -> tuple[str, bool]:
        """Stream the sheet XML row by row and stop once max_rows non-empty rows are read."""
        grid: dict[tuple[int, int], str] = {}
        rows: list[int] = []
        truncated = False
        row_tag, cell_tag = q("s:row"), q("s:c")
        last = -1
        with self.pkg.zip.open(part) as stream:
            for _, row in ET.iterparse(stream, events=("end",)):
                if row.tag != row_tag:
                    continue
                r = int(row.get("r")) - 1 if row.get("r") else last + 1
                last = r
                cells = {}
                for i, c in enumerate(row.iter(cell_tag)):
                    ref = c.get("r")
                    text = self.value(c, date1904)
                    if text.strip():
                        cells[_col(ref) if ref else i] = text
                row.clear()
                if not cells:
                    continue
                if len(rows) == self.max_rows:
                    truncated = True
                    break
                rows.append(r)
                for col, text in cells.items():
                    grid[(r, col)] = text
        if not grid:
            return "", False
        cols = sorted({k[1] for k in grid})
        table = [[grid.get((r, c), "") for c in cols] for r in rows]
        return md_table(table), truncated

    def convert(self) -> str:
        wb = self.pkg.xml("xl/workbook.xml")
        pr = wb.find(q("s:workbookPr"))
        date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        rels = self.pkg.rels("xl/workbook.xml")
        out = []
        for sh in wb.iter(q("s:sheet")):
            target = rels.get(sh.get(q("r:id")), ("", ""))[0]
            if not self.pkg.has(target):
                continue  # chart sheets etc.
            hidden = "（非表示）" if sh.get("state") in ("hidden", "veryHidden") else ""
            body, truncated = self.sheet(target, date1904)
            note = f"\n\n（先頭 {self.max_rows:,} 行まで）" if truncated else ""
            out.append(f"## {sh.get('name')}{hidden}\n\n{body or '（空）'}{note}")
        return "\n\n".join(out)


# ------------------------------------------------------------------- pptx
class _Pptx:
    def __init__(self, pkg: Package):
        self.pkg = pkg

    @staticmethod
    def paragraphs(tx_body) -> list[tuple[int, str, bool]]:
        out = []
        for p in tx_body.findall(q("a:p")):
            parts = []
            for ch in p:
                if ch.tag in (q("a:r"), q("a:fld")):
                    t = ch.find(q("a:t"))
                    parts.append((t.text or "") if t is not None else "")
                elif ch.tag == q("a:br"):
                    parts.append("\n")
            text = "".join(parts).strip()
            if not text:
                continue
            ppr = p.find(q("a:pPr"))
            lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
            bullet = ppr is not None and (ppr.find(q("a:buChar")) is not None or ppr.find(q("a:buAutoNum")) is not None)
            out.append((lvl, text, bullet))
        return out

    @staticmethod
    def position(el) -> tuple[int, int] | None:
        for xfrm in (el.find(f"{q('p:spPr')}/{q('a:xfrm')}"), el.find(f"{q('p:grpSpPr')}/{q('a:xfrm')}"),
                     el.find(q("p:xfrm"))):
            if xfrm is not None:
                off = xfrm.find(q("a:off"))
                if off is not None:
                    return int(off.get("y", "0")), int(off.get("x", "0"))
        return None

    def table(self, tbl) -> str:
        rows = []
        for tr in tbl.findall(q("a:tr")):
            row = []
            for tc in tr.findall(q("a:tc")):
                if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                    row.append("")
                    continue
                body = tc.find(q("a:txBody"))
                row.append("\n".join(t for _, t, _ in self.paragraphs(body)) if body is not None else "")
            rows.append(row)
        return md_table(rows)

    def shapes(self, tree, items, title):
        """Collect (y, x, order, markdown) for every shape; returns the title text."""
        for el in tree:
            pos = self.position(el)
            if el.tag == q("p:sp"):
                ph = el.find(f"{q('p:nvSpPr')}/{q('p:nvPr')}/{q('p:ph')}")
                ph_type = ph.get("type", "body") if ph is not None else None
                body = el.find(q("p:txBody"))
                if body is None:
                    continue
                paras = self.paragraphs(body)
                if not paras:
                    continue
                if ph_type in ("title", "ctrTitle") and not title:
                    title = " ".join(t.replace("\n", " ") for _, t, _ in paras)
                    continue
                if ph_type in ("sldNum", "dt", "ftr"):
                    continue
                as_list = ph_type in ("body", "obj") or any(b for _, _, b in paras)
                lines = [("  " * lvl + "- " if as_list else "") + t.replace("\n", "<br>" if as_list else "\n")
                         for lvl, t, _ in paras]
                items.append((pos, len(items), "\n".join(lines)))
            elif el.tag == q("p:graphicFrame"):
                tbl = el.find(f".//{q('a:tbl')}")
                if tbl is not None:
                    items.append((pos, len(items), self.table(tbl)))
            elif el.tag == q("p:grpSp"):
                inner: list = []
                title = self.shapes(el, inner, title)
                inner.sort(key=self._key)
                if inner:
                    items.append((pos, len(items), "\n\n".join(t for _, _, t in inner)))
        return title

    @staticmethod
    def _key(item):
        pos, order, _ = item
        if pos is None:
            return (-1, 0, order)  # inherited placement (placeholders): keep document order first
        y, x = pos
        return (y // 300000, x, order)  # same visual row (~0.8 cm) reads left to right

    def notes(self, slide_part) -> str:
        for target, kind in self.pkg.rels(slide_part).values():
            if kind.endswith("/notesSlide") and self.pkg.has(target):
                tree = self.pkg.xml(target)
                texts = []
                for sp in tree.iter(q("p:sp")):
                    ph = sp.find(f"{q('p:nvSpPr')}/{q('p:nvPr')}/{q('p:ph')}")
                    body = sp.find(q("p:txBody"))
                    if ph is not None and ph.get("type") == "body" and body is not None:
                        texts += [t for _, t, _ in self.paragraphs(body)]
                return "\n".join(texts)
        return ""

    def convert(self) -> str:
        pres = self.pkg.xml("ppt/presentation.xml")
        rels = self.pkg.rels("ppt/presentation.xml")
        out = []
        ids = pres.find(q("p:sldIdLst"))
        for n, sid in enumerate(ids if ids is not None else [], 1):
            part = rels.get(sid.get(q("r:id")), ("", ""))[0]
            if not self.pkg.has(part):
                continue
            slide = self.pkg.xml(part)
            tree = slide.find(f"{q('p:cSld')}/{q('p:spTree')}")
            items: list = []
            title = self.shapes(tree, items, "") if tree is not None else ""
            items.sort(key=self._key)
            hidden = "（非表示）" if slide.get("show") == "0" else ""
            block = [f"## {n}. {title}{hidden}" if title else f"## {n}.{hidden}"]
            block += [t for _, _, t in items if t]
            note = self.notes(part)
            if note:
                block.append("> ノート: " + note.replace("\n", "\n> "))
            out.append("\n\n".join(block))
        return "\n\n".join(out)


# -------------------------------------------------------------------- API
def convert(path, *, max_rows: int = 5000) -> str:
    path = Path(path)
    kind = SUPPORTED.get(path.suffix.lower())
    if not kind:
        raise ValueError(f"未対応の形式です: {path.suffix}（docx / xlsx / pptx）")
    try:
        pkg = Package(path)
    except zipfile.BadZipFile:
        raise ValueError(f"Office Open XML として読めません（暗号化・破損の可能性）: {path.name}") from None
    with pkg.zip:  # always release the file handle (Windows cannot delete open files)
        if kind == "docx":
            body = _Docx(pkg).convert()
        elif kind == "xlsx":
            body = _Xlsx(pkg, max_rows).convert()
        else:
            body = _Pptx(pkg).convert()
    return f"# {path.name}\n\n{body}\n"


def convert_file(path, out_dir=None) -> Path:
    path = Path(path)
    # keep the original extension (a.docx.md / a.xlsx.md) so same-named files never overwrite each other
    out = Path(out_dir or path.parent) / (path.name + ".md")
    out.write_text(convert(path), "utf-8", newline="\n")
    return out
