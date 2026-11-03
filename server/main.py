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
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from server import (agent, approval, audit, auth, config, custom_tools,
                    packs, telemetry)
from server.webdeps import _bearer_token

logger = logging.getLogger(__name__)


APPROVAL_SCAN_INTERVAL = 300.0  # 周期扫描间隔（秒）：已批准但未执行的审批 → abandoned


async def _approval_scan_loop() -> None:
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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
async def auth_middleware(request: Request, call_next) -> Any:
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
async def index() -> FileResponse:
    return FileResponse(config.PROJECT_ROOT / "web" / "index.html")


@app.get("/console")
async def console_page() -> FileResponse:
    """管理控制台页面（v0.17.29，PLAN 3.1）。页面级路径不受认证中间件拦截
    （与 / 同口径），登录态由页面自行经 /api/health 判定后展示遮罩或跳转。"""
    return FileResponse(config.PROJECT_ROOT / "web" / "console.html")


@app.get("/tokens.css")
async def tokens_css() -> FileResponse:
    """共享设计 token（v0.17.28 快赢体系落盘，聊天页与控制台共用单一来源）。
    白名单式静态文件，不做任意路径文件服务。"""
    return FileResponse(config.PROJECT_ROOT / "web" / "tokens.css",
                        media_type="text/css")


@app.get("/api/health")
async def health(request: Request) -> dict:
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
# _docx_to_text 已迁往 server/routes/files.py（批 4 收尾，仅上传端点使用）


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
def vendor_file(name: str) -> Any:
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


# 知识库权限判定与库根目录定位已迁往 server/kb_access.py（批 4 收尾，
# 纠正 routes → main 反向依赖；测试 monkeypatch 锚点随迁该模块）。


# ---- 路由注册（编码规范批 4：按资源域拆出 server/routes/，端点实现原样搬迁）----
from server.routes import register  # noqa: E402

register(app)


def _parse_host_port() -> tuple[str, int]:
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
