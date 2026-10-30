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
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from server import (agent, approval, audit, auth, config, custom_tools, packs,
                    session_lock, sessions, skills, telemetry, usage)
from tools import kb_parsers  # 定位端点按锚点重解析文档（服务器侧，不进包）


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
        print("[auth] WARNING: 已配置 AUTH_IDENTITY_HEADER 但未配置"
              " AUTH_PROXY_ALLOWED_IPS——身份头将被忽略（fail-closed，防伪造）。"
              "请在 .env 配置受信代理来源 IP 白名单后重启，SSO 身份头才会生效。")
    # v0.17.11（评审）：部门头嵌套在身份头分支内读取——只配部门头不配身份头时
    # 永不生效且无提示，运维难以自查；按 fail-closed 配置须明确告警的惯例补提示
    if auth.enabled() and config.AUTH_IDENTITY_DEPARTMENT_HEADER \
            and not config.AUTH_IDENTITY_HEADER:
        print("[auth] WARNING: 已配置 AUTH_IDENTITY_DEPARTMENT_HEADER 但未配置"
              " AUTH_IDENTITY_HEADER——部门头不会被读取（部门头随身份头一同生效），"
              "SSO 部门透传未启用。")
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
    header_department = ""
    if config.AUTH_IDENTITY_HEADER:
        if auth.proxy_ip_allowed(request.client.host if request.client else None):
            header_user = request.headers.get(config.AUTH_IDENTITY_HEADER, "")
            # 部门头（v0.17.11 PLAN 6.1）：同样只在受信代理来源生效
            if config.AUTH_IDENTITY_DEPARTMENT_HEADER:
                header_department = request.headers.get(
                    config.AUTH_IDENTITY_DEPARTMENT_HEADER, "")
        elif request.headers.get(config.AUTH_IDENTITY_HEADER):
            print("[auth] 身份头来自非受信来源（不在 AUTH_PROXY_ALLOWED_IPS 白名单），"
                  "已忽略（防伪造冒充）")
    # 解析涉及 SQLite 同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
    request.state.user = await asyncio.to_thread(
        auth.resolve_identity, token, header_user, header_department)
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/health", "/api/auth/login") \
            and request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


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
    return result


@app.post("/api/auth/logout")
async def logout_endpoint(request: Request):
    """注销：使当前 pat- 登录令牌立即失效（pak- Key 请用吊销/轮换）。"""
    token = _bearer_token(request)
    auth.logout(token)
    return {"ok": True}


# ---- PLAN 3.1 管理控制台：用户与 Key 管理（admin 专属，CLI 能力收口到页面）----

def _admin_guard(request: Request):
    """管理端点统一守卫（七端点复用；与包级 RBAC 同构的双层拦截——前端隐藏
    入口只是体验层，服务端判定才是防线）。返回 JSONResponse 表示拦截。"""
    if not auth.enabled():
        return JSONResponse(
            {"error": "开放模式无用户体系，管理端点不可用"}, status_code=400)
    if request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.state.user.get("role") != "admin":
        return JSONResponse({"error": "需要管理员角色"}, status_code=403)
    return None


def _audit(operator: str, action: str, target: str, role: str = "") -> None:
    """管理动作结构化日志（AC10）+ 统一审计落库（PLAN 6.3）。
    绝不打印明文 Key 等敏感值。"""
    print(f"[admin] {operator} {action} {target}")
    audit.log(operator, role, action, target=target)


class AdminUserCreate(BaseModel):
    username: str
    role: str = "user"
    # 上界 10 年：防止超大值写出永不触发的 expires_at（评审问题3；CLI 暂未加）
    days: int = Field(default=0, ge=0, le=3650)
    department: str = ""


class AdminUserOption(BaseModel):
    days: int = Field(default=0, ge=0, le=3650)


class AdminPasswordSet(BaseModel):
    password: str


class AdminRoleSet(BaseModel):
    role: str


class AdminDepartmentSet(BaseModel):
    department: str = ""


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    """用户全量清单（AC2）：口径与 CLI list 一致，状态判定（正常/已过期/已吊销）
    由前端按 revoked / expires_at 呈现。"""
    if err := _admin_guard(request):
        return err
    return {"users": auth.list_users()}


