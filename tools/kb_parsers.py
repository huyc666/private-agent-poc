"""知识库文档解析器（v0.17.25，PLAN 2.1/2.2）：多格式 → 伪 Markdown + 行→锚点映射。

索引期专用：由 tools/build_kb.py 调用，不进包、不进运行时（server 侧定位端点
复用同一解析器做「按锚点取片段」，同样不进包）。
- .docx   python-docx   Heading 样式 → 标题行，表格行 →「列：列」行；锚点=段落序号
- .pdf    pypdf         每页 →「第N页」章节；无文本层（扫描件）→ 报错跳过；锚点=页码
- .xlsx   openpyxl      Sheet → 章节，行内非空单元格用「：」连接；锚点=Sheet+行号
- .pptx   python-pptx   页标题（无标题则「第N页」）→ 章节，页内文本框逐行；锚点=页码
- .html/.htm  stdlib HTMLParser   h1-h6 → 标题行，块级文本 → 段落；锚点=输出行号

统一输出（伪 Markdown 文本, anchors）二元组：anchors 与 text.splitlines()
逐行对齐，每行为一个锚点 dict 或 None（空行/分隔行）。锚点结构：
    {"kind": "para"|"page"|"sheet"|"slide"|"line", "a": N, "b": M?, "sheet": 名?}
kind=line 时 a 为伪 Markdown 输出行号（1 起）；文档锚点与分块无关，
由 build_kb.chunk_markdown_anchored 按块聚合。
所有依赖均为纯 pip 离线可装的纯 Python 包（零系统依赖、零 JVM）。
约定：单文档解析失败抛 KbParseError（message 不含绝对路径），由调用方
跳过 + WARNING，不阻塞整库索引。
"""
import re
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_MB = 50  # 单文件解析大小上限（防畸形大文件拖垮内存，超限由调用方跳过）


class KbParseError(Exception):
    """解析不可继续（格式不支持/文件损坏/无文本层等）；message 面向索引报告。"""


def _join_sections(sections: list[list[tuple[str, dict | None]]]) -> tuple[str, list]:
    """各 section 的 (行, 锚点) 列表 → (text, anchors)。
    section 之间以一个空行分隔（锚点 None），保持与旧版「\\n\\n 连接」一致的版面。"""
    all_lines: list[tuple[str, dict | None]] = []
    for i, sec in enumerate(sections):
        if i:
            all_lines.append(("", None))
        all_lines.extend(sec)
    text = "\n".join(l for l, _ in all_lines)
    anchors = [a for _, a in all_lines]
    return text, anchors


def parse_document(path: Path) -> tuple[str, list]:
    """按扩展名分派解析，返回 (伪 Markdown 全文, 行锚点列表)；不支持/失败抛 KbParseError。"""
    ext = path.suffix.lower()
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".xlsx":
        return _parse_xlsx(path)
    if ext == ".pptx":
        return _parse_pptx(path)
    if ext in (".html", ".htm"):
        return _parse_html(path)
    raise KbParseError(
        f"不支持的老格式 .{ext.lstrip('.')}，请转存为 docx/xlsx/pptx 等新格式后重试")


# ---------------------------------------------------------------- docx


def _parse_docx(path: Path) -> tuple[str, list]:
    try:
        import docx  # python-docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:
        raise KbParseError("缺少依赖 python-docx（pip install python-docx）") from e
    try:
        d = docx.Document(str(path))
        lines: list[tuple[str, dict | None]] = []
        para_no = 0  # 段落序号（输出行即伪段落，表格行独立计数，1 起）
        for block in d.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if not text:
                    continue
                para_no += 1
                m = re.match(r"heading (\d)", (block.style.name or "").lower())
                if m and int(m.group(1)) <= 4:
                    lines.append(("#" * int(m.group(1)) + " " + text,
                                  {"kind": "para", "a": para_no}))
                else:
                    lines.append((text, {"kind": "para", "a": para_no}))
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        para_no += 1
                        lines.append(("：".join(cells), {"kind": "para", "a": para_no}))
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"docx 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not lines:
        raise KbParseError("docx 未提取到文本内容")
    return _join_sections([lines])


