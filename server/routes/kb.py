"""知识库定位端点（v0.17.25，PLAN 2.2）：Mammoth 输出消毒通道 + 原文定位视图。
自 server/main.py 原样搬迁。

_kb_home / _declared_kb_packs / kb_allowed 保留在 server.main（测试以
server.main 属性为 monkeypatch 锚点），本模块经函数内延迟 import 访问。
"""
import asyncio
import logging
import re
import secrets
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from server import auth, html_sanitize
from server.webdeps import KbSanitizeRequest, _acl_identity
from tools import kb_parsers  # 定位端点按锚点重解析文档（服务器侧，不进包）

logger = logging.getLogger(__name__)

router = APIRouter()

_KB_DOC_MAX = 200  # 文档名长度上限（防超长名资源浪费；正常文档名远小于此）

_KB_SANITIZE_MAX = 2 * 1024 * 1024  # Mammoth 输出消毒上限（超限 413，防无界处理）


@router.post("/api/kb/sanitize")
async def kb_sanitize(request: Request):
    """Mammoth 输出消毒通道（v0.17.37 P0-2，spec 4.3）：docx 原文页前端还原后
    不再直写 innerHTML，先提交服务端白名单消毒（html_sanitize）再入页。
    认证由中间件统一管（开放模式与 source 端点同口径）；cookie 身份的
    POST 自带 X-Requested-With（前端 authHdr 统一附加，过 CSRF 校验）。
    评审 #1 修复：Content-Length 前置预检（对齐上传端点 _check_body_limit
    先例）——超限直接 413，不把恶意超大 body 全量读进内存（无该头的
    chunked 请求读完后再兜底校验）。"""
    cl = request.headers.get("content-length")
    try:
        if cl and int(cl) > _KB_SANITIZE_MAX:
            return JSONResponse({"error": "payload too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "非法 Content-Length"}, status_code=400)
    try:
        body = await request.json()
        req = KbSanitizeRequest(**body)
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=422)
    if len(req.html) > _KB_SANITIZE_MAX:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    out = await asyncio.to_thread(html_sanitize.sanitize_html, req.html)
    return {"html": out}


def _source_page(title: str, body: str) -> HTMLResponse:
    """片段视图公共壳：纯内联样式（离线前端约束），命中行 .hl 高亮。
    v0.17.37 P0-2（spec 4.1/4.4）：标题两处入页先转义（文档名含引号/& 时
    不得突破元素文本上下文）；无脚本页配简版 CSP + X-Frame-Options。"""
    resp = HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>"
        "body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;"
        "margin:24px;color:#1f2937;max-width:900px}"
        "h1{font-size:16px}.path{color:#6b7280;font-size:13px;margin-bottom:16px}"
        ".kb-line{display:flex;gap:12px;padding:2px 8px;line-height:1.7;"
        "border-left:3px solid transparent}"
        ".kb-line.hl{background:#fef9c3;border-left-color:#eab308}"
        ".ln{color:#9ca3af;min-width:3em;text-align:right;user-select:none;"
        "font-size:12px;padding-top:3px}"
        "p{margin:4px 0;line-height:1.7}"
        "p.hl{background:#fef9c3;outline:2px solid #eab308}"
        "</style></head><body>"
        f"<h1>知识库原文</h1><div class='path'>{_esc(title)}</div>{body}</body></html>")
    resp.headers["Content-Security-Policy"] = \
        "default-src 'none'; style-src 'unsafe-inline'"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@router.get("/api/kb/source")
