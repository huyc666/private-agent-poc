"""内核内置工具：工作区文件读取与沙箱代码执行（运行时安全边界相关的最小集合）。

通用业务能力（时间/计算）已迁为平台包 packs/core-utils（v0.12.0 起）；
天气能力为领域包 packs/weather-ops（v0.8.0 起）。
内核只保留运行时管理与安全边界类工具，业务能力一律走「平台包/领域包」。

审批样板统一走 approval.require_approval（v0.13.0 起，原 7 处重复代码已收敛）。
"""
import json
import re
import urllib.request
from pathlib import Path

from . import approval, auth, config, usage

# 敏感文件判定（v0.17.4 安全审计 F1）：.env 及变体（含部署密钥）与运行时
# SQLite 库（state.db/usage.db 及 WAL/SHM）。默认工作区=项目根目录时，
# 根目录的 .env 会落在工作区内——若不做拦截，对话可直接把它读出外泄。
_SENSITIVE_FILENAME_RE = re.compile(
    r"^(\.env($|\..*)|.*\.db($|-wal$|-shm$))$", re.IGNORECASE)
# 行级密钥掩码：识别 "KEY = value" / "KEY: value"（键名可带引号，兼容 JSON）
_KEYVALUE_RE = re.compile(r'^["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?(\s*[=:]\s*)(.+)$')
_SECRET_KEY_RE = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
# 模型产出/上传文件名的 Windows 非法字符与保留设备名（与 main.py 上传端点同规则，
# v0.17.7 评审 P1-3：write 抛错后异常文本携带服务器绝对路径 → 一律前置拒绝）
_WIN_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def _output_safe_name(name: str) -> bool:
    """save_output_file 的文件名合法性校验（在 basename/白名单后缀之上，
    增加 Windows 非法字符与保留设备名检查）。合法返回 True。"""
    if _WIN_BAD_NAME.search(name):
        return False
    return Path(name).stem.lower() not in _WIN_RESERVED


def _file_scope() -> str:
    """当前工具调用的文件域（多用户隔离，v0.17.7 评审）：认证模式按
    usage.current_user（stream_reply 注入的对话归属身份）分域；开放模式
    及身份缺失时 shared（与端点侧 _user_scope 同口径，规则在 config.scope_clean）。"""
    u = usage.current_user.get() or ""
    if not auth.enabled() or not u or u == "anonymous":
        return "shared"
    return config.scope_clean(u)


def _is_sensitive_file(target) -> bool:
    """敏感文件判定：返回 True 则拒绝读取。"""
    return bool(_SENSITIVE_FILENAME_RE.match(target.name))


def _cross_scope_denied(rel_parts: tuple) -> str | None:
    """user 角色越域访问他人私有目录时返回资源类别（用于拒绝文案），放行返回
    None（v0.17.9）。覆盖三类按身份分域的目录：skills_personal/<域>/（专属技能，
    本次新增）、uploads/<域>/ 与 outputs/<域>/（v0.17.8 分域此前只在清单/端点层
    生效，直拼路径可读他人文档——同一提示词注入/路径猜测面，一并收口）。
    approver/admin/开放模式（current_role != "user"）全局可见，一律放行。"""
    if not rel_parts or rel_parts[0] not in ("skills_personal", "uploads", "outputs"):
        return None
    if auth.current_role() != "user":
        return None
    if len(rel_parts) < 2 or rel_parts[1] == _file_scope():
        return None
    return "专属技能" if rel_parts[0] == "skills_personal" else "上传/产出文件"


def _redact_secrets(text: str) -> str:
    """按行掩码密钥值：键名含 token/key/secret/password/credential 等敏感词的
    键值行，值统一替换为占位符（工具代码/配置文件里的硬编码密钥不外泄到对话）。"""
    out = []
    for line in text.splitlines():
        m = _KEYVALUE_RE.match(line)
        if m and _SECRET_KEY_RE.search(m.group(1)):
            out.append(f"{m.group(1)}{m.group(2)}***（已脱敏）")
        else:
            out.append(line)
    return "\n".join(out)


