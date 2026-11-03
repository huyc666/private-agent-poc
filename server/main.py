"""FastAPI 入口：应用装配（lifespan/中间件）+ 静态页面 + 健康检查；
业务端点按资源域拆在 server/routes/（编码规范批 4），本文件末尾 register(app) 挂载。

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
import logging
import os
import re
import secrets
import sys
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from server import (agent, approval, audit, auth, config, custom_tools,
                    html_sanitize, packs, session_lock, sessions, telemetry,
                    usage)
from server.webdeps import (_bearer_token, _acl_identity, LoginRequest,
                            ChatRequest, KbSanitizeRequest, ApproveRequest)
from tools import kb_parsers  # 定位端点按锚点重解析文档（服务器侧，不进包）

logger = logging.getLogger(__name__)


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
                logger.info(f"[approval] 周期扫描：{n} 条「已批准但执行丢失」标记为 abandoned")
        except Exception as e:
            # 单次失败不终止任务：DB 短暂锁竞争/瞬时错误恢复后下一轮自动重试
            logger.warning(f"[approval] 周期扫描失败（下轮重试）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动清理带宽限期：同机多进程共享 STATE_DB 时，其他进程可能有在途
    # resume（approved 尚未 mark_executed），刚决议的记录不判为丢失
    cleaned = approval.cleanup_stale(
        grace=approval.APPROVAL_TIMEOUT + APPROVAL_SCAN_INTERVAL)
    if cleaned:
        logger.info(f"[approval] 启动清理：{cleaned} 条残留审批已处理"
                    "（pending→expired，approved 未执行→abandoned）")
    auth.bootstrap_admin()  # 认证模式首启：创建引导 admin 并打印一次性 Key
    audit.init()  # PLAN 6.3：审计表建表 + 存量容量裁剪（失败不阻断启动）
    packs.load_disabled_packs()  # PLAN 3.2 Q2：恢复禁用名单（须在 Agent 装配前）
    # 多用户文件分域（v0.17.7 评审）：旧版单目录 uploads/outputs 的存量文件
    # 迁入 shared/ 域，保证升级后旧文件仍可按 shared 寻址（不迁则分域后找不到）
    await asyncio.to_thread(_migrate_legacy_file_dirs)
    # SEC-2（v0.17.32）：文件域改用 user_scope 哈希域名（防清洗碰撞），
    # 存量老域名目录一次性迁移到新名（幂等；碰撞/冲突场景不盲动 + WARNING）
    await asyncio.to_thread(_migrate_scope_dirs)
    # v0.17.4（安全审计 F2）：SSO 身份头必须配合受信代理白名单才生效；
    # 配置了身份头却没配白名单 = 身份头永远不生效（fail-closed），启动时明确告警
    if auth.enabled() and config.AUTH_IDENTITY_HEADER and not config.AUTH_PROXY_ALLOWED_IPS:
        logger.warning("[auth] WARNING: 已配置 AUTH_IDENTITY_HEADER 但未配置"
                       " AUTH_PROXY_ALLOWED_IPS——身份头将被忽略（fail-closed，防伪造）。"
                       "请在 .env 配置受信代理来源 IP 白名单后重启，SSO 身份头才会生效。")
    # v0.17.11（评审）：部门头嵌套在身份头分支内读取——只配部门头不配身份头时
    # 永不生效且无提示，运维难以自查；按 fail-closed 配置须明确告警的惯例补提示
    if auth.enabled() and config.AUTH_IDENTITY_DEPARTMENT_HEADER \
            and not config.AUTH_IDENTITY_HEADER:
        logger.warning("[auth] WARNING: 已配置 AUTH_IDENTITY_DEPARTMENT_HEADER 但未配置"
                       " AUTH_IDENTITY_HEADER——部门头不会被读取（部门头随身份头一同生效），"
                       "SSO 部门透传未启用。")
    # v0.17.42（PLAN #4 Cookie 生产化，rereview P2，用户裁决 a）：认证开启但
    # 未开 AUTH_COOKIE_SECURE 时启动告警——pap_token 不带 Secure 会在 HTTP 明文
    # 链路回传凭据。不阻断启动（本地 HTTP 开发场景合法），与 SSO 白名单告警同一
    # 惯例；Secure 是浏览器侧语义，反向代理终止 TLS 时 Agent 可继续监听 HTTP。
    if auth.enabled() and not config.AUTH_COOKIE_SECURE:
        logger.warning("[auth] WARNING: 认证已开启但 AUTH_COOKIE_SECURE=false——"
                       "pap_token Cookie 未带 Secure，HTTP 明文链路上凭据有窃取风险。"
                       "生产 HTTPS 部署（含反向代理终止 TLS 的场景）请在 .env 设置"
                       " AUTH_COOKIE_SECURE=true；仅本地 HTTP 开发可忽略本告警。")
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
    反向代理身份头（SSO 钩子）、Bearer 凭据（pak- Key / pat- 令牌）或
    pap_token 登录 Cookie（v0.17.37 P0-2：登录态迁 HttpOnly Cookie，
    凭据优先级 SSO 头 > Bearer > Cookie），身份解析后写入
    request.state.user，成为 user_id 的唯一可信来源。
    CSRF 纵深（spec 4.5）：Cookie 身份的非 GET/HEAD/OPTIONS 的 /api/* 请求
    必须带 X-Requested-With 自定义头（跨站表单/导航无法附加），否则 403；
    login/logout 豁免；Bearer/SSO 头凭据不自动随请求携带，免校验。"""
    request.state.user = None
    request.state.auth_via = ""  # header / bearer / cookie（v0.17.39 供端点构造身份快照）
    if not auth.enabled():
        return await call_next(request)
    token = _bearer_token(request)
    # SSO 钩子：身份解析唯一入口（身份头模式优先，其次 Bearer，最后 Cookie）。
    # v0.17.4（安全审计 F2）：身份头必须来自受信代理白名单才生效——
    # 未命中白名单/未配置白名单一律忽略（fail-closed），直连端口伪造头无效。
    header_user = ""
    header_department = ""
    if config.AUTH_IDENTITY_HEADER:
        if auth.proxy_ip_allowed(request.client.host if request.client else None):
            header_user = request.headers.get(config.AUTH_IDENTITY_HEADER, "")
            # 部门头（v0.17.11 PLAN 6.1）：同样只在受信代理来源生效
            if config.AUTH_IDENTITY_DEPARTMENT_HEADER:
                header_department = request.headers.get(
                    config.AUTH_IDENTITY_DEPARTMENT_HEADER, "")
        elif request.headers.get(config.AUTH_IDENTITY_HEADER):
            logger.warning("[auth] 身份头来自非受信来源（不在 AUTH_PROXY_ALLOWED_IPS 白名单），"
                           "已忽略（防伪造冒充）")
    auth_via = ""  # header / bearer / cookie（CSRF 校验只针对 cookie 身份）
    if header_user:
        auth_via = "header"
    elif token:
        auth_via = "bearer"
    else:
        token = request.cookies.get("pap_token", "")
        if token:
            auth_via = "cookie"
    # 解析涉及 SQLite 同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
    request.state.user = await asyncio.to_thread(
        auth.resolve_identity, token, header_user, header_department)
    # Bearer 失效回退 Cookie 身份：迁移期旧客户端可能残留过期 Bearer
    # （历史 localStorage 令牌），不阻断新 Cookie 会话
    if auth_via == "bearer" and request.state.user is None and not header_user:
        cookie_token = request.cookies.get("pap_token", "")
        if cookie_token:
            request.state.user = await asyncio.to_thread(
                auth.resolve_identity, cookie_token, "", header_department)
            if request.state.user is not None:
                auth_via = "cookie"
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/health", "/api/auth/login") \
            and request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # CSRF 纵深（v0.17.37 P0-2，spec 4.5）：置于 401 之后——只对
    # 已认证的 cookie 会话校验，未认证请求仍统一回 401
    if auth_via == "cookie" and request.method not in ("GET", "HEAD", "OPTIONS") \
            and path.startswith("/api/") \
            and path not in ("/api/auth/login", "/api/auth/logout") \
            and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "csrf"}, status_code=403)
    request.state.auth_via = auth_via
    return await call_next(request)


