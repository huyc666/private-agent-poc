"""FastAPI 入口：静态页面 + SSE 流式聊天接口 + 健康检查 + 会话/审批记录查询。

启动：python server/main.py [--host 0.0.0.0] [--port 7100]

无状态化说明（P2，v0.13.0 起）：
- 对话记录存 SQLite（sessions.py / STATE_DB），重启不丢，不再是进程内存 dict；
- 审批记录同样落库（approval.py），跨进程可决议；
- 模型上下文由 LangGraph checkpointer 持有（memory/postgres），
  因此真实模式下每次请求只把「新用户消息」交给图——历史由 checkpointer 接续，
  避免了旧实现「全量历史 + checkpointer 追加」导致的上下文重复。
"""
import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from server import (agent, approval, auth, config, custom_tools, packs,
                    session_lock, sessions, skills, telemetry, usage)


def _bearer_token(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else ""


APPROVAL_SCAN_INTERVAL = 300.0  # 周期扫描间隔（秒）：已批准但未执行的审批 → abandoned


async def _approval_scan_loop():
    """后台周期任务：把「已批准但从未执行」的审批标记为 abandoned。
    等待协程可能在进程存活时死亡（如客户端断开被取消），仅靠启动扫描
    捕捉不到，需周期兜底（v0.17.2）。单条 UPDATE，开销可忽略。
    v0.17.3：循环体异常容错——单次扫描失败只记日志继续，后台任务不会
    静默死亡（否则兜底永久失效且无人知晓）。
    架构评审 P1 修复：宽限期——刚批准正在 resume 执行的记录处于「approved 且未
    标记 executed」的正常中间态，扫描跳过决议时间距今不足 grace 的记录，
    避免误标正在执行的长任务（grace 覆盖审批等待上限 + 扫描间隔）。"""
    grace = approval.APPROVAL_TIMEOUT + APPROVAL_SCAN_INTERVAL
    while True:
        await asyncio.sleep(APPROVAL_SCAN_INTERVAL)
        try:
            n = await asyncio.to_thread(approval.mark_abandoned, grace)
            if n:
                print(f"[approval] 周期扫描：{n} 条「已批准但执行丢失」标记为 abandoned")
        except Exception as e:
            # 单次失败不终止任务：DB 短暂锁竞争/瞬时错误恢复后下一轮自动重试
            print(f"[approval] 周期扫描失败（下轮重试）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动清理带宽限期：同机多进程共享 STATE_DB 时，其他进程可能有在途
    # resume（approved 尚未 mark_executed），刚决议的记录不判为丢失
    cleaned = approval.cleanup_stale(
        grace=approval.APPROVAL_TIMEOUT + APPROVAL_SCAN_INTERVAL)
    if cleaned:
        print(f"[approval] 启动清理：{cleaned} 条残留审批已处理"
              "（pending→expired，approved 未执行→abandoned）")
    auth.bootstrap_admin()  # 认证模式首启：创建引导 admin 并打印一次性 Key
    # 多用户文件分域（v0.17.7 评审）：旧版单目录 uploads/outputs 的存量文件
    # 迁入 shared/ 域，保证升级后旧文件仍可按 shared 寻址（不迁则分域后找不到）
    await asyncio.to_thread(_migrate_legacy_file_dirs)
    # v0.17.4（安全审计 F2）：SSO 身份头必须配合受信代理白名单才生效；
    # 配置了身份头却没配白名单 = 身份头永远不生效（fail-closed），启动时明确告警
    if auth.enabled() and config.AUTH_IDENTITY_HEADER and not config.AUTH_PROXY_ALLOWED_IPS:
        print("[auth] WARNING: 已配置 AUTH_IDENTITY_HEADER 但未配置"
              " AUTH_PROXY_ALLOWED_IPS——身份头将被忽略（fail-closed，防伪造）。"
              "请在 .env 配置受信代理来源 IP 白名单后重启，SSO 身份头才会生效。")
    await agent.build_agent()
    scan_task = asyncio.create_task(_approval_scan_loop())
    yield
    scan_task.cancel()  # 周期扫描随服务退出结束
    telemetry.flush()  # 关闭前冲刷 Langfuse 缓冲的观测数据
    await agent.close_checkpointer()  # 关闭 PostgresSaver 连接（若启用）


app = FastAPI(title="Private Agent POC", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """认证中间件：开放模式放行（user=None，各端点按旧行为处理）；
    认证模式下 /api/*（除 /api/health 与 /api/auth/login）必须持有效身份——
    反向代理身份头（SSO 钩子）或 Bearer 凭据（pak- Key / pat- 令牌），
    身份解析后写入 request.state.user，成为 user_id 的唯一可信来源。"""
    request.state.user = None
    if not auth.enabled():
        return await call_next(request)
    token = _bearer_token(request)
    # SSO 钩子：身份解析唯一入口（身份头模式优先，其次 Bearer 凭据）。
    # v0.17.4（安全审计 F2）：身份头必须来自受信代理白名单才生效——
    # 未命中白名单/未配置白名单一律忽略（fail-closed），直连端口伪造头无效。
    header_user = ""
    if config.AUTH_IDENTITY_HEADER:
        if auth.proxy_ip_allowed(request.client.host if request.client else None):
            header_user = request.headers.get(config.AUTH_IDENTITY_HEADER, "")
        elif request.headers.get(config.AUTH_IDENTITY_HEADER):
            print("[auth] 身份头来自非受信来源（不在 AUTH_PROXY_ALLOWED_IPS 白名单），"
                  "已忽略（防伪造冒充）")
    # 解析涉及 SQLite 同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
    request.state.user = await asyncio.to_thread(
        auth.resolve_identity, token, header_user)
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/health", "/api/auth/login") \
            and request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest):
    """账号密码登录（v0.15.0）：签发 pat- 短期令牌。
    成功失败都走同一路径，不区分「用户不存在」与「密码错误」（防枚举）。"""
    if not auth.enabled():
        return JSONResponse({"error": "当前为开放模式，无需登录"}, status_code=400)
    result = auth.login(req.username, req.password)
    if result is None:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    return result


@app.post("/api/auth/logout")
async def logout_endpoint(request: Request):
    """注销：使当前 pat- 登录令牌立即失效（pak- Key 请用吊销/轮换）。"""
    token = _bearer_token(request)
    auth.logout(token)
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "anonymous"   # 用户标识（token 计量维度；API 认证落地后由网关/中间件注入）


@app.get("/")
async def index():
    return FileResponse(config.PROJECT_ROOT / "web" / "index.html")


@app.get("/api/health")
async def health(request: Request):
    # 认证模式未携带有效 Key：只回最小信息（不泄露模型/工具/包等内部状态）
    if auth.enabled() and request.state.user is None:
        return {"status": "ok", "auth_enabled": True}
    pack_list, pending = await asyncio.gather(
        asyncio.to_thread(packs.list_packs),
        asyncio.to_thread(approval.pending_count),
    )
    info = {
        "status": "ok",
        "auth_enabled": auth.enabled(),
        "mode": "mock" if config.MOCK_LLM else "live",
        "model": config.MODEL_NAME,
        "llm_base_url": config.LLM_BASE_URL,
        "tools": agent.STATE.tool_names,
        "mcp_enabled": config.MCP_ENABLED,
        "custom_tools_exec": custom_tools.exec_mode(),
        "packs": pack_list,
        "langfuse_enabled": telemetry.enabled(),
        "langfuse_connected": telemetry.get_client() is not None,
        "pending_approvals": pending,
    }
    if request.state.user is not None:
        info["identity"] = request.state.user  # 前端徽章显示服务端确认的身份
    return info


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    session_id = req.session_id or uuid.uuid4().hex[:8]
    run_id = uuid.uuid4().hex[:12]  # 任务维度：一次请求的完整运行
    # 认证模式：user_id 只信中间件解析的身份（请求体 user_id 字段忽略，兼容旧客户端）；
    # 开放模式：沿用请求体自报（演示用）
    if auth.enabled():
        user_id = request.state.user["username"]
    else:
        user_id = (req.user_id or "anonymous").strip()[:64] or "anonymous"

    # 对话记录落库在下方锁内进行（顺序与执行顺序一致）；模型上下文由
    # checkpointer 接续，所以只把本轮新消息交给图，历史不再重复传入。
    new_message = [{"role": "user", "content": req.message}]

    collected: list[str] = []
    finished = False

    async def event_stream():
        nonlocal finished
        owner = f"{run_id}@{os.getpid()}"
        # 同会话串行化（v0.16.4 + v0.17.1 加固）：
        # 1) 进程内 asyncio.Lock（agent.session_lock）承担同进程快速互斥；
        # 2) STATE_DB 分布式租约（SessionLease）承担跨进程互斥——
        #    多副本指向同一 STATE_DB 时，同一 session 的并发请求在此排队，
        #    避免 checkpointer 状态写冲突与对话记录交错；不同会话互不干扰。
        async with agent.session_lock(session_id):
            lease = session_lock.SessionLease(session_id, owner)
            try:
                await lease.__aenter__()
            except session_lock.LeaseTimeout as e:
                # 租约等待超时（架构评审 P3）：此前无界等待——同会话另一请求
                # 长时间持有租约（如审批 resume 挂起）时新请求无限挂起且用户
                # 无任何反馈。现在返回可理解的错误事件，用户稍后重试即可。
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
                return
            try:
                # SQLite 写为同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
                await asyncio.to_thread(sessions.append, session_id, user_id,
                                        "user", req.message)
                try:
                    yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'run_id': run_id}, ensure_ascii=False)}\n\n"
                    async for ev in agent.stream_reply(new_message, session_id=session_id,
                                                       user_id=user_id, run_id=run_id):
                        if lease.lost:
                            # 租约被其他进程接管（心跳续租失败）：继续推进
                            # 等于两进程同时写同一会话，互斥已失效——立即中止
                            # 本轮并让已产出部分按「不完整」落库（架构评审 P2）
                            yield f"data: {json.dumps({'type': 'error', 'content': '会话租约已被其他进程接管，本轮回复已中止，请重新发送消息'}, ensure_ascii=False)}\n\n"
                            break
                        if ev["type"] == "token":
                            collected.append(ev["content"])
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    else:
                        finished = True
                finally:
                    # 客户端中途断连时生成器被 aclose()：已产出的部分回复也要落库，
                    # 保证对话记录成对（user 消息已先于流写入），并标注不完整供审计识别
                    if collected:
                        text = "".join(collected)
                        if not finished:
                            text += ("\n\n（会话租约被其他进程接管，回复中止）"
                                     if lease.lost else "\n\n（连接中断，回复不完整）")
                        await asyncio.to_thread(
                            sessions.append, session_id, user_id, "assistant", text)
            finally:
                await lease.__aexit__()  # 停心跳 + 释放租约（与 __aenter__ 配对）

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _acl_identity(request: Request) -> dict | None:
    """资源级 ACL（v0.14.1）：返回当前身份 {username, role}；
    开放模式返回 None（不过滤，行为同旧版）。
    使用规则：role == 'user' 只能访问自己的资源；approver/admin 全局视图。"""
    if not auth.enabled():
        return None
    return request.state.user