def read_text_file(path: str) -> str:
    """读取工作区内的文本文件内容（最大 20KB）。path 相对项目根目录。
    v0.17.4：敏感文件（.env 及运行时 DB）拒绝读取；其余内容按行掩码密钥值。
    v0.17.9：user 角色不可读取他人专属技能与他人域的上传/产出文件。"""
    try:
        target = (config.TOOL_WORKSPACE / path).resolve()
        if not target.is_relative_to(config.TOOL_WORKSPACE):
            return "拒绝访问：路径越出工作区边界"
        denied = _cross_scope_denied(target.relative_to(config.TOOL_WORKSPACE).parts)
        if denied:
            return f"拒绝访问：他人{denied}不可读取"
        if _is_sensitive_file(target):
            return f"拒绝访问：{target.name} 为敏感文件（部署密钥/运行时数据），不予读取"
        if not target.is_file():
            return f"文件不存在: {path}"
        return _redact_secrets(
            target.read_text(encoding="utf-8", errors="replace"))[:20_000]
    except Exception as e:
        return f"读取失败: {e}"


def list_uploaded_docs() -> str:
    """列出当前用户域的上传文档（uploads/<域>/）：逻辑路径与大小。
    多用户隔离（v0.17.7 评审）：认证模式只列本域，跨域文档对模型不可见。
    配合 read_text_file 读取 uploads/<域>/<文件名> 获取内容进行分析。"""
    scope = _file_scope()
    d = config.UPLOADS_DIR / scope
    if not d.is_dir():
        return "（暂无上传文档；用户可通过前端 📎 按钮上传）"
    lines = []
    for p in sorted(d.iterdir()):
        try:
            if p.is_file():
                lines.append(f"- uploads/{scope}/{p.name}（{p.stat().st_size:,} 字节）")
        except OSError:
            continue    # 清单遍历与删除竞态：跳过已消失条目
    return ("已上传文档：\n" + "\n".join(lines)) if lines else "（uploads/ 目录为空）"


def _mdtext_to_docx(content: str) -> bytes:
    """把文本（Markdown 命令风格）转换为公文排版的 .docx（零第三方依赖）。
    .docx 本质是 zip + OOXML：手写最小包（[Content_Types].xml/_rels/word/document.xml）。
    公文版式（GB/T 9704 风格）：标题「# 」居中二号方正小标宋（回退宋体）；
    「一、」层级黑体三号；「（一）」层级楷体三号；正文仿宋三号；正文均首行缩进 2 字符。
    v0.17.7 评审 P2：先剥离 XML 1.0 非法控制字符（\x00-\x08/\x0b/\x0c/\x0e-\x1f），
    否则模型带入的不可见控制字符会让 Word 打开报「内容有问题」。"""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)

    def _run(text: str, east: str, half_pts: int, bold: bool = False) -> str:
        rpr = (f'<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"'
               f' w:eastAsia="{east}"/>'
               + ("<w:b/>" if bold else "")
               + f'<w:sz w:val="{half_pts}"/><w:szCs w:val="{half_pts}"/></w:rPr>')
        return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'

    def _para(text: str, east: str, half_pts: int, center: bool = False,
              indent: bool = True, bold: bool = False) -> str:
        ppr = "<w:pPr>"
        ppr += '<w:spacing w:line="560" w:lineRule="exact"/>'   # 固定行距 28 磅
        if center:
            ppr += '<w:jc w:val="center"/>'
        elif indent:
            ppr += '<w:ind w:firstLineChars="200" w:firstLine="640"/>'
        ppr += "</w:pPr>"
        return f"<w:p>{ppr}{_run(text, east, half_pts, bold)}</w:p>"

    paras = []
    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):       # 标题：居中二号
            paras.append(_para(line[2:].strip(), "方正小标宋简体", 44, center=True, indent=False))
        elif line.startswith("## "):    # 次级标题：居中三号宋体加粗
            paras.append(_para(line[3:].strip(), "宋体", 32, center=True, indent=False, bold=True))
        elif re.match(r"^[一二三四五六七八九十]+、", line.strip()):
            paras.append(_para(line.strip(), "黑体", 32))
        elif re.match(r"^（[一二三四五六七八九十]+）", line.strip()):
            paras.append(_para(line.strip(), "楷体", 32))
        else:
            paras.append(_para(line.strip(), "仿宋", 32))

    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>" + "".join(paras) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
                "</w:body></w:document>")
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType='
                     '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def save_output_file(name: str, content: str, as_docx: bool = False) -> str:
    """把整理/生成的结果保存为可下载文件（outputs/<用户域>/ 目录），返回下载链接。
    v0.17.7：文件名限 basename + 白名单后缀 + 120 字限长（与上传同规则，
    .env/*.db 等敏感名天然被白名单挡下）；内容限 UPLOAD_MAX_MB。
    as_docx=true 时把文本转换为公文排版 Word 文档（.docx，名称须以 .docx 结尾）。
    保存后在最终回复中附上返回的下载链接告知用户。
    v0.17.7 评审：多用户按身份分域落盘（认证模式 outputs/<域>/，开放模式 shared/），
    user 角色只能下载自己域的产出；下载链接统一带域名，服务端按请求身份路由。"""
    name = (name or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]              # 任何路径成分都剥掉
    if not name or len(name) > 120 or name in {".", ".."}:
        return "保存失败：文件名不合法（只允许 basename，≤120 字符）"
    if not _output_safe_name(name):
        return "保存失败：文件名含非法字符或是系统保留设备名"
    suffix = Path(name).suffix.lower()
    if as_docx:
        if suffix != ".docx":
            return "保存失败：as_docx=true 时文件名必须以 .docx 结尾"
    elif suffix not in config.FILE_TEXT_EXTS:
        return ("保存失败：不支持的文件类型（白名单："
                + ", ".join(sorted(config.FILE_TEXT_EXTS))
                + "；Word 文档请传 as_docx=true 并以 .docx 结尾命名）")
    if len(content.encode("utf-8")) > config.UPLOAD_MAX_MB * 1024 * 1024:
        return f"保存失败：内容超过大小上限（{config.UPLOAD_MAX_MB}MB）"
    try:
        scope = _file_scope()
        out = config.OUTPUTS_DIR / scope
        out.mkdir(parents=True, exist_ok=True)
        if as_docx:
            (out / name).write_bytes(_mdtext_to_docx(content))
        else:
            (out / name).write_text(content, encoding="utf-8")
        kind = "Word 文档" if as_docx else "文本文件"
        return (f"已保存 {kind} {name}（{len(content):,} 字符）。"
                f"下载链接：/api/files/{scope}/{name}")
    except OSError:
        # 不拼 {e}：OSError 文本含完整服务器绝对路径，外泄部署布局（P1-3）
        return "保存失败：文件名含非法字符或磁盘写入被拒绝"
    except Exception:
        return "保存失败（服务器内部错误）"