# ---------------------------------------------------------------- pdf


def _parse_pdf(path: Path) -> tuple[str, list]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise KbParseError("缺少依赖 pypdf（pip install pypdf）") from e
    try:
        reader = PdfReader(str(path))
        sections: list[list[tuple[str, dict | None]]] = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if not t:
                continue
            anchor = {"kind": "page", "a": i}
            sec: list[tuple[str, dict | None]] = [(f"## 第{i}页", anchor), ("", None)]
            sec.extend((ln, anchor) for ln in t.splitlines())
            sections.append(sec)
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"pdf 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("PDF 无文本层（疑似扫描件），已跳过（本管道不做 OCR）")
    return _join_sections(sections)


# ---------------------------------------------------------------- xlsx


def _parse_xlsx(path: Path) -> tuple[str, list]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise KbParseError("缺少依赖 openpyxl（pip install openpyxl）") from e
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        sections: list[list[tuple[str, dict | None]]] = []
        for ws in wb.worksheets:
            sec: list[tuple[str, dict | None]] = [(f"## {ws.title}", None), ("", None)]
            row_no = 0
            for row in ws.iter_rows(values_only=True):
                cells = [str(v) for v in row if v is not None and str(v).strip()]
                if cells:
                    row_no += 1
                    sec.append(("：".join(cells),
                                {"kind": "sheet", "sheet": ws.title, "a": row_no}))
            if row_no:
                sections.append(sec)
        wb.close()
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"xlsx 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("xlsx 未提取到有效内容")
    return _join_sections(sections)


# ---------------------------------------------------------------- pptx


def _parse_pptx(path: Path) -> tuple[str, list]:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise KbParseError("缺少依赖 python-pptx（pip install python-pptx）") from e
    try:
        prs = Presentation(str(path))
        sections: list[list[tuple[str, dict | None]]] = []
        for i, slide in enumerate(prs.slides, 1):
            title = ""
            try:
                if slide.shapes.title is not None:
                    title = slide.shapes.title.text.strip()
            except Exception:
                title = ""
            texts: list[str] = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t and t != title:
                        texts.append(t)
            if not texts and not title:
                continue
            anchor = {"kind": "slide", "a": i}
            heading = title or f"第{i}页"
            sec: list[tuple[str, dict | None]] = [(f"## {heading}", anchor)]
            body = "\n".join(texts)
            if body:
                sec.append(("", None))
                sec.extend((ln, anchor) for ln in body.splitlines())
            sections.append(sec)
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"pptx 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("pptx 未提取到有效内容")
    return _join_sections(sections)


# ---------------------------------------------------------------- html


class _HTMLText(HTMLParser):
    """提取可见文本：h1-h6 → 标题行，块级元素 → 独立段落；跳过 script/style。"""

    _HEAD = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _BLOCK = {"p", "div", "li", "tr", "td", "th", "section", "article", "br",
              "ul", "ol", "table", "pre", "blockquote", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skip = 0        # script/style 嵌套深度
        self._head = 0        # 当前标题级别（0=普通块）
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif self._skip:
            return
        elif tag in self._HEAD:
            self._flush()
            self._head = self._HEAD[tag]
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in self._HEAD and self._head:
            self._flush()
            self._head = 0
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip == 0 and data.strip():
            self._buf.append(data.strip())

    def _flush(self):
        if self._buf:
            text = "".join(self._buf)
            self._buf = []
            if text:
                if self._head:
                    self.lines.append("#" * min(self._head, 4) + " " + text)
                else:
                    self.lines.append(text)

    def close(self):
        super().close()
        self._flush()


def _parse_html(path: Path) -> tuple[str, list]:
    parser = _HTMLText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    if not parser.lines:
        raise KbParseError("html 未提取到有效内容")
    lines = [(ln, {"kind": "line", "a": i + 1})
             for i, ln in enumerate(parser.lines)]
    return _join_sections([lines])