@app.get("/")
async def index():
    return FileResponse(config.PROJECT_ROOT / "web" / "index.html")


@app.get("/console")
async def console_page():
    """管理控制台页面（v0.17.29，PLAN 3.1）。页面级路径不受认证中间件拦截
    （与 / 同口径），登录态由页面自行经 /api/health 判定后展示遮罩或跳转。"""
    return FileResponse(config.PROJECT_ROOT / "web" / "console.html")


@app.get("/tokens.css")
async def tokens_css():
    """共享设计 token（v0.17.28 快赢体系落盘，聊天页与控制台共用单一来源）。
    白名单式静态文件，不做任意路径文件服务。"""
    return FileResponse(config.PROJECT_ROOT / "web" / "tokens.css",
                        media_type="text/css")


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


# ---------------------------------------------------------------- 文档上传（v0.17.7）
# _docx_to_text 与其上限常量留在本文件：测试以 server.main._DOCX_XML_MAX_CHARS
# 为 monkeypatch 锚点（tests/test_deny_paths.py），路由侧经延迟 import 访问。
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
                        logger.warning(f"[startup] WARNING: 存量文件迁移失败（旧版文件将"
                                       f"无法按新分域寻址，请手动移动）: {p.name}: {e}")
                    else:
                        time.sleep(0.05)