@app.post("/api/admin/users")
async def admin_create_user(request: Request, req: AdminUserCreate):
    """建用户（AC3）：响应携带一次性明文 Key，服务端不落日志不复存。"""
    if err := _admin_guard(request):
        return err
    key = auth.add_user(req.username, role=req.role, days=req.days,
                        department=req.department)
    if key is None:
        return JSONResponse(
            {"error": "创建失败：用户名已存在、为保留名（shared）、超长（>64 字）或角色非法"},
            status_code=400)
    _audit(request.state.user["username"], "创建用户", req.username.strip(),
           role=request.state.user["role"])
    return {"key": key}


@app.post("/api/admin/users/{username}/rotate")
async def admin_rotate_key(request: Request, username: str,
                           req: AdminUserOption):
    """轮换 Key（AC4）：旧 Key 立即失效。已吊销用户拒绝轮换——rotate 会清
    吊销位，须封死「借轮换复活」的口子（吊销不可恢复，spec 澄清 4）。"""
    if err := _admin_guard(request):
        return err
    # 评审问题1：路径参数先归一化（auth 层 set_role 等会 strip），否则带空格
    # 变体（如 %20admin）可绕过「不能操作自己」的自保护比较
    username = (username or "").strip()
    if auth.user_by_name(username) is None:
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    key = auth.rotate_user(username, days=req.days)
    if key is None:
        return JSONResponse({"error": "用户不存在"}, status_code=404)
    _audit(request.state.user["username"], "轮换 Key", username,
           role=request.state.user["role"])
    return {"key": key}


@app.post("/api/admin/users/{username}/password")
async def admin_reset_password(request: Request, username: str,
                               req: AdminPasswordSet):
    """重置密码（AC5）：长度服务端强校验，明文不落日志。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if auth.user_by_name(username) is None:
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    if len(req.password or "") < auth.MIN_PASSWORD_LEN:
        return JSONResponse(
            {"error": f"密码至少 {auth.MIN_PASSWORD_LEN} 位"}, status_code=400)
    if not auth.set_password(username, req.password):
        return JSONResponse({"error": "密码设置失败"}, status_code=400)
    _audit(request.state.user["username"], "重置密码", username,
           role=request.state.user["role"])
    return {"ok": True}


@app.post("/api/admin/users/{username}/role")
async def admin_set_role(request: Request, username: str, req: AdminRoleSet):
    """设角色（AC6）：立即生效；admin 不能把自己的角色改为非 admin
    （防最后一个管理员自锁，端点层判定——CLI 保持原行为）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if req.role not in auth.ROLES:
        return JSONResponse({"error": "非法角色"}, status_code=400)
    operator = request.state.user["username"]
    if operator == username and req.role != "admin":
        return JSONResponse(
            {"error": "不能把自己的角色改为非 admin"}, status_code=400)
    if not auth.set_role(username, req.role):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(operator, "设角色", f"{username} → {req.role}",
           role=request.state.user["role"])
    return {"ok": True}


@app.post("/api/admin/users/{username}/department")
async def admin_set_department(request: Request, username: str,
                               req: AdminDepartmentSet):
    """设部门（AC7）：空串清除；调岗即时生效（报表/知识库分库权限按新部门）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if not auth.set_department(username, req.department):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(request.state.user["username"], "设部门",
           f"{username} → {req.department.strip() or '（清空）'}",
           role=request.state.user["role"])
    return {"ok": True}


@app.post("/api/admin/users/{username}/revoke")
async def admin_revoke_user(request: Request, username: str):
    """吊销账号（AC8）：pak- Key 与 pat- 令牌立即失效，不可恢复（无解禁）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    operator = request.state.user["username"]
    if operator == username:
        return JSONResponse({"error": "不能吊销自己"}, status_code=400)
    if not auth.revoke_user(username):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(operator, "吊销账号", username, role=request.state.user["role"])
    return {"ok": True}


