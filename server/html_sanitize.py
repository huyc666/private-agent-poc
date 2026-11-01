"""HTML 白名单消毒器（v0.17.37 P0-2，spec 4.2，审核报告 §4）。

Mammoth 前端还原 docx 后直写 innerHTML——恶意文档携带的 HTML 片段可在
原文页形成存储型 XSS。离线约束（无 bleach/nh3，lxml.html.clean 已从 lxml 5+
剥离），基于 stdlib html.parser 自研白名单消毒：

- 标签白名单：结构/排版语义（Mammoth 输出全集：段落/标题/列表/表格/引用/代码）
- 属性白名单：a[href,title]、img[src,alt]、td/th[colspan,rowspan]，
  其余标签零属性——事件属性（onerror 等）不存在放行可能
- URL 白名单：http/https/mailto/相对路径；img 另放行 data:image/*
- 主动内容（script/iframe/svg/math 等）连内容整体移除；
  危险 URL 的容器标签剥壳保文（img 为 void 无文本可保 → 整标签丢弃）
- 未知标签剥壳保留文本；输出全部转义（实体不二次解码）

纯 stdlib 实现，供 POST /api/kb/sanitize 服务端消毒通道使用（spec 4.3）。
"""
import html as _html
import re
from html.parser import HTMLParser

# 标签白名单（结构/排版语义）
_TAGS = frozenset(
    "p h1 h2 h3 h4 h5 h6 strong em b i u s sup sub "
    "ol ul li table thead tbody tfoot tr td th "
    "a img br blockquote code pre dl dt dd span".split())

# 属性白名单（按标签；未列出 = 不接受任何属性 → 事件属性必被丢弃）
_ATTRS = {
    "a": frozenset(("href", "title")),
    "img": frozenset(("src", "alt")),
    "td": frozenset(("colspan", "rowspan")),
    "th": frozenset(("colspan", "rowspan")),
}

# URL 属性（值经 _safe_url 校验；img 额外放行 data:image/*）
_URL_ATTRS = {"a": "href", "img": "src"}

# 主动内容·容器型：连内容整体移除（压 drop 栈与闭合标签配对）
_DROP_TREE = frozenset(
    "script style iframe object embed applet form button select "
    "textarea video audio svg math".split())

# 主动内容·void 型：无闭合标签、无文本，出现即丢（不压栈）
_DROP_VOID = frozenset("input link meta base source".split())

# void 标签（无闭合形态；br 白名单直放，img 走 URL 校验）
_VOID = frozenset(("br", "img"))

_SCHEME_SAFE = frozenset(("http", "https", "mailto"))


def _safe_url(url: str, tag: str) -> bool:
    """URL scheme 白名单：http/https/mailto/相对路径放行；img 另放行
    data:image/*。先剥控制字符——浏览器解析 URL 时忽略 tab/LF/CR，
    不剥则 `jav\\tascript:` 可走私 scheme 检查。"""
    u = re.sub(r"[\x00-\x1f\x7f]", "", url or "").strip()
    head = re.split(r"[/?#]", u, 1)[0]
    if ":" not in head:
        return True  # scheme 出现在路径/查询/锚点之后 → 相对路径
    scheme = head.split(":", 1)[0].lower()
    if scheme in _SCHEME_SAFE:
        return True
    if tag == "img" and scheme == "data":
        # data:<mediatype>[;base64],<payload> —— 只放行 image/*
        mediatype = u.split(":", 1)[1].split(",", 1)[0].split(";", 1)[0]
        return mediatype.strip().lower().startswith("image/")
    return False


class _Sanitizer(HTMLParser):
    """白名单消毒器：_suppress 栈双模式——"drop"（子树全丢，压栈配对
    嵌套主动内容）、"strip"（剥壳保文，子内容照常处理）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._suppress: list[tuple[str, str]] = []  # [(tag, "drop"|"strip")]

    def handle_starttag(self, tag, attrs):
        if self._suppress and self._suppress[-1][1] == "drop":
            # 已在被丢子树内：只跟踪嵌套的主动内容用于闭合配对，其余静默
            if tag in _DROP_TREE:
                self._suppress.append((tag, "drop"))
            return
        if tag in _DROP_TREE:
            self._suppress.append((tag, "drop"))
            return
        if tag in _DROP_VOID:
            return
        if tag not in _TAGS:
            self._suppress.append((tag, "strip"))  # 未知标签剥壳保文
            return
        url_attr = _URL_ATTRS.get(tag)
        url_val = (dict(attrs).get(url_attr) or "") if url_attr else ""
        if url_val and not _safe_url(url_val, tag):
            if tag == "img":
                return  # void 无文本可保 → 危险 URL 整标签丢弃
            self._suppress.append((tag, "strip"))  # 容器标签剥壳保文
            return
        parts = [tag]
        for name, val in attrs:
            if name in _ATTRS.get(tag, ()):
                parts.append(f' {name}="{_html.escape(val or "", quote=True)}"')
        self._out.append(f"<{''.join(parts)}>")

    def handle_endtag(self, tag):
        if self._suppress:
            top_tag, top_mode = self._suppress[-1]
            if tag == top_tag:
                self._suppress.pop()
                return
            if top_mode == "drop":
                return  # 被丢子树内的杂散闭合标签一并吞掉
            # strip 模式：子内容照常处理，落穿到常规闭合
        if tag in _TAGS and tag not in _VOID:
            self._out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._suppress and self._suppress[-1][1] == "drop":
            return
        # convert_charrefs=True 下实体已解码为字面文本，输出统一转义
        # ——实体不会被二次解码成标签（&lt;script&gt; 只能以文本呈现）
        self._out.append(_html.escape(data, quote=False))


def sanitize_html(src: str) -> str:
    """HTML 白名单消毒：返回只含白名单标签/属性/URL 的安全片段。"""
    s = _Sanitizer()
    s.feed(src or "")
    s.close()
    return "".join(s._out)