def _migrate_scope_dirs() -> None:
    """老域名目录 → user_scope 哈希域名目录（SEC-2，v0.17.32）：文件域改用
    哈希后缀后，存量用户目录须改名寻址才不断档。认领口径：目录名与某在册
    用户（含已吊销——其文件可能仍需本人接续）的 scope_clean 相同。规则：
    唯一认领 → rename（重试 3 次）；多用户碰撞 → 不动 + WARNING（无法判定
    归属，人工分拣）；新名已存在 → 跳过 + WARNING（人工核对）；无人认领 →
    不动（留待管理员核查，绝不盲目归并）；shared 域排除。幂等：迁移后老名
    消失，新哈希名不被认领规则命中，重复启动为空操作。"""
    import time
    try:
        users = auth.list_users()
    except Exception as e:
        logger.warning(f"[startup] WARNING: 用户表读取失败，跳过文件域目录迁移: {e}")
        return
    claims: dict[str, list[str]] = {}
    for u in users:
        claims.setdefault(config.scope_clean(u["username"]), []).append(u["username"])
    for base in (config.UPLOADS_DIR, config.OUTPUTS_DIR,
                 config.PERSONAL_SKILLS_DIR, config.PERSONAL_CUSTOM_TOOLS_DIR):
        if not base.is_dir():
            continue
        for d in list(base.iterdir()):
            if not d.is_dir() or d.name == "shared":
                continue
            owners = claims.get(d.name, [])
            if len(owners) > 1:
                logger.warning(f"[startup] WARNING: 老域名「{d.name}」有 {len(owners)} 位"
                               f"认领用户（清洗碰撞），目录未迁移、请人工分拣: {d}")
                continue
            if not owners:
                continue        # 无人认领（含已是新哈希名）：不动
            new = config.user_scope(owners[0])
            if new == d.name:
                continue
            target = base / new
            if target.exists():
                logger.warning(f"[startup] WARNING: 新域名「{new}」已存在，跳过迁移"
                               f"（请人工核对两目录内容）: {target}")
                continue
            moved = False
            for attempt in range(3):
                try:
                    d.rename(target)
                    moved = True
                    break
                except OSError:
                    if attempt < 2:
                        time.sleep(0.05)
            if moved:
                logger.info(f"[startup] 文件域目录已迁移（SEC-2 哈希防碰撞）: "
                            f"{base.name}/{d.name} → {new}")
            else:
                logger.warning(f"[startup] WARNING: 目录迁移失败（3 次重试），老域名文件"
                               f"将无法按新域寻址，请手动改名: {base.name}/{d.name} → {new}")


# ---------------------------------------------------------------- 知识库定位（v0.17.25，PLAN 2.2）