def _can_access_session(identity: dict, session_id: str) -> bool:
    """user 角色只能访问全部消息都属于自己（或开放模式遗留 anonymous）的会话。"""
    if identity["role"] in ("admin", "approver"):
        return True
    owners = sessions.session_users(session_id)
    return bool(owners) and owners <= {identity["username"], "anonymous"}


@app.get("/api/sessions")
async def session_list(request: Request, limit: int = 50):
    """会话列表（对话记录来自 SQLite，重启后仍在）。
    user 角色只列自己的会话；approver/admin 列全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    return {"sessions": sessions.list_sessions(limit=min(limit, 200), user_id=uid)}


@app.get("/api/sessions/{session_id}/history")
async def session_history(request: Request, session_id: str, limit: int = 0):
    """一个会话的最近 N 条消息（默认 SESSION_MAX_HISTORY）。user 角色限本人会话。"""
    ident = _acl_identity(request)
    if ident and not _can_access_session(ident, session_id):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"session_id": session_id, "messages": sessions.history(session_id, limit)}


@app.delete("/api/sessions/{session_id}")
async def session_delete(request: Request, session_id: str):
    """删除一个会话的对话记录（隐私/治理）。user 角色限本人会话。"""
    ident = _acl_identity(request)
    if ident and not _can_access_session(ident, session_id):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"deleted": sessions.delete_session(session_id)}


# ---------------------------------------------------------------- 文档上传（v0.17.7）

# 纯文本格式：字节原样落盘（白名单收敛至 config.FILE_TEXT_EXTS，与模型输出
# save_output_file 共用）；.docx：标准库解包提取正文文本（零第三方依赖）。
# PDF 无可靠零依赖提取方案 → 明确拒绝并引导转换（fail-closed，不假装能读）。
_UPLOAD_DOC_EXTS = {".docx"}
_UPLOAD_ALL_EXTS = config.FILE_TEXT_EXTS | _UPLOAD_DOC_EXTS
# Windows 非法文件名字符（含控制字符）与保留设备名——防 write_text 抛错后
# 异常文本携带服务器绝对路径外泄，同时避免保留名在 Windows 部署引发歧义
_WIN_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
# docx 解包上限：word/document.xml 解压后字符数（防解压炸弹，10MB 压缩体
# 理论可膨胀出 GB 级 XML）
_DOCX_XML_MAX_CHARS = 20_000_000


def _docx_to_text(data: bytes) -> str:
    """从 .docx（zip 包）提取 word/document.xml 正文文本，去标签保留段落换行。
    解压前校验条目声明大小（防解压炸弹）；非 zip/缺条目抛 ValueError（→400）。"""
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


def _scope_clean(username: str) -> str:
    """身份 → 文件域目录名。清洗规则收敛在 config.scope_clean（v0.17.7 评审：
    工具侧 tools_builtin 落盘与端点侧寻址必须同规则，避免两处实现漂移）。"""
    return config.scope_clean(username)


def _user_scope(request: Request) -> str:
    """当前请求的文件域：开放模式/无身份 → shared（兼容旧版单目录）；
    认证模式按用户名分域（uploads/outputs 多用户隔离，v0.17.7 评审修复）。"""
    if not auth.enabled():
        return "shared"
    ident = request.state.user
    return _scope_clean(ident["username"]) if ident else "shared"


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
        scope, fname = _scope_clean(parts[0]), _upload_safe_name(parts[1])
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


def _migrate_legacy_file_dirs() -> None:
    """旧版单目录文件迁入 shared/ 域（多用户隔离上线时一次性执行）。
    重试 3 次：Windows 上「删除后立即重建同名路径」或杀软扫描新文件会出现
    瞬时 sharing violation——静默跳过会导致旧文件失联，必须重试并告警。"""
    import time
    for base in (config.UPLOADS_DIR, config.OUTPUTS_DIR):
        if not base.is_dir():
            continue
        for p in list(base.iterdir()):
            if not p.is_file():
                continue
            dest = base / "shared" / p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(3):
                try:
                    p.rename(dest)
                    break
                except OSError as e:
                    if attempt == 2:
                        print(f"[startup] WARNING: 存量文件迁移失败（旧版文件将"
                              f"无法按新分域寻址，请手动移动）: {p.name}: {e}")
                    else:
                        time.sleep(0.05)


@app.post("/api/uploads")
async def upload_doc(request: Request, name: str = ""):
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
        return {"name": f"{scope}/{safe}", "chars": len(text), "preview": text[:200]}
    except OSError:
        # 不拼 {e}：OSError 文本含完整服务器绝对路径，外泄部署布局
        return JSONResponse({"error": "保存失败：文件名含非法字符或磁盘写入被拒绝"},
                            status_code=500)
    except Exception:
        return JSONResponse({"error": "保存失败（服务器内部错误）"}, status_code=500)


@app.get("/api/files/{name:path}")
async def download_output_file(request: Request, name: str):
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
    return FileResponse(target, media_type=media, filename=fname)


@app.get("/api/uploads")
async def uploads_list(request: Request):
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


@app.post("/api/skillpacks")
async def upload_skillpack(request: Request, overwrite: bool = False):
    """上传技能包 zip：服务端安全解包直接挂载到 skills/<name>/（多文件技能：
    SKILL.md + references/ 等）。技能是全局资产（不按用户分域）。安全校验：
    条目路径防穿越（zip-slip）、条目数与解压后总量上限（防 zip 炸弹）、扩展名
    白名单；技能名取 SKILL.md frontmatter，与 create_skill 同一命名白名单与
    冲突语义；覆盖导入先解包临时目录校验后原子换名（失败不损坏原技能）。
    导入成功后重建图使技能立即可发现。"""
    pre = _check_body_limit(request)
    if pre:
        return pre
    body = await request.body()
    if not body:
        return JSONResponse({"error": "请求体为空"}, status_code=400)
    if len(body) > config.UPLOAD_MAX_MB * 1024 * 1024:
        return JSONResponse({"error": f"文件超过大小上限（{config.UPLOAD_MAX_MB}MB）"},
                            status_code=413)
    try:
        result = await asyncio.to_thread(skills.import_skill_zip, body, overwrite)
    except FileExistsError as e:
        return JSONResponse({"error": str(e), "conflict": True}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "导入失败（服务器内部错误）"}, status_code=500)
    await asyncio.to_thread(agent.maybe_reload_custom_tools)  # 技能签名变化 → 重建图，立即可发现
    result["files"] = result["files"][:50]
    return result


@app.delete("/api/uploads/{name:path}")
async def upload_delete(request: Request, name: str):
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
    return {"deleted": rel}


@app.get("/api/approvals")
async def approval_list(request: Request, limit: int = 50):
    """最近审批记录（审计）：工具、参数摘要、状态（pending/approved/rejected/
    expired/abandoned，abandoned = 已批准但执行丢失，见 approval.mark_abandoned）。
    user 角色只看自己创建的；approver/admin 看全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    return {"approvals": approval.list_recent(limit=min(limit, 200), user_id=uid)}


