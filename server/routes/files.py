"""文档上传/下载/删除与技能包导入端点（v0.17.7）。自 server/main.py 原样搬迁。

# 纯文本格式：字节原样落盘（白名单收敛至 config.FILE_TEXT_EXTS，与模型输出
# save_output_file 共用）；.docx：标准库解包提取正文文本（零第三方依赖）。
# PDF 无可靠零依赖提取方案 → 明确拒绝并引导转换（fail-closed，不假装能读）。
"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from server import agent, audit, auth, config, skills
from server.webdeps import _acl_identity

logger = logging.getLogger(__name__)

router = APIRouter()

_UPLOAD_DOC_EXTS = {".docx"}
_UPLOAD_ALL_EXTS = config.FILE_TEXT_EXTS | _UPLOAD_DOC_EXTS
# Windows 非法文件名字符（含控制字符）与保留设备名——防 write_text 抛错后
# 异常文本携带服务器绝对路径外泄，同时避免保留名在 Windows 部署引发歧义
_WIN_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}

# docx 解包上限：word/document.xml 解压后字符数（防解压炸弹，10MB 压缩体
# 理论可膨胀出 GB 级 XML）。测试 monkeypatch 锚点即本模块
# （tests/test_deny_paths.py）
_DOCX_XML_MAX_CHARS = 20_000_000


def _docx_to_text(data: bytes) -> str:
    """从 .docx（zip 包）提取 word/document.xml 正文文本，去标签保留段落换行。
    解压前校验条目声明大小（防解压炸弹）；非 zip/缺条目抛 ValueError（→400）。
    批 4 收尾自 server/main.py 迁回本域（仅上传端点使用）。"""
    import html as _html
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > _DOCX_XML_MAX_CHARS:
                raise ValueError("docx 正文过大，超出解析上限")
            xml = z.read(info).decode("utf-8", errors="replace")[:_DOCX_XML_MAX_CHARS]
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("不是有效的 docx 文件（缺少 word/document.xml）")
    xml = re.sub(r"</w:p>", "\n", xml)          # 段落 → 换行
    xml = re.sub(r"<[^>]+>", "", xml)           # 去其余标签
    return _html.unescape(xml).strip()


def _upload_safe_name(name: str) -> str:
    """文件名清洗：只取 basename、限长、白名单后缀、拒绝 Windows 非法字符与
    保留设备名、Unicode NFC 归一化；返回空串表示不合法。"""
    import unicodedata
    name = unicodedata.normalize("NFC", (name or "").strip()).replace("\\", "/")
    name = name.rsplit("/", 1)[-1]              # 任何路径成分都剥掉
    if not name or len(name) > 120 or name in {".", ".."}:
        return ""
    if _WIN_BAD_NAME.search(name):
        return ""
    if Path(name).stem.lower() in _WIN_RESERVED:
        return ""
    suffix = Path(name).suffix.lower()
    return name if suffix in _UPLOAD_ALL_EXTS else ""


def _user_scope(request: Request) -> str:
    """当前请求的文件域：开放模式/无身份 → shared（兼容旧版单目录）；
    认证模式按用户名分域（uploads/outputs 多用户隔离，v0.17.7 评审修复；
    v0.17.32 SEC-2 改用 user_scope 哈希域名，彻底消除清洗碰撞）。"""
    if not auth.enabled():
        return "shared"
    ident = request.state.user
    return config.user_scope(ident["username"]) if ident else "shared"


def _global_file_view(request: Request) -> bool:
    """是否全局文件视图（列出/删除/下载任意域）：开放模式或 approver/admin。
    user 角色只能访问自己的域（与 sessions/usage 资源级 ACL 同口径）。"""
    if not auth.enabled():
        return True
    ident = request.state.user
    return ident is None or ident["role"] in ("approver", "admin")


def _safe_rel(rel: str) -> str:
    """相对路径清洗：允许「<域>/<文件名>」两段式（清单/删除/下载接口统一用
    相对名寻址），域段与文件段分别过清洗；含 .. 段或超过两段一律拒绝；
    返回空串表示不合法。"""
    rel = (rel or "").strip().replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    if len(parts) == 2:
        # 域段清洗走 config.scope_segment（评审修复 C2，v0.17.32）：
        # user_scope 域名最长 57 字符，沿用 scope_clean 的 48 截断会截掉
        # 基名 ≥40 字用户的哈希后缀 → 全局视图按 URL 寻址其域 404。
        scope, fname = config.scope_segment(parts[0]), _upload_safe_name(parts[1])
        return f"{scope}/{fname}" if scope and fname else ""
    if len(parts) == 1:
        return _upload_safe_name(parts[0])
    return ""


def _check_body_limit(request: Request) -> JSONResponse | None:
    """请求体大小预检：先看 Content-Length 头（无该头的 chunked 请求由调用方
    读完后再兜底校验），超限直接 413，避免先全量读进内存。"""
    cl = request.headers.get("content-length")
    try:
        if cl and int(cl) > config.UPLOAD_MAX_MB * 1024 * 1024:
            return JSONResponse({"error": f"文件超过大小上限（{config.UPLOAD_MAX_MB}MB）"},
                                status_code=413)
    except ValueError:
        return JSONResponse({"error": "非法 Content-Length"}, status_code=400)
    return None


def _scope_files(base: Path, scope: str | None) -> list[Path]:
    """列出文件域目录下的文件。scope=None 表示全局视图（遍历所有用户域）。"""
    if scope is not None:
        d = base / scope
        return [p for p in sorted(d.iterdir()) if p.is_file()] if d.is_dir() else []
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir():
                out += [p for p in sorted(d.iterdir()) if p.is_file()]
    return out


@router.post("/api/uploads")
async def upload_doc(request: Request, name: str = "") -> Any:
    """上传文档（raw body，免 python-multipart 依赖）：存 TOOL_WORKSPACE/uploads/
    <用户域>/（落在 read_text_file 工作区边界内，模型直接可读分析）。.docx 解包
    提取为文本落盘（<原名>.docx.md）。多用户按身份分域（开放模式 shared）。"""
    safe = _upload_safe_name(name)
    if not safe:
        return JSONResponse({"error": f"不支持的文件类型，允许后缀："
                             f"{', '.join(sorted(_UPLOAD_ALL_EXTS))}（PDF 请先转换为 docx/txt/md）"},
                            status_code=400)
    pre = _check_body_limit(request)
    if pre:
        return pre
    data = await request.body()
    if not data:
        return JSONResponse({"error": "请求体为空"}, status_code=400)
    if len(data) > config.UPLOAD_MAX_MB * 1024 * 1024:
        return JSONResponse({"error": f"文件超过大小上限（{config.UPLOAD_MAX_MB}MB）"},
                            status_code=413)
    try:
        if Path(safe).suffix.lower() in _UPLOAD_DOC_EXTS:
            try:
                text = _docx_to_text(data)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            if not text:
                return JSONResponse({"error": "docx 解析结果为空（可能是空文档或加密文档）"},
                                    status_code=400)
            safe = Path(safe).stem + ".docx.md"   # 提取出的文本以 .docx.md 落盘
        else:
            text = data.decode("utf-8", errors="replace")
        scope = _user_scope(request)
        scope_dir = config.UPLOADS_DIR / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        target = scope_dir / safe
        if not target.resolve().is_relative_to(config.UPLOADS_DIR.resolve()):
            return JSONResponse({"error": "非法文件名"}, status_code=400)
        target.write_text(text, encoding="utf-8")
        ident = _acl_identity(request)
        audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
                  "文件上传", target=safe)
        # 4.2 事件挂钩：落盘且转换均成功后触发（旁路自界隔离，不影响上传响应）
        from .. import schedulers
        await schedulers.dispatch_upload_event(
            rel_path=f"{scope}/{safe}",
            uploader=ident["username"] if ident else "",
            scope=scope, chars=len(text))
        return {"name": f"{scope}/{safe}", "chars": len(text), "preview": text[:200]}
    except OSError:
        # 不拼 {e}：OSError 文本含完整服务器绝对路径，外泄部署布局
        return JSONResponse({"error": "保存失败：文件名含非法字符或磁盘写入被拒绝"},
                            status_code=500)
    except Exception:
        return JSONResponse({"error": "保存失败（服务器内部错误）"}, status_code=500)


@router.get("/api/files/{name:path}")
async def download_output_file(request: Request, name: str) -> Any:
    """下载模型输出的文件（仅限 OUTPUTS_DIR 目录内，即 save_output_file 落盘处；
    不开放 uploads/ 与工作区其余路径——下载面只暴露模型主动产出的结果文件）。
    多用户按身份分域：user 角色只能访问自己域的文件；approver/admin/开放模式
    可带 <域>/ 前缀访问任意域；单段文件名按请求身份路由到本域。
    认证由全局中间件强制（/api/*）。附件下载语义（Content-Disposition: attachment）。"""
    rel = _safe_rel(name)
    if not rel:
        return JSONResponse({"error": "非法文件名"}, status_code=400)
    if "/" not in rel:
        # 单段名：按请求身份路由到本域（开放模式=shared；链接含域前缀时走显式域）。
        # 旧版兼容：分域前的历史会话里链接不带域名，产出已迁入 shared/——
        # 全局视图身份本域未命中时回退 shared（user 角色不回退，保持隔离）
        scoped = f"{_user_scope(request)}/{rel}"
        if (not (config.OUTPUTS_DIR / scoped).is_file()
                and _global_file_view(request)
                and (config.OUTPUTS_DIR / f"shared/{rel}").is_file()):
            scoped = f"shared/{rel}"
        rel = scoped
    elif not _global_file_view(request):
        rel = f"{_user_scope(request)}/{Path(rel).name}"   # user 角色强制本域
    target = config.OUTPUTS_DIR / rel
    if (not target.resolve().is_relative_to(config.OUTPUTS_DIR.resolve())
            or not target.is_file()):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    fname = Path(rel).name
    if fname.endswith(".docx"):
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fname.endswith(".docx.md"):
        media = "application/octet-stream"
    else:
        media = "text/plain; charset=utf-8"
    ident = _acl_identity(request)
    audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
              "文件下载", target=rel)
    return FileResponse(target, media_type=media, filename=fname)


@router.get("/api/uploads")
async def uploads_list(request: Request) -> dict:
    """已上传文档清单（相对名/大小/修改时间）。user 角色只列自己域，
    approver/admin/开放模式列全部域（相对名带 <域>/ 前缀）。"""
    scope = None if _global_file_view(request) else _user_scope(request)
    items = []
    for p in _scope_files(config.UPLOADS_DIR, scope):
        try:
            st = p.stat()
        except OSError:
            continue    # 清单遍历与删除竞态：跳过已消失条目
        rel = f"{p.parent.name}/{p.name}" if p.parent != config.UPLOADS_DIR else p.name
        items.append({"name": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"uploads": items}


@router.post("/api/skillpacks")
async def upload_skillpack(request: Request, overwrite: bool = False) -> Any:
    """上传技能包 zip：服务端安全解包直接挂载（多文件技能：SKILL.md +
    references/ 等）。存储按角色分流（v0.17.9）：user 角色导入本人专属域
    skills_personal/<域>/（仅本人可见），approver/admin/开放模式导入通用
    skills/（全员共享）。安全校验：条目路径防穿越（zip-slip）、条目数与
    解压后总量上限（防 zip 炸弹）、扩展名白名单；技能名取 SKILL.md
    frontmatter，与 create_skill 同一命名白名单与冲突语义；覆盖导入先解包
    临时目录校验后原子换名（失败不损坏原技能）。通用层导入成功后重建图使
    技能立即可发现；专属技能索引动态注入，无需重建图。"""
    pre = _check_body_limit(request)
    if pre:
        return pre
    body = await request.body()
    if not body:
        return JSONResponse({"error": "请求体为空"}, status_code=400)
    if len(body) > config.UPLOAD_MAX_MB * 1024 * 1024:
        return JSONResponse({"error": f"文件超过大小上限（{config.UPLOAD_MAX_MB}MB）"},
                            status_code=413)
    ident = _acl_identity(request)
    personal_scope = (config.user_scope(ident["username"])
                      if ident and ident.get("role") == "user" else None)
    try:
        result = await asyncio.to_thread(
            skills.import_skill_zip, body, overwrite, personal_scope)
    except FileExistsError as e:
        return JSONResponse({"error": str(e), "conflict": True}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "导入失败（服务器内部错误）"}, status_code=500)
    if not personal_scope:
        await asyncio.to_thread(agent.maybe_reload_custom_tools)  # 通用技能签名变化 → 重建图
    audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
              "技能包导入", target=result["name"])
    result["files"] = result["files"][:50]
    return result


@router.delete("/api/uploads/{name:path}")
async def upload_delete(request: Request, name: str) -> Any:
    """删除一个已上传文档（相对名寻址）。user 角色只能删除自己域的文档。"""
    rel = _safe_rel(name)
    if not rel:
        return JSONResponse({"error": "非法文件名"}, status_code=400)
    if "/" not in rel:
        rel = f"{_user_scope(request)}/{rel}"   # 单段名按请求身份路由到本域
    elif not _global_file_view(request):
        rel = f"{_user_scope(request)}/{Path(rel).name}"   # user 角色强制本域
    target = config.UPLOADS_DIR / rel
    if (not target.resolve().is_relative_to(config.UPLOADS_DIR.resolve())
            or not target.is_file()):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    target.unlink()
    ident = _acl_identity(request)
    audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
              "文件删除", target=Path(rel).name)
    return {"deleted": rel}