@app.get("/vendor/{name}")
def vendor_file(name: str):
    """本地 vendor 静态资源（离线前端约束：mammoth 等第三方库 vendor 到
    web/vendor/，内网断网可用，不引 CDN）。仅纯文件名，防穿越。"""
    if not name or name != Path(name).name or ".." in name \
            or "/" in name or "\\" in name:
        return JSONResponse({"error": "非法资源名"}, status_code=404)
    p = config.PROJECT_ROOT / "web" / "vendor" / name
    if not p.is_file():
        return JSONResponse({"error": "资源不存在"}, status_code=404)
    media = "application/javascript" if name.endswith(".js") else \
        "text/css" if name.endswith(".css") else "application/octet-stream"
    return FileResponse(str(p), media_type=media)


# 以下三个知识库辅助函数留在本文件：测试以 server.main 属性为 monkeypatch 锚点
# （tests/test_kb_citation.py 等 patch _kb_home/_declared_kb_packs），
# routes/kb.py 经函数内延迟 import 访问本命名空间。
def _kb_home() -> Path:
    """知识库根目录（kb/<库名>/<文档名>）。独立函数便于测试替身注入。"""
    return Path(__file__).resolve().parents[1] / "kb"


def _declared_kb_packs() -> list[dict]:
    """启用包中声明了 knowledge_bases 的清单（kb 名 → 包归属 + 包级 RBAC）。
    独立函数便于测试替身注入。"""
    return [p for p in packs.enabled_packs() if p.get("knowledge_bases")]


def kb_allowed(kb: str, role: str | None, department: str | None = None,
               packs_list: list[dict] | None = None) -> bool:
    """定位端点权限（fail-closed，PLAN 2.4 多包 OR 合并）：kb 名必须被启用包
    knowledge_bases 声明，且**任一声明包**按「角色 + 部门」双重校验通过即放行
    （跨领域通用库形态：同一库挂多个领域包，各包自治授权，任一授权链成立即可见；
    任一维度未配置 = 该维度放行，与工具层/提示层同口径；role=None 视为开放模式
    放行；admin 全局视图跨部门放行；未分配部门不命中白名单）。堵住「包 RBAC 挡了
    检索工具、定位端点仍可枚举翻库」的旁路（PLAN 2.2 澄清 4）。
    全部声明包拒绝或库未声明 → False（不区分「不存在」与「无权」）。"""
    for p in (packs_list if packs_list is not None else _declared_kb_packs()):
        if kb in (p.get("knowledge_bases") or []):
            if role is None:
                return True  # 开放模式（认证关闭）
            # 角色+部门判定复用 pack_scope_denied（与提示层/执行层同一实现，防口径漂移）；
            # department=None 归一为 "" 保持本函数纯函数语义（不回落上下文，fail-closed）
            if custom_tools.pack_scope_denied(
                    p.get("name", kb), p.get("allowed_roles") or [],
                    p.get("allowed_departments") or [],
                    role=role, department=department or "") is None:
                return True
    return False


# ---- 待迁出域：auth（批 4 拆分进行中，后续 commit 引出至 routes/）----

@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest, request: Request):
    """账号密码登录（v0.15.0）：签发 pat- 短期令牌。
    成功失败都走同一路径，不区分「用户不存在」与「密码错误」（防枚举）。
    PLAN 6.3：登录成败均落审计（who/when/what + 来源 IP）。"""
    if not auth.enabled():
        return JSONResponse({"error": "当前为开放模式，无需登录"}, status_code=400)
    ip = request.client.host if request.client else ""
    # PBKDF2 200k 迭代是 CPU 密集同步调用（实测 300ms+），挪线程池避免阻塞
    # 事件循环（全项目评审问题4；同 resolve_identity 的 to_thread 先例）
    result = await asyncio.to_thread(auth.login, req.username, req.password)
    if result is None:
        audit.log(req.username.strip()[:64], "", "登录失败", detail=ip)
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    audit.log(req.username.strip()[:64], "", "登录成功", detail=ip)
    # v0.17.37 P0-2（spec 4.5）：登录态迁 HttpOnly Cookie——JS 不可读
    # （XSS 窃取面消除），SameSite=Lax 挡跨站附带；AUTH_COOKIE_SECURE=true
    # （HTTPS 部署）追加 Secure；响应体 token 字段保留（程序化 curl 兼容）。
    resp = JSONResponse(result)
    resp.set_cookie(
        "pap_token", result["token"], httponly=True, samesite="lax",
        max_age=int(config.AUTH_TOKEN_TTL_HOURS * 3600), path="/",
        secure=config.AUTH_COOKIE_SECURE)
    return resp


