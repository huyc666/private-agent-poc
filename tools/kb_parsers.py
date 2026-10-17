"""知识库文档解析器（v0.17.24，PLAN 2.1）：多格式 → 伪 Markdown。

索引期专用：由 tools/build_kb.py 调用，不进包、不进运行时。
- .docx   python-docx   Heading 样式 → 标题行，表格行 →「列：列」行
- .pdf    pypdf         每页 →「第N页」章节；无文本层（扫描件）→ 报错跳过
- .xlsx   openpyxl      Sheet → 章节，行内非空单元格用「：」连接
- .pptx   python-pptx   页标题（无标题则「第N页」）→ 章节，页内文本框逐行
- .html/.htm  stdlib HTMLParser   h1-h6 → 标题行，块级文本 → 段落（去 script/style）

统一输出伪 Markdown（标题行 + 段落），复用 build_kb.chunk_markdown 分块。
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


def parse_document(path: Path) -> str:
    """按扩展名分派解析，返回伪 Markdown 全文；不支持/失败抛 KbParseError。"""
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


def _parse_docx(path: Path) -> str:
    try:
        import docx  # python-docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:
        raise KbParseError("缺少依赖 python-docx（pip install python-docx）") from e
    try:
        d = docx.Document(str(path))
        lines: list[str] = []
        for block in d.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if not text:
                    continue
                m = re.match(r"heading (\d)", (block.style.name or "").lower())
                if m and int(m.group(1)) <= 4:
                    lines.append("#" * int(m.group(1)) + " " + text)
                else:
                    lines.append(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        lines.append("：".join(cells))
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"docx 解析失败（{type(e).__name__}），文件可能损坏") from e
    text = "\n".join(lines)
    if not text.strip():
        raise KbParseError("docx 未提取到文本内容")
    return text


# ---------------------------------------------------------------- pdf


def _parse_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise KbParseError("缺少依赖 pypdf（pip install pypdf）") from e
    try:
        reader = PdfReader(str(path))
        sections: list[str] = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                sections.append(f"## 第{i}页\n\n{t}")
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"pdf 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("PDF 无文本层（疑似扫描件），已跳过（本管道不做 OCR）")
    return "\n\n".join(sections)


# ---------------------------------------------------------------- xlsx


def _parse_xlsx(path: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise KbParseError("缺少依赖 openpyxl（pip install openpyxl）") from e
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        sections: list[str] = []
        for ws in wb.worksheets:
            lines = [f"## {ws.title}"]
            for row in ws.iter_rows(values_only=True):
                cells = [str(v) for v in row if v is not None and str(v).strip()]
                if cells:
                    lines.append("：".join(cells))
            if len(lines) > 1:
                sections.append("\n".join(lines))
        wb.close()
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"xlsx 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("xlsx 未提取到有效内容")
    return "\n\n".join(sections)


# ---------------------------------------------------------------- pptx


def _parse_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise KbParseError("缺少依赖 python-pptx（pip install python-pptx）") from e
    try:
        prs = Presentation(str(path))
        sections: list[str] = []
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
            heading = title or f"第{i}页"
            body = "\n".join(texts)
            sections.append(f"## {heading}\n\n{body}" if body else f"## {heading}")
    except Exception as e:
        if isinstance(e, KbParseError):
            raise
        raise KbParseError(f"pptx 解析失败（{type(e).__name__}），文件可能损坏") from e
    if not sections:
        raise KbParseError("pptx 未提取到有效内容")
    return "\n\n".join(sections)


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


def _parse_html(path: Path) -> str:
    parser = _HTMLText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    text = "\n\n".join(parser.lines)
    if not text.strip():
        raise KbParseError("html 未提取到有效内容")
    return text