@app.get("/api/audit")
async def audit_query(request: Request, actor: str = "", action: str = "",
                      since: int = 0, limit: int = 200):
    """审计流水查询（PLAN 6.3，AC5）：admin 专属。过滤：操作人/动作/起始时间
    （毫秒）；limit 收敛 [1,1000]（防全表拉取）。拒绝路径复用 _admin_guard。"""
    if err := _admin_guard(request):
        return err
    return {"records": audit.query(actor=actor, action=action,
                                   since_ms=since, limit=limit)}


@app.get("/api/audit/export")
async def audit_export(request: Request, actor: str = "", action: str = "",
                       since: int = 0):
    """审计流水导出 CSV（AC5）：admin 专属；含 UTF-8 BOM 保证 Excel 中文不乱码。"""
    import csv
    import io as _io
    if err := _admin_guard(request):
        return err
    rows = audit.query(actor=actor, action=action, since_ms=since,
                       limit=1000)
    buf = _io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM：Excel 打开 CSV 中文防乱码
    w = csv.writer(buf)
    w.writerow(["时间", "操作人", "角色", "动作", "目标", "详情"])
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000))
        w.writerow([ts, r["actor"], r["role"], r["action"],
                    r["target"], r["detail"]])
    # 直接返回 writer 输出（默认行尾已是 \r\n，且 \ufeff BOM 在首）：不做
    # splitlines 再拼接——str.splitlines 按 \u2028/\x85 等 Unicode 行边界切分，
    # 会把 csv 未引用该类字符的记录裂成多条残行（评审 C2，可被含特殊字符的
    # 用户名污染导出完整性）
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="audit_log.csv"'})


# ---- PLAN 3.2 包与审批面板：包管理端点（admin 专属，packs.py 函数收口到页面）----

def _pack_toggle_result(result: tuple[str, str]):
    """启停结构化结果 → HTTP 响应（评审①：按状态分流，不靠文案子串猜测）。"""
    status, msg = result
    if status == "ok":
        return {"ok": True, "message": msg}
    return JSONResponse(
        {"error": msg},
        status_code={"not_found": 404, "refused": 400, "error": 500}[status])


@app.get("/api/admin/packs")
async def admin_list_packs(request: Request):
    """包全量清单（AC1）：口径与 packs.list_packs 管理视图一致，含平台包。"""
    if err := _admin_guard(request):
        return err
    return {"packs": packs.list_packs()}


@app.post("/api/admin/packs/{name}/disable")
async def admin_disable_pack(request: Request, name: str):
    """停用领域包（AC2/AC3）：立即生效并持久化（STATE_DB disabled_packs 表）。
    平台包拒停（400，前端按钮隐藏只是体验层，服务端是防线）。"""
    if err := _admin_guard(request):
        return err
    result = packs._toggle_pack(name.strip(), enable=False)
    if result[0] == "ok":  # 仅成功动作记审计（AC4）
        _audit(request.state.user["username"], "停用包", name.strip(),
               role=request.state.user["role"])
    return _pack_toggle_result(result)


@app.post("/api/admin/packs/{name}/enable")
async def admin_enable_pack(request: Request, name: str):
    """启用领域包（AC2/AC3）：status≠ok 拒启（400，不把故障包引进运行时）。"""
    if err := _admin_guard(request):
        return err
    result = packs._toggle_pack(name.strip(), enable=True)
    if result[0] == "ok":  # 仅成功动作记审计（AC4）
        _audit(request.state.user["username"], "启用包", name.strip(),
               role=request.state.user["role"])
    return _pack_toggle_result(result)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "anonymous"   # 用户标识（token 计量维度；API 认证落地后由网关/中间件注入）


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


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    session_id = req.session_id or uuid.uuid4().hex[:8]
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
                    citations: list[dict] = []  # 本轮 run 的出处（多工具合并）
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
                        await asyncio.to_thread(
                            sessions.append, session_id, user_id, "assistant", text,
                            extra=(json.dumps(citations, ensure_ascii=False)
                                   if citations else None))
            finally:
                await lease.__aexit__()  # 停心跳 + 释放租约（与 __aenter__ 配对）

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _acl_identity(request: Request) -> dict | None:
    """资源级 ACL（v0.14.1）：返回当前身份 {username, role}；
    开放模式返回 None（不过滤，行为同旧版）。
    使用规则：role == 'user'/'approver' 只能访问自己的资源（v0.17.29 会话
    隐私收紧，approver 由全局视图改为仅本人）；admin 全局视图。"""
    if not auth.enabled():
        return None
    return request.state.user