@app.post("/api/auth/logout")
async def logout_endpoint(request: Request):
    """注销：使当前 pat- 登录令牌立即失效（pak- Key 请用吊销/轮换）。
    v0.17.37 P0-2：按 Bearer 或 pap_token Cookie 注销（cookie 登录态同样
    可登出），并以 Max-Age=0 清除浏览器侧 Cookie。logout 豁免 CSRF 校验
    （登出本身无破坏性，且 cookie 过期后登出不应被拦）。"""
    token = _bearer_token(request) or request.cookies.get("pap_token", "")
    await asyncio.to_thread(auth.logout, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("pap_token", path="/")
    return resp


# ---- 待迁出域：chat（批 4 拆分进行中，后续 commit 引出至 routes/）----

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    # 服务端生成不可预测会话 ID（P0-1，v0.17.37）：token_hex(8)=16 位 hex，
    # 替代 uuid4().hex[:8]（可预测且空间小）；客户端自报 ID 仍兼容（续聊既有会话）
    session_id = req.session_id or secrets.token_hex(8)
    run_id = uuid.uuid4().hex[:12]  # 任务维度：一次请求的完整运行
    # 认证模式：user_id 只信中间件解析的身份（请求体 user_id 字段忽略，兼容旧客户端）；
    # 开放模式：沿用请求体自报（演示用）
    if auth.enabled():
        user_id = request.state.user["username"]
        # SEC-1（v0.17.32）：会话归属校验——防止登录用户拿他人 session_id
        # 向其会话写入并接续 LangGraph thread_id 的模型上下文（跨用户上下文
        # 泄露 + 会话状态污染）。校验先于任何写库/进流完成。
        if _chat_session_denied(request.state.user, session_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        # v0.17.39（spec 3.2）：请求级不可变身份快照——中间件解析一次，
        # 执行层经 stream_reply 注入后只读（HTTP 层与执行层同源；SSO 部门
        # 取 IdP 头值，角色恒为本地表）。auth_via 映射 "header" → "sso"。
        u = request.state.user
        identity = usage.Identity(
            username=u["username"], role=u.get("role") or "user",
            department=u.get("department") or "",
            auth_source={"header": "sso"}.get(request.state.auth_via,
                                              request.state.auth_via))
    else:
        user_id = (req.user_id or "anonymous").strip()[:64] or "anonymous"
        # 开放模式无凭据来源，身份固定 admin/无部门（同旧版全局开放契约）；
        # username 与自报 user_id 对齐，避免下游两口径分叉（评审 minor-1）
        identity = usage.Identity(username=user_id, role="admin",
                                  department="", auth_source="open")

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
                # SEC-1（v0.17.33 复验 R1 → v0.17.37 P0-1 升级）：锁外首查
                # 存在并发窗口——取得锁与租约后、首次写入前终判一次
                # （_chat_session_verdict：tombstone 拒 / meta 归属判 / 存量
                # 回填 / 全新会话原子抢注，并发恰一人成功）；被拒以 SSE error
                # 事件终止（已开流，HTTP 状态码不可再改），错误语不区分原因
                # （防探测）。SQLite 读写挪线程池（同下方 append 先例）。
                if auth.enabled() and not await asyncio.to_thread(
                        _chat_session_verdict, request.state.user, session_id):
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话归属校验失败或会话已删除，已拒绝本次写入'}, ensure_ascii=False)}\n\n"
                    return
                # v0.17.38 P0（spec 3.3）：代际栅栏——开场绑定当前代际（删除时
                # 递增），此后消息写入/审批创建/checkpoint/危险工具 resume 各
                # 提交点校验「活跃且代际未变」，旧代际 fail-closed；开放模式
                # tombstone 同口径（认证模式经上方终判，此处双检无害）。
                gen = await asyncio.to_thread(sessions.fence_value, session_id)
                if gen is None:
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已删除，本轮写入被拒'}, ensure_ascii=False)}\n\n"
                    return
                # SQLite 写为同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
                if not await asyncio.to_thread(
                        sessions.append_fenced, session_id, gen, user_id,
                        "user", req.message):
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已删除，本轮写入被拒'}, ensure_ascii=False)}\n\n"
                    return
                discarded = False   # 本轮回复是否因会话删除被拒绝落库
                try:
                    yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'run_id': run_id}, ensure_ascii=False)}\n\n"
                    citations: list[dict] = []  # 本轮 run 的出处（多工具合并）
                    async for ev in agent.stream_reply(new_message, session_id=session_id,
                                                       user_id=user_id, run_id=run_id,
                                                       generation=gen, identity=identity):
                        if lease.lost:
                            # 租约被其他进程接管（心跳续租失败）：继续推进
                            # 等于两进程同时写同一会话，互斥已失效——立即中止
                            # 本轮并让已产出部分按「不完整」落库（架构评审 P2）
                            yield f"data: {json.dumps({'type': 'error', 'content': '会话租约已被其他进程接管，本轮回复已中止，请重新发送消息'}, ensure_ascii=False)}\n\n"
                            break
                        if ev["type"] == "token":
                            collected.append(ev["content"])
                        elif ev["type"] == "citations":
                            # 出处卡片数据（v0.17.25）：随 assistant 消息持久化，
                            # 历史回放可重渲染；同轮多次工具调用由前端去重
                            citations.extend(ev.get("citations") or [])
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
                        # v0.17.38 P0：assistant 写回经代际栅栏——会话在流中途
                        # 被删除时拒绝落库（删除后存储不增），改以 error 事件告知
                        discarded = not await asyncio.to_thread(
                            sessions.append_fenced, session_id, gen, user_id,
                            "assistant", text,
                            extra=(json.dumps(citations, ensure_ascii=False)
                                   if citations else None))
                if discarded:
                    logger.warning(f"[sessions] 会话 {session_id} 已删除，本轮 assistant 回复被丢弃")
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已被删除，本轮回复已丢弃'}, ensure_ascii=False)}\n\n"
            finally:
                await lease.__aexit__()  # 停心跳 + 释放租约（与 __aenter__ 配对）

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _chat_session_denied(identity: dict, session_id: str) -> bool:
    """chat 锁外快查（SEC-1 快路径，v0.17.37 升级 meta 口径）：tombstone 或
    既有归属冲突 → 403 快速返回，避免开流后才拒；空会话（新会话首轮）放行
    ——首轮写入前无从校验归属（v0.17.32 语义保留）。返回 True = 拒绝。
    评审 #2 修复：存量会话（meta 无）改用与锁内终判一致的 resolve_owner
    口径（最早 user 消息作者归属，顺带回填 meta）——旧 owners 全体口径会把
    合法属主因他人也曾发言而误拒 403（过严方向，无越权但伤可用性）。
    终判以锁内 _chat_session_verdict 为准（锁外检查存在并发窗口，
    沿 v0.17.33 双查先例）。"""
    meta = sessions.get_meta(session_id)
    if meta is not None:
        if meta["deleted"] is not None:
            return True                        # tombstone：删除即封禁（含 admin）
        if identity["role"] == "admin":
            return False
        return meta["owner_id"] not in (identity["username"], "anonymous")
    if identity["role"] == "admin":
        return False
    owner = sessions.resolve_owner(session_id)
    if owner is None:
        return False            # 空会话（新会话首轮）放行，终判负责原子抢注
    return owner not in (identity["username"], "anonymous")