@app.get("/api/usage")
async def usage_stats(request: Request, user_id: str = "", session_id: str = "", days: int = 0):
    """Token 消耗聚合：总量 + 按用户 / 按会话 / 按任务（run）分组。
    days>0 时只统计最近 N 天。user 角色强制只统计本人（查询参数不可越权）。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        user_id = ident["username"]
    return usage.summary(user_id=user_id, session_id=session_id, days=days)


class ApproveRequest(BaseModel):
    approval_id: str
    approve: bool


@app.post("/api/approve")
async def approve(req: ApproveRequest, request: Request):
    """前端提交危险操作审批决定。
    决议权限：admin/approver 可决议任何 pending；普通 user 只能决议自己创建的。
    ok=false 时 error 区分 forbidden（无权限）与 not_found（不存在/已决议）。"""
    u = request.state.user or {"username": "", "role": "admin"}  # 开放模式：任何人可决议（旧行为）
    result = approval.resolve(req.approval_id, req.approve, u["username"], u["role"])
    if result == "ok":
        return {"ok": True}
    if result == "forbidden":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": False, "error": "not_found_or_decided"}


def _parse_host_port():
    host, port = "0.0.0.0", 7100
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
    return host, port


if __name__ == "__main__":
    import uvicorn

    host, port = _parse_host_port()
    print(f"Private Agent POC 启动于 http://localhost:{port}  (模式: {'mock' if config.MOCK_LLM else 'live'})")
    uvicorn.run(app, host=host, port=port)