def _can_access_session(identity: dict, session_id: str) -> bool:
    """user/approver 角色只能访问全部消息都属于自己（或开放模式遗留
    anonymous）的会话；admin 保留全局视图（管理职责）。v0.17.29（PLAN 3.1
    人工测试反馈）：approver 由全局视图收紧为仅本人——会话内容属用户隐私，
    审批所需信息经审批单流转，无需读取会话原文。"""
    if identity["role"] == "admin":
        return True
    owners = sessions.session_users(session_id)
    return bool(owners) and owners <= {identity["username"], "anonymous"}


def _chat_session_denied(identity: dict, session_id: str) -> bool:
    """chat 端点专属会话归属校验（SEC-1，v0.17.32）：登录用户向既有会话发言时，
    会话全部属主必须是本人（或开放模式遗留 anonymous），否则 403。与
    _can_access_session 的关键差异：空会话（新会话首轮）必须放行——chat 是
    会话属主的判定写入来源，首轮写入前无从校验；复用后者（空 owners 返回
    False）会把新会话也判为 forbidden，首轮即死锁。admin 放行（全局视图）。
    返回 True = 拒绝。"""
    if identity["role"] == "admin":
        return False
    owners = sessions.session_users(session_id)
    if not owners:
        return False
    return not owners <= {identity["username"], "anonymous"}