async def kb_source(request: Request, kb: str, doc: str, kind: str = "line",
                    a: int = 0, b: int = 0, sheet: str = "", raw: int = 0):
    """知识库原文定位视图（只读）：出处卡片点击后按锚点打开对应格式。

    分流（spec 2.2 澄清 2 分层排版还原）：
    - kind=page 且 pdf → 原始 pdf 流（浏览器原生查看器 #page=N 跳页，真实排版）
    - kind=para 且 docx → raw=1 返回原始二进制（mammoth 前端还原语义排版）；
      否则返回渲染页（本地 vendor mammoth + 服务端文本片段作降级兜底）
    - kind=line/sheet/slide（md/txt/html/xlsx/pptx）→ 文本片段页
      （命中行 ±5 上下文、高亮标记）
    权限：认证开启时须登录，可见性跟随声明包的 allowed_roles + allowed_departments
    （两维 AND，PLAN 2.3；部门取请求身份——SSO 部门头优先，与请求级 ACL 同口径）；
    kb 未声明/文档名非法/文件不存在一律 404（不区分「不存在」与「无权」，
    不暴露库清单）。逻辑路径 kb/<库名>/<文档名>，错误信息不含服务器绝对路径。"""
    # 延迟 import：kb_allowed / _kb_home 是测试补丁锚点（monkeypatch 定点
    # server.main 命名空间），须走 main 模块属性
    from server import main as _main
    if auth.enabled():
        ident = _acl_identity(request)
        if not ident:
            return JSONResponse({"error": "未登录"}, status_code=401)
        role = ident.get("role", "")
        department = ident.get("department", "") or ""
    else:
        role = None  # 开放模式：与现有开放部署行为一致
        department = None
    if not _main.kb_allowed(kb, role, department):
        return JSONResponse({"error": "知识库不存在"}, status_code=404)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", kb or ""):
        return JSONResponse({"error": "知识库不存在"}, status_code=404)
    # 文档名防御：NFC 归一化后必须为纯文件名（穿越/控制字符一律 404）
    doc = unicodedata.normalize("NFC", doc or "")
    if (not doc or len(doc) > _KB_DOC_MAX or doc != Path(doc).name
            or "/" in doc or "\\" in doc or ".." in doc
            or any(ord(c) < 32 for c in doc)):
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    path = _main._kb_home() / kb / doc
    if not path.is_file():
        return JSONResponse({"error": "文档不存在"}, status_code=404)

    logical = f"kb/{kb}/{doc}"
    if b < a:
        b = a
    ext = path.suffix.lower()

    # ---- pdf：原始流（原生查看器跳页由前端链接 #page=N 附加）----
    if kind == "page" and ext == ".pdf":
        return FileResponse(str(path), media_type="application/pdf")

    # ---- docx：mammoth 渲染页（raw=1 给前端 fetch 原始二进制）----
    if kind == "para" and ext == ".docx":
        if raw:
            return FileResponse(str(path), filename=doc)
        try:
            text, _anchors = await asyncio.to_thread(kb_parsers.parse_document, path)
        except kb_parsers.KbParseError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        rows = [(ln, anc) for ln, anc in zip(text.splitlines(), _anchors)
                if ln.strip()]
        # v0.17.37 P0-2（spec 4.1）：文档文本/锚点值入页前一律转义——
        # 恶意 docx 的标签字面量只能以文本呈现，不得形成可执行节点
        fallback = "".join(
            f"<p class='hl' data-a='{_esc(str((anc or {}).get('a', '')))}'>"
            f"{_esc(ln)}</p>"
            if anc and a <= (anc.get("a") or 0) <= b else f"<p>{_esc(ln)}</p>"
            for ln, anc in rows)
        src_url = f"/api/kb/source?kb={quote(kb)}&doc={quote(doc)}&kind=para&a={a}&raw=1"
        # CSP nonce（spec 4.4）：本页唯一内联脚本的放行口，逐请求随机——
        # 任何注入的 <script>（无 nonce）都被浏览器 CSP 拦截
        nonce = secrets.token_hex(16)
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_esc(doc)}</title><style>"
            "body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;"
            "margin:24px;color:#1f2937;max-width:860px;line-height:1.75}"
            "h1{font-size:16px}.path{color:#6b7280;font-size:13px;margin-bottom:16px}"
            "table{border-collapse:collapse}td,th{border:1px solid #d1d5db;"
            "padding:4px 10px}.hl{background:#fef9c3}"
            "#fallback{display:none;border:1px dashed #d1d5db;padding:12px;"
            "border-radius:8px}"
            "</style></head><body>"
            f"<h1>知识库原文（Word 还原）</h1><div class='path'>{_esc(logical)}</div>"
            "<div id='content'>加载中……</div>"
            "<div id='fallback'><p>浏览器还原失败，显示提取文本：</p>"
            + fallback + "</div>"
            "<script src='/vendor/mammoth.browser.min.js'></script>"
            f"<script nonce='{nonce}'>(function(){{"
            "var mark=function(root){var lo=" + str(a) + ",hi=" + str(b) + ";"
            "var ps=root.querySelectorAll('p');for(var i=0;i<ps.length;i++){"
            "if(ps[i].getAttribute('data-a')&&+ps[i].getAttribute('data-a')>=lo"
            "&&+ps[i].getAttribute('data-a')<=hi){ps[i].classList.add('hl');"
            "if(!window.__scrolled){window.__scrolled=1;"
            "ps[i].scrollIntoView({block:'center'});}}}};"
            "var fb=function(){document.getElementById('content').style.display='none';"
            "document.getElementById('fallback').style.display='block';};"
            # v0.17.37 P0-2（spec 4.3）：Mammoth 输出不再直写 innerHTML——
            # 先 POST /api/kb/sanitize 服务端白名单消毒，再入页；任一环失败
            # 走 fallback（服务端已转义的提取文本，自带 .hl 高亮）
            "fetch('" + src_url + "').then(function(r){if(!r.ok)throw 0;"
            "return r.arrayBuffer()}).then(function(buf){"
            "return mammoth.convertToHtml({arrayBuffer:buf})})."
            "then(function(res){"
            "return fetch('/api/kb/sanitize',{method:'POST',"
            "headers:{'Content-Type':'application/json',"
            "'X-Requested-With':'XMLHttpRequest'},"
            "body:JSON.stringify({html:res.value})})"
            ".then(function(r){if(!r.ok)throw 0;return r.json()})"
            ".then(function(d){"
            "document.getElementById('content').innerHTML=d.html;"
            "mark(document);});})"
            ".catch(fb);})();</script>"
            "</body></html>")
        resp = HTMLResponse(page)
        # 严格 CSP（spec 4.4）：脚本只放行 self + 本页 nonce；Mammoth 内联图片
        # 需要 data:；frame-ancestors/XFO 双保险防被嵌入恶意页
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' 'nonce-" + nonce + "';"
            " style-src 'unsafe-inline'; img-src 'self' data:;"
            " connect-src 'self'; frame-ancestors 'self'")
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    # ---- 其余：文本片段页（md/txt 原文件行号；xlsx/pptx/html 解析器行锚点）----
    try:
        if kind == "line" and ext in (".md", ".txt"):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            rows = [(i + 1, ln, {"kind": "line", "a": i + 1})
                    for i, ln in enumerate(lines)]
        else:
            text, anchors = await asyncio.to_thread(kb_parsers.parse_document, path)
            rows = [(i + 1, ln, anchors[i])
                    for i, ln in enumerate(text.splitlines())]
    except kb_parsers.KbParseError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    hit: set[int] = set()       # 高亮行号（片段视图坐标系）
    shown: list[tuple[int, str]] = []
    if kind == "line":
        for n, ln, _anc in rows:
            if a - 5 <= n <= b + 5:
                shown.append((n, ln))
                if a <= n <= b:
                    hit.add(n)
    else:
        for n, ln, anc in rows:
            k = (anc or {}).get("kind")
            if k == "para" and a <= (anc.get("a") or 0) <= b + 5:
                if a - 5 <= (anc.get("a") or 0) <= b:
                    hit.add(n)
                shown.append((n, ln))
            elif k == "sheet" and (anc.get("sheet") or "") == sheet \
                    and a - 3 <= (anc.get("a") or 0) <= b + 3:
                if a <= (anc.get("a") or 0) <= b:
                    hit.add(n)
                shown.append((n, ln))
            elif k == "slide" and a <= (anc.get("a") or 0) <= b:
                hit.add(n)
                shown.append((n, ln))
    if not shown:
        return JSONResponse({"error": "锚点无法定位（文档可能已更新，请重建索引后重试）"},
                            status_code=404)
    body = "".join(
        f"<div class='kb-line{' hl' if n in hit else ''}' id='L{n}'>"
        f"<span class='ln'>{n}</span><span>{_esc(ln)}</span></div>"
        for n, ln in shown)
    return _source_page(logical, body)


def _esc(s: str) -> str:
    """HTML 转义（片段视图防注入：文档内容先转义再入页）。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))