def _chat_session_verdict(identity: dict, session_id: str) -> bool:
    """chat 锁内终判（v0.17.37 P0-1）：返回 True=放行 / False=拒绝。
    - tombstone → 拒（删除 = 不可逆终局，含 admin——admin 治理语义保留在
      读/删端点，chat 不得复活已删除会话的模型上下文）；
    - admin 放行（不注册不改写归属——接入他人会话不漂移所有权，报告建议 5）；
    - meta 有归属：本人或 anonymous 宽恕放行，否则拒；
    - meta 无：messages 有 → resolve_owner 回填存量归属后按上判；皆无 →
      全新会话，register_session 原子抢注（并发恰一人成功，失败=被抢 → 拒）。"""
    meta = sessions.get_meta(session_id)
    if meta is not None and meta["deleted"] is not None:
        return False
    if identity["role"] == "admin":
        return True
    if meta is not None:
        return meta["owner_id"] in (identity["username"], "anonymous")
    owner = sessions.resolve_owner(session_id)
    if owner is None:
        return sessions.register_session(
            session_id, identity["username"]) == "ok"
    return owner in (identity["username"], "anonymous")


# ---- 待迁出域：kb（批 4 拆分进行中，后续 commit 引出至 routes/）----

_KB_DOC_MAX = 200  # 文档名长度上限（防超长名资源浪费；正常文档名远小于此）