@app.get("/api/sessions")
async def session_list(request: Request, limit: int = 50):
    """会话列表（对话记录来自 SQLite，重启后仍在）。
    user/approver 只列自己的会话（v0.17.29 收紧）；admin 列全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] in ("user", "approver") else ""
    # limit<=0 回退默认（全项目评审问题3：SQLite LIMIT 负数 = 全量返回，防绕过 200 条上限）
    return {"sessions": sessions.list_sessions(
        limit=min(limit if limit > 0 else 50, 200), user_id=uid)}


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
        print(f"[startup] WARNING: 用户表读取失败，跳过文件域目录迁移: {e}")
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
                print(f"[startup] WARNING: 老域名「{d.name}」有 {len(owners)} 位"
                      f"认领用户（清洗碰撞），目录未迁移、请人工分拣: {d}")
                continue
            if not owners:
                continue        # 无人认领（含已是新哈希名）：不动
            new = config.user_scope(owners[0])
            if new == d.name:
                continue
            target = base / new
            if target.exists():
                print(f"[startup] WARNING: 新域名「{new}」已存在，跳过迁移"
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
                print(f"[startup] 文件域目录已迁移（SEC-2 哈希防碰撞）: "
                      f"{base.name}/{d.name} → {new}")
            else:
                print(f"[startup] WARNING: 目录迁移失败（3 次重试），老域名文件"
                      f"将无法按新域寻址，请手动改名: {base.name}/{d.name} → {new}")


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
        ident = _acl_identity(request)
        audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
                  "文件上传", target=safe)
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
    ident = _acl_identity(request)
    audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
              "文件下载", target=rel)
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
    ident = _acl_identity(request)
    audit.log(ident["username"] if ident else "", ident["role"] if ident else "",
              "文件删除", target=Path(rel).name)
    return {"deleted": rel}


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


_KB_DOC_MAX = 200  # 文档名长度上限（防超长名资源浪费；正常文档名远小于此）


def _source_page(title: str, body: str) -> HTMLResponse:
    """片段视图公共壳：纯内联样式（离线前端约束），命中行 .hl 高亮。"""
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>"
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
        f"<h1>知识库原文</h1><div class='path'>{title}</div>{body}</body></html>")


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
        fallback = "".join(
            f"<p class='hl' data-a='{(anc or {}).get('a', '')}'>{ln}</p>"
            if anc and a <= (anc.get("a") or 0) <= b else f"<p>{ln}</p>"
            for ln, anc in rows)
        src_url = f"/api/kb/source?kb={quote(kb)}&doc={quote(doc)}&kind=para&a={a}&raw=1"
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{doc}</title><style>"
            "body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;"
            "margin:24px;color:#1f2937;max-width:860px;line-height:1.75}"
            "h1{font-size:16px}.path{color:#6b7280;font-size:13px;margin-bottom:16px}"
            "table{border-collapse:collapse}td,th{border:1px solid #d1d5db;"
            "padding:4px 10px}.hl{background:#fef9c3}"
            "#fallback{display:none;border:1px dashed #d1d5db;padding:12px;"
            "border-radius:8px}"
            "</style></head><body>"
            f"<h1>知识库原文（Word 还原）</h1><div class='path'>{logical}</div>"
            "<div id='content'>加载中……</div>"
            "<div id='fallback'><p>浏览器还原失败，显示提取文本：</p>"
            + fallback + "</div>"
            "<script src='/vendor/mammoth.browser.min.js'></script>"
            "<script>(function(){"
            "var mark=function(root){var lo=" + str(a) + ",hi=" + str(b) + ";"
            "var ps=root.querySelectorAll('p');for(var i=0;i<ps.length;i++){"
            "if(ps[i].getAttribute('data-a')&&+ps[i].getAttribute('data-a')>=lo"
            "&&+ps[i].getAttribute('data-a')<=hi){ps[i].classList.add('hl');"
            "if(!window.__scrolled){window.__scrolled=1;"
            "ps[i].scrollIntoView({block:'center'});}}}};"
            "fetch('" + src_url + "').then(function(r){if(!r.ok)throw 0;"
            "return r.arrayBuffer()}).then(function(buf){"
            "mammoth.convertToHtml({arrayBuffer:buf})"
            ".then(function(res){document.getElementById('content').innerHTML=res.value;"
            "mark(document);})"
            ".catch(function(){document.getElementById('content').style.display='none';"
            "document.getElementById('fallback').style.display='block';});})"
            ".catch(function(){document.getElementById('content').style.display='none';"
            "document.getElementById('fallback').style.display='block';});})();</script>"
            "</body></html>")
        return HTMLResponse(page)

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


@app.get("/api/usage")
async def usage_stats(request: Request, user_id: str = "", session_id: str = "", days: int = 0):
    """Token 消耗聚合：总量 + 按用户 / 按会话 / 按任务（run）分组。
    days>0 时只统计最近 N 天。user 角色强制只统计本人（查询参数不可越权）。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        user_id = ident["username"]
    return usage.summary(user_id=user_id, session_id=session_id, days=days)


def _dept_report_guard(request: Request):
    """部门报表 ACL（v0.17.21，PLAN 6.2）：管理视角——user 角色 403；
    approver/admin 与开放模式（_acl_identity 返回 None）放行，同 /api/usage 口径。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        return JSONResponse({"error": "forbidden",
                             "message": "部门报表仅限管理角色查看"}, status_code=403)


@app.get("/api/usage/departments")
async def usage_departments(request: Request, days: int = 0):
    """部门维度用量报表（含 6.5 金额列）：按 api_users 当前部门映射聚合，
    无部门用户计入「未分配」。days>0 只统计最近 N 天。"""
    denied = _dept_report_guard(request)
    if denied:
        return denied
    return usage.department_usage(days=days, department_map=auth.department_map())


@app.get("/api/usage/departments.csv")
async def usage_departments_csv(request: Request, days: int = 0):
    """部门成本报表 CSV 导出（UTF-8 带 BOM，Excel 直开不乱码）。"""
    denied = _dept_report_guard(request)
    if denied:
        return denied
    text, filename = usage.department_csv(days=days, department_map=auth.department_map())
    return Response(content=text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class ApproveRequest(BaseModel):
    approval_id: str
    approve: bool


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