def run_python_code(code: str) -> str:
    """在隔离沙箱中运行 Python 代码并返回 stdout/stderr。仅在部署 Docker 沙箱服务并配置 SANDBOX_URL 后可用。"""
    if not config.SANDBOX_URL:
        return ("代码执行未启用：需先部署 Docker 沙箱服务（docker-compose.sandbox.yml）"
                "并配置 SANDBOX_URL。本框架不允许在未隔离环境中执行代码（fail-closed）。")
    try:
        headers = {"Content-Type": "application/json"}
        if config.SANDBOX_AUTH_TOKEN:
            headers["X-Sandbox-Token"] = config.SANDBOX_AUTH_TOKEN
        req = urllib.request.Request(
            config.SANDBOX_URL.rstrip("/") + "/run",
            data=json.dumps({"code": code}).encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(req, timeout=config.SANDBOX_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("output") or data.get("error") or "（无输出）"
    except Exception as e:
        return f"沙箱调用失败: {e}"


def do_delete(path: str) -> str:
    """删除的实际执行逻辑（审批通过后才会走到这里；Mock 演示剧本也复用）。
    v0.17.5 架构评审 P2：与 read_text_file 对称的敏感文件拦截（F1）——
    .env 承载部署密钥、state.db 承载会话/审批/租约锁，误删破坏并发控制与
    部署配置；删除此类文件属运维操作，一律不走对话路径（即使批准也拒绝）。
    v0.17.9：user 角色不可删除他人专属技能与他人域的上传/产出文件（与读取同规则）。"""
    try:
        target = (config.TOOL_WORKSPACE / path).resolve()
        if not target.is_relative_to(config.TOOL_WORKSPACE):
            return "拒绝访问：路径越出工作区边界"
        denied = _cross_scope_denied(target.relative_to(config.TOOL_WORKSPACE).parts)
        if denied:
            return f"拒绝删除：他人{denied}不可操作"
        if _is_sensitive_file(target):
            return f"拒绝删除：{target.name} 为敏感文件（部署密钥/运行时状态库），请由管理员在部署层操作"
        if not target.is_file():
            return f"文件不存在: {path}"
        target.unlink()
        return f"已删除文件: {path}"
    except Exception as e:
        return f"删除失败: {e}"


async def delete_workspace_file(path: str) -> str:
    """删除工作区内的指定文件（危险操作，执行前需用户批准）。path 相对项目根目录。"""
    return approval.require_approval(
        "delete_workspace_file", {"path": path}, "删除",
        lambda: do_delete(path))