_KB_SANITIZE_MAX = 2 * 1024 * 1024  # Mammoth 输出消毒上限（超限 413，防无界处理）


@app.post("/api/kb/sanitize")
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


@app.get("/api/kb/source")
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
    if auth.enabled():
        ident = _acl_identity(request)
        if not ident:
            return JSONResponse({"error": "未登录"}, status_code=401)
        role = ident.get("role", "")
        department = ident.get("department", "") or ""
    else:
        role = None  # 开放模式：与现有开放部署行为一致
        department = None
    if not kb_allowed(kb, role, department):
        return JSONResponse({"error": "知识库不存在"}, status_code=404)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", kb or ""):
        return JSONResponse({"error": "知识库不存在"}, status_code=404)
    # 文档名防御：NFC 归一化后必须为纯文件名（穿越/控制字符一律 404）
    doc = unicodedata.normalize("NFC", doc or "")
    if (not doc or len(doc) > _KB_DOC_MAX or doc != Path(doc).name
            or "/" in doc or "\\" in doc or ".." in doc
            or any(ord(c) < 32 for c in doc)):
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    path = _kb_home() / kb / doc
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


# ---- 待迁出域：approvals（批 4 拆分进行中，后续 commit 引出至 routes/）----

@app.get("/api/approvals")
async def approval_list(request: Request, limit: int = 50):
    """最近审批记录（审计）：工具、参数摘要、状态（pending/approved/rejected/
    expired/abandoned，abandoned = 已批准但执行丢失，见 approval.mark_abandoned）。
    user 角色只看自己创建的；approver/admin 看全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    # limit<=0 回退默认（全项目评审问题3：SQLite LIMIT 负数 = 全量返回，防绕过 200 条上限）
    return {"approvals": approval.list_recent(
        limit=min(limit if limit > 0 else 50, 200), user_id=uid)}


@app.post("/api/approve")
async def approve(req: ApproveRequest, request: Request):
    """前端提交危险操作审批决定。
    决议权限（v0.17.9 收紧）：仅 admin/approver 可决议任何 pending——此前
    「user 可决议自己创建的」存在自审自批漏洞（发起者自己批准即可放行
    危险操作）；user 角色发起的审批等待 approver/admin 决议。
    ok=false 时 error 区分 forbidden（无权限）与 not_found（不存在/已决议）。"""
    u = request.state.user or {"username": "", "role": "admin"}  # 开放模式：任何人可决议（旧行为）
    result = approval.resolve(req.approval_id, req.approve, u["username"], u["role"])
    if result == "ok":
        audit.log(u["username"], u["role"],
                  "审批通过" if req.approve else "审批驳回",
                  target=req.approval_id)
        return {"ok": True}
    if result == "forbidden":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": False, "error": "not_found_or_decided"}


# ---- 路由注册（编码规范批 4：按资源域拆出 server/routes/，端点实现原样搬迁）----
# /api/chat 现位于 server/routes/chat.py；tests 中锚定实现文件的静态守卫已随迁。
from server.routes import register  # noqa: E402

register(app)


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
    logger.info(f"Private Agent POC 启动于 http://localhost:{port}  (模式: {'mock' if config.MOCK_LLM else 'live'})")
    uvicorn.run(app, host=host, port=port)
